#!/usr/bin/env python3
"""
AI Roundtable — a lightweight local web app for a shared brainstorm between you,
Claude Code, OpenAI Codex, and Grok Build.

The app owns ONE shared transcript. Every turn, the full labeled conversation and
the current participant roster are handed to whichever selected agent is replying,
so active participants can address / question / build on each other.

Replies stream live (NDJSON over a long-lived POST). If you barge in, the browser
aborts the request and the server kills the agent's process group/tree so it stops
generating.

No third-party dependencies: run `python3 server.py` (`py -3 server.py` on Windows),
then open the printed URL.
Auth is reused from your existing `claude`, `codex`, and `grok` CLI logins.
"""

import contextlib
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import queue
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

# ---------------------------------------------------------------------------
# Config (env-overridable)
# ---------------------------------------------------------------------------
HOST = os.environ.get("ROUNDTABLE_HOST", "127.0.0.1")
_CLI_PORT = sys.argv[1] if __name__ == "__main__" and len(sys.argv) > 1 else 8765
PORT = int(os.environ.get("ROUNDTABLE_PORT", _CLI_PORT))
HERE = os.path.dirname(os.path.abspath(__file__))
WINDOWS_SHIM_BRIDGE = os.path.join(HERE, "windows_shim.ps1")
WINDOWS_SHIM_PATH_ENV = "AICONVO_SHIM_PATH"
WINDOWS_SHIM_ARGS_ENV = "AICONVO_SHIM_ARGS_JSON"
# Session signing keys are handed to an in-app replacement process through a
# private, one-use file.  The environment contains only the file name, never the
# key itself.  The legacy direct-token name remains solely so it can be stripped
# from provider environments created by older launchers/configurations.
REMOTE_TOKEN_ENV = "ROUNDTABLE_REMOTE_TOKEN"
REMOTE_TOKEN_FILE_ENV = "ROUNDTABLE_SESSION_SECRET_FILE"
_REMOTE_TOKEN_FILE_PREFIX = ".ai-roundtable-session-"


def _host_is_loopback(value):
    if str(value).lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _canonical_bind_host(value):
    """Resolve an IPv4 bind target once so URL and Host checks agree."""
    raw = str(value).strip()
    if not raw:
        return ""
    if raw.casefold() == "localhost":
        return "127.0.0.1"
    try:
        address = ipaddress.ip_address(raw)
    except ValueError:
        try:
            candidates = socket.getaddrinfo(
                raw, None, socket.AF_INET, socket.SOCK_STREAM,
            )
        except OSError as exc:
            raise ValueError(f"cannot resolve {raw!r}") from exc
        if not candidates:
            raise ValueError(f"cannot resolve {raw!r} to IPv4")
        address = ipaddress.ip_address(candidates[0][4][0])
    if address.version != 4:
        raise ValueError("IPv6 binds are not supported; use an IPv4 address")
    return str(address)


try:
    HOST = _canonical_bind_host(HOST)
except ValueError as exc:
    raise SystemExit(f"Invalid ROUNDTABLE_HOST: {exc}") from exc


def _is_lan_ipv4(address):
    """Allow private/non-global IPv4, including RFC6598 VPN addresses."""
    try:
        address = ipaddress.ip_address(address)
    except ValueError:
        return False
    return address.version == 4 and not (
        address.is_loopback or address.is_unspecified or address.is_link_local
        or address.is_multicast or address.is_global
    )


def _consume_remote_token_handoff():
    """Read and remove the one-use session-signing-key restart handoff."""
    # Never accept a signing key directly through the environment.  Remove the
    # obsolete variable in case an older parent process supplied it.
    os.environ.pop(REMOTE_TOKEN_ENV, None)
    token = ""
    path = os.environ.pop(REMOTE_TOKEN_FILE_ENV, "")
    if not path:
        return token

    absolute = os.path.abspath(path)
    expected_dir = os.path.realpath(tempfile.gettempdir())
    valid_name = (
        os.path.realpath(os.path.dirname(absolute)) == expected_dir
        and os.path.basename(absolute).startswith(_REMOTE_TOKEN_FILE_PREFIX)
        and absolute.endswith(".key")
    )
    if not valid_name:
        return token

    owned_file = False
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(absolute, flags)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 1024:
                return token
            if hasattr(os, "getuid") and info.st_uid != os.getuid():
                return token
            if os.name != "nt" and info.st_mode & 0o077:
                return token
            owned_file = True
            candidate = os.read(fd, 1025).decode("utf-8")
            if candidate:
                token = candidate
        finally:
            os.close(fd)
    except (OSError, UnicodeError):
        pass
    finally:
        if owned_file:
            try:
                os.unlink(absolute)
            except OSError:
                pass
    return token


REMOTE_ACCESS_ENABLED = not _host_is_loopback(HOST)
REMOTE_SESSION_SECRET = _consume_remote_token_handoff() or secrets.token_urlsafe(32)
SERVER_INSTANCE_ID = secrets.token_urlsafe(12)


def _lan_ipv4_addresses():
    """Best-effort LAN addresses for device links, without sending network traffic."""
    found = set()
    primary = ""
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(item[4][0])
    except OSError:
        pass
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("192.0.2.1", 9))  # route lookup only; UDP connect sends nothing
            primary = probe.getsockname()[0]
            found.add(primary)
        finally:
            probe.close()
    except OSError:
        pass

    result = []
    for value in found:
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            continue
        if _is_lan_ipv4(address):
            result.append(str(address))
    return sorted(result, key=lambda value: (value != primary, ipaddress.ip_address(value)))


def _remote_urls(port=None, bind_host=None):
    port = PORT if port is None else port
    bind_host = HOST if bind_host is None else bind_host
    addresses = _lan_ipv4_addresses()
    if bind_host not in ("", "0.0.0.0") and not _host_is_loopback(bind_host):
        # An explicit single-interface bind must not advertise addresses where
        # the listener cannot accept connections.
        addresses = [bind_host]
    return [f"http://{address}:{port}/" for address in addresses]


def _absolute_user_path(value):
    return os.path.abspath(os.path.expanduser(value))

# Directory the agents run in. Read-only tools (file inspection) are scoped here.
AGENT_CWD = _absolute_user_path(os.environ.get("ROUNDTABLE_CWD", HERE))

# Default Claude model. Opus 4.8 for the strongest reasoning; switch to Sonnet in
# the UI for snappier replies. Codex uses its own configured default unless set.
DEFAULT_CLAUDE_MODEL = os.environ.get("ROUNDTABLE_CLAUDE_MODEL", "claude-opus-4-8")
DEFAULT_CODEX_MODEL = os.environ.get("ROUNDTABLE_CODEX_MODEL", "")  # "" = codex default
DEFAULT_GROK_MODEL = os.environ.get("ROUNDTABLE_GROK_MODEL", "grok-4.5")

CONFIG_PATH = _absolute_user_path(os.environ.get("ROUNDTABLE_CONFIG", os.path.join(HERE, "config.json")))
CODEX_HOME = _absolute_user_path(os.environ.get("CODEX_HOME", "~/.codex"))
GROK_HOME = _absolute_user_path(os.environ.get("GROK_HOME", "~/.grok"))
DEFAULT_USER_NAME = "You"
MAX_USER_NAME = 64
AI_KEYS = ("claude", "codex", "grok")

LAN_PASSWORD_CONFIG_KEY = "lanPassword"
LAN_PASSWORD_MIN_CHARS = 8
LAN_PASSWORD_MAX_CHARS = 128
LAN_PASSWORD_MAX_BYTES = 512
LAN_PASSWORD_SCRYPT_N = 1 << 15
LAN_PASSWORD_SCRYPT_R = 8
LAN_PASSWORD_SCRYPT_P = 3
LAN_PASSWORD_SALT_BYTES = 16
LAN_PASSWORD_DKLEN = 32
LAN_PASSWORD_SCRYPT_MAXMEM = 128 * 1024 * 1024

REMOTE_SESSION_TTL = 24 * 60 * 60
REMOTE_SESSION_MAX_TOKEN_BYTES = 2048
_SESSION_AUDIENCE = "ai-roundtable-lan-v1"
_AUTH_LOCK = threading.RLock()

LOGIN_RATE_WINDOW = 60.0
LOGIN_RATE_MAX_ATTEMPTS = 5
LOGIN_RATE_GLOBAL_MAX_ATTEMPTS = 20
_LOGIN_ATTEMPTS = {}
_LOGIN_GLOBAL_ATTEMPTS = []
_LOGIN_RATE_LOCK = threading.Lock()
_LOGIN_SCRYPT_SLOTS = threading.BoundedSemaphore(1)


def _b64encode(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value):
    if not isinstance(value, str) or not value:
        raise ValueError("invalid base64 value")
    padding = "=" * (-len(value) % 4)
    try:
        return base64.b64decode(value + padding, altchars=b"-_", validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise ValueError("invalid base64 value") from exc


def _password_record(config):
    """Return a validated (salt, verifier) pair, or None for malformed data."""
    record = config.get(LAN_PASSWORD_CONFIG_KEY) if isinstance(config, dict) else None
    if not isinstance(record, dict) or set(record) != {"salt", "verifier"}:
        return None
    try:
        salt = _b64decode(record["salt"])
        verifier = _b64decode(record["verifier"])
    except ValueError:
        return None
    if len(salt) != LAN_PASSWORD_SALT_BYTES or len(verifier) != LAN_PASSWORD_DKLEN:
        return None
    return salt, verifier


def _password_configured(config=None):
    with _AUTH_LOCK:
        return _password_record(APP_CONFIG if config is None else config) is not None


def _clean_lan_password(value):
    if not isinstance(value, str):
        raise ValueError("password must be text")
    if not value.strip():
        raise ValueError("password cannot be blank")
    if any(unicodedata.category(char) in ("Cc", "Cf") for char in value):
        raise ValueError("password cannot contain control characters")
    if len(value) < LAN_PASSWORD_MIN_CHARS:
        raise ValueError(f"password must be at least {LAN_PASSWORD_MIN_CHARS} characters")
    if len(value) > LAN_PASSWORD_MAX_CHARS or len(value.encode("utf-8")) > LAN_PASSWORD_MAX_BYTES:
        raise ValueError(f"password must be {LAN_PASSWORD_MAX_CHARS} characters or fewer")
    return value


def _derive_password_verifier(password, salt):
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt,
        n=LAN_PASSWORD_SCRYPT_N, r=LAN_PASSWORD_SCRYPT_R, p=LAN_PASSWORD_SCRYPT_P,
        maxmem=LAN_PASSWORD_SCRYPT_MAXMEM, dklen=LAN_PASSWORD_DKLEN,
    )


def _clean_user_name(value):
    """Validate a display name before it reaches HTML, prompts, or disk."""
    if not isinstance(value, str):
        raise ValueError("name must be text")
    if any(unicodedata.category(char) in ("Cc", "Cf") for char in value):
        raise ValueError("name must be a single line without control characters")
    name = " ".join(value.split())
    if not name:
        raise ValueError("name cannot be empty")
    if len(name) > MAX_USER_NAME:
        raise ValueError(f"name must be {MAX_USER_NAME} characters or fewer")
    if name.casefold() in AI_KEYS:
        raise ValueError("name must be different from Claude, Codex, and Grok")
    return name


def _load_app_config(path=None):
    path = path or CONFIG_PATH
    try:
        with open(path, encoding="utf-8") as fh:
            config = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(config, dict):
        return {}
    try:
        config["userName"] = _clean_user_name(config.get("userName", DEFAULT_USER_NAME))
    except ValueError:
        config.pop("userName", None)
    if LAN_PASSWORD_CONFIG_KEY in config and _password_record(config) is None:
        config.pop(LAN_PASSWORD_CONFIG_KEY, None)
    return config


def _write_app_config(config, path=None):
    """Atomically replace the local config so interrupted writes cannot corrupt it."""
    path = os.path.abspath(path or CONFIG_PATH)
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, pending = tempfile.mkstemp(prefix=".roundtable-config-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(config, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(pending, path)
    except Exception:
        try:
            os.unlink(pending)
        except OSError:
            pass
        raise


APP_CONFIG = _load_app_config()
_CONFIG_LOCK = threading.Lock()
DISPLAY = {
    "user": APP_CONFIG.get("userName", DEFAULT_USER_NAME),
    "claude": "Claude", "codex": "Codex", "grok": "Grok",
}


def update_user_name(value, path=None):
    """Persist and activate a human display name, leaving memory unchanged on failure."""
    name = _clean_user_name(value)
    with _CONFIG_LOCK:
        updated = dict(APP_CONFIG)
        if name == DEFAULT_USER_NAME:
            updated.pop("userName", None)
        else:
            updated["userName"] = name
        _write_app_config(updated, path)
        APP_CONFIG.clear()
        APP_CONFIG.update(updated)
        DISPLAY["user"] = name
    return name


def configure_lan_password(value, path=None):
    """Persist a new salted verifier. Plaintext never enters config or global state."""
    password = _clean_lan_password(value)
    salt = secrets.token_bytes(LAN_PASSWORD_SALT_BYTES)
    verifier = _derive_password_verifier(password, salt)
    record = {"salt": _b64encode(salt), "verifier": _b64encode(verifier)}
    with _AUTH_LOCK:
        with _CONFIG_LOCK:
            updated = dict(APP_CONFIG)
            updated[LAN_PASSWORD_CONFIG_KEY] = record
            _write_app_config(updated, path)
            APP_CONFIG.clear()
            APP_CONFIG.update(updated)
    return True


def _verify_lan_password(value):
    """Perform the expensive verifier check after the caller applies rate limiting."""
    try:
        password = _clean_lan_password(value)
    except ValueError:
        return False
    with _AUTH_LOCK:
        record = _password_record(APP_CONFIG)
        if record is None:
            return False
        salt, expected = record
        actual = _derive_password_verifier(password, salt)
        return hmac.compare_digest(actual, expected)


def _authenticate_lan_password(value):
    """Verify and issue without allowing a password/key rotation in between."""
    with _AUTH_LOCK:
        if not _verify_lan_password(value):
            return ""
        return _issue_remote_session()


def _rotate_remote_sessions():
    global REMOTE_SESSION_SECRET
    replacement = secrets.token_urlsafe(32)
    with _AUTH_LOCK:
        REMOTE_SESSION_SECRET = replacement
    return replacement


def _issue_remote_session(now=None):
    now = int(time.time() if now is None else now)
    payload = {
        "aud": _SESSION_AUDIENCE,
        "iat": now,
        "exp": now + REMOTE_SESSION_TTL,
        "nonce": secrets.token_urlsafe(18),
    }
    encoded = _b64encode(json.dumps(
        payload, separators=(",", ":"), sort_keys=True,
    ).encode("utf-8"))
    with _AUTH_LOCK:
        key = REMOTE_SESSION_SECRET.encode("utf-8")
    signature = _b64encode(hmac.new(key, encoded.encode("ascii"), hashlib.sha256).digest())
    return encoded + "." + signature


def _valid_remote_session(value, now=None):
    if not isinstance(value, str) or not value or len(value) > REMOTE_SESSION_MAX_TOKEN_BYTES:
        return False
    try:
        encoded, supplied_signature = value.split(".", 1)
        supplied = _b64decode(supplied_signature)
        payload_bytes = _b64decode(encoded)
    except (ValueError, UnicodeError):
        return False
    with _AUTH_LOCK:
        key = REMOTE_SESSION_SECRET.encode("utf-8")
    expected = hmac.new(key, encoded.encode("ascii"), hashlib.sha256).digest()
    if not hmac.compare_digest(supplied, expected):
        return False
    try:
        payload = json.loads(payload_bytes)
        issued = int(payload["iat"])
        expires = int(payload["exp"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False
    current = int(time.time() if now is None else now)
    return (
        payload.get("aud") == _SESSION_AUDIENCE
        and isinstance(payload.get("nonce"), str)
        and bool(payload["nonce"])
        and issued <= current + 60
        and issued < expires <= issued + REMOTE_SESSION_TTL
        and current < expires
    )


def _login_rate_limit(client, now=None):
    """Record one direct-client login attempt; return retry seconds or zero."""
    current = time.monotonic() if now is None else float(now)
    cutoff = current - LOGIN_RATE_WINDOW
    with _LOGIN_RATE_LOCK:
        for key, recorded in list(_LOGIN_ATTEMPTS.items()):
            active = [stamp for stamp in recorded if stamp > cutoff]
            if active:
                _LOGIN_ATTEMPTS[key] = active
            else:
                _LOGIN_ATTEMPTS.pop(key, None)
        _LOGIN_GLOBAL_ATTEMPTS[:] = [
            stamp for stamp in _LOGIN_GLOBAL_ATTEMPTS if stamp > cutoff
        ]
        attempts = _LOGIN_ATTEMPTS.get(client, [])
        if len(attempts) >= LOGIN_RATE_MAX_ATTEMPTS:
            return max(1, int(LOGIN_RATE_WINDOW - (current - attempts[0]) + 0.999))
        if len(_LOGIN_GLOBAL_ATTEMPTS) >= LOGIN_RATE_GLOBAL_MAX_ATTEMPTS:
            return max(1, int(
                LOGIN_RATE_WINDOW - (current - _LOGIN_GLOBAL_ATTEMPTS[0]) + 0.999
            ))
        attempts.append(current)  # reserve the attempt before the expensive scrypt call
        _LOGIN_ATTEMPTS[client] = attempts
        _LOGIN_GLOBAL_ATTEMPTS.append(current)
    return 0


def _clear_login_rate_limit(client):
    with _LOGIN_RATE_LOCK:
        _LOGIN_ATTEMPTS.pop(client, None)

# Conversations are stored as JSON files on disk so they survive a browser clear.
CONV_DIR = _absolute_user_path(os.environ.get("ROUNDTABLE_DATA", os.path.join(HERE, "conversations")))
os.makedirs(CONV_DIR, exist_ok=True)
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _conv_path(cid):
    return os.path.join(CONV_DIR, cid + ".json")


def derive_title(messages):
    for m in messages:
        if (m.get("name") or "").lower() not in AI_KEYS:
            t = " ".join((m.get("text") or "").split())
            if t:
                return t[:60]
    return "New conversation"


def load_conv(cid):
    if not _ID_RE.match(cid or ""):
        return None
    try:
        with open(_conv_path(cid), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def save_conv(doc):
    with open(_conv_path(doc["id"]), "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, ensure_ascii=False)


def list_convs():
    out = []
    for name in os.listdir(CONV_DIR):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(CONV_DIR, name), encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        out.append({
            "id": d.get("id"), "title": d.get("title") or "Untitled",
            "updatedAt": d.get("updatedAt", 0), "createdAt": d.get("createdAt", 0),
            "count": len(d.get("messages", [])), "pinned": bool(d.get("pinned", False)),
        })
    out.sort(key=lambda x: (0 if x["pinned"] else 1, -x["updatedAt"]))  # pinned first, then recent
    return out


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------
def _normalize_agent_keys(value, field="activeAgents"):
    """Validate and de-duplicate an ordered list of assistant keys."""
    if value is None:
        return list(AI_KEYS)
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field} must be a list")
    result = []
    for item in value:
        if not isinstance(item, str) or item.lower() not in AI_KEYS:
            raise ValueError(f"{field} contains an unknown agent")
        key = item.lower()
        if key not in result:
            result.append(key)
    return result


def _join_names(names):
    if len(names) < 2:
        return "".join(names)
    if len(names) == 2:
        return " and ".join(names)
    return ", ".join(names[:-1]) + ", and " + names[-1]


def build_prompt(self_key, transcript, write=False, active_agents=None):
    """Render the transcript and current turn's exact roster for `self_key`."""
    if self_key not in AI_KEYS:
        raise ValueError(f"unknown agent {self_key}")
    active_keys = _normalize_agent_keys(active_agents)
    if self_key not in active_keys:
        raise ValueError(f"{self_key} must be included in activeAgents")

    self_name = DISPLAY[self_key]
    user_name = DISPLAY["user"]
    active_peers = [DISPLAY[k] for k in active_keys if k != self_key]
    inactive_keys = [k for k in AI_KEYS if k not in active_keys]

    status_lines = [f"- {user_name} — the human participant."]
    for key in AI_KEYS:
        if key in active_keys:
            status = ("participating in this round (selected or directly addressed); "
                      "may already have replied or will reply when scheduled.")
        else:
            status = "not participating in this round; will not reply."
        status_lines.append(f"- {DISPLAY[key]} — an AI assistant; {status}")
    participant_status = "\n".join(status_lines)

    collaborators = [user_name] + active_peers
    collaboration = _join_names(collaborators)
    inactive_note = ""
    if inactive_keys:
        inactive_names = _join_names([DISPLAY[k] for k in inactive_keys])
        inactive_note = (
            f"\n- Do not address {inactive_names}, ask them questions, wait for them, or comment on "
            "their silence; they are not participating in this turn. If they appear in the "
            "transcript, treat those earlier messages as context only."
        )

    lines = []
    for msg in transcript:
        key = (msg.get("name") or "").lower()
        name = DISPLAY[key] if key in AI_KEYS else user_name
        text = (msg.get("text") or "").strip()
        lines.append(f"{name}: {text}")
    convo = "\n\n".join(lines) if lines else "(no messages yet)"

    access = ("- You have full read/write access to the working directory: you can read and edit "
              "files and run commands (git, tests, builds) when the conversation calls for it."
              if write else
              "- You may read files in the working directory to inform your answer, but you are "
              "read-only — you cannot modify files or run commands that change anything.")

    return f"""You are {self_name}, an AI assistant in a live collaborative brainstorm \
happening inside a shared chat app.

Participant status for this turn:
{participant_status}

Every active assistant receives the shared transcript before replying. Collaborate with \
{collaboration}: build on what they said, agree or disagree with specific reasoning, ask \
direct questions, and raise follow-ups. Address people by name when it helps move the idea forward.

Rules:
- Reply ONLY as {self_name}. Never write lines for anyone else.
- Do NOT prefix your message with your own name — the app already labels it.
- Be conversational and concise by default; go deep only when the topic warrants it. \
A single sentence is a fine reply when that's all that's needed.{inactive_note}
{access}

Conversation so far:
----------------------------------------
{convo}
----------------------------------------

Write {self_name}'s next message now."""


# ---------------------------------------------------------------------------
# Agent process helpers
# ---------------------------------------------------------------------------
WINDOWS_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
_PIPE_EOF = object()
_ACTIVE_PROCESSES = set()
_ACTIVE_PROCESS_LOCK = threading.Lock()


def _is_windows():
    return os.name == "nt"


def _platform_key():
    if _is_windows():
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux" if sys.platform.startswith("linux") else sys.platform


class DirectoryPickerUnavailable(RuntimeError):
    pass


class DirectoryPickerBusy(RuntimeError):
    pass


_PICKER_LOCK = threading.Lock()
_PICKER_INITIAL_ENV = "AICONVO_PICKER_INITIAL"
_TK_PICKER_SCRIPT = r"""
import os
import sys

try:
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
    except tk.TclError:
        pass
    root.update_idletasks()
    selected = filedialog.askdirectory(
        parent=root,
        initialdir=sys.argv[1],
        title="Choose the AI Roundtable workspace folder",
        mustexist=True,
    )
    root.destroy()
    sys.stdout.buffer.write(os.fsencode(selected))
except Exception as exc:
    sys.stderr.write(str(exc))
    raise SystemExit(2)
""".strip()


def _picker_initial_directory(value):
    """Use the nearest existing parent so pickers always open somewhere useful."""
    try:
        path = _absolute_user_path(value.strip()) if value and value.strip() else ""
    except (OSError, ValueError):
        path = ""
    while path and not os.path.isdir(path):
        parent = os.path.dirname(path)
        if parent == path:
            path = ""
            break
        path = parent
    for candidate in (path, AGENT_CWD, os.getcwd()):
        if candidate and os.path.isdir(candidate):
            return os.path.abspath(candidate)
    return os.path.abspath(os.curdir)


def _directory_picker_candidates(initial):
    """Yield native picker commands without passing user paths through a shell."""
    platform = _platform_key()
    if platform == "windows":
        powershell = _windows_powershell_path()
        if powershell:
            script = (
                "Add-Type -AssemblyName System.Windows.Forms; "
                "$dialog = New-Object System.Windows.Forms.FolderBrowserDialog; "
                "$dialog.Description = 'Choose the AI Roundtable workspace folder'; "
                "$dialog.ShowNewFolderButton = $true; "
                f"$initial = [Environment]::GetEnvironmentVariable('{_PICKER_INITIAL_ENV}'); "
                "if ($initial -and [IO.Directory]::Exists($initial)) { "
                "$dialog.SelectedPath = $initial }; "
                "if ($dialog.ShowDialog() -eq [Windows.Forms.DialogResult]::OK) { "
                "$utf8 = [System.Text.UTF8Encoding]::new($false); "
                "[Console]::OutputEncoding = $utf8; "
                "[Console]::Write($dialog.SelectedPath) }"
            )
            yield "Windows folder dialog", [
                powershell, "-NoLogo", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass",
                "-Command", script,
            ], {_PICKER_INITIAL_ENV: initial}
    elif platform == "macos":
        osascript = "/usr/bin/osascript"
        if os.path.isfile(osascript):
            script = (
                "on run argv\n"
                "try\n"
                "set startFolder to POSIX file (item 1 of argv)\n"
                "set picked to choose folder with prompt "
                '"Choose the AI Roundtable workspace folder" default location startFolder\n'
                "return POSIX path of picked\n"
                "on error number -128\n"
                'return ""\n'
                "end try\n"
                "end run"
            )
            yield "macOS folder dialog", [osascript, "-e", script, initial], {}
    else:
        zenity = shutil.which("zenity")
        if zenity:
            start = initial if initial.endswith(os.sep) else initial + os.sep
            yield "Zenity folder dialog", [
                zenity, "--file-selection", "--directory",
                "--title=Choose the AI Roundtable workspace folder",
                "--filename=" + start,
            ], {}
        kdialog = shutil.which("kdialog")
        if kdialog:
            yield "KDialog folder dialog", [
                kdialog, "--getexistingdirectory", initial,
                "--title", "Choose the AI Roundtable workspace folder",
            ], {}

    # Tk is deliberately isolated in a child process: GUI toolkits should not be
    # initialized inside ThreadingHTTPServer request threads.
    if sys.executable:
        yield "Tk folder dialog", [sys.executable, "-c", _TK_PICKER_SCRIPT, initial], {}


def pick_directory(value=""):
    """Open one host-native folder dialog; return None when the user cancels."""
    if not _PICKER_LOCK.acquire(blocking=False):
        raise DirectoryPickerBusy("a folder picker is already open")
    try:
        initial = _picker_initial_directory(value)
        failures = []
        for label, command, environment_updates in _directory_picker_candidates(initial):
            environment = dict(os.environ)
            environment.pop(REMOTE_TOKEN_ENV, None)
            environment.pop(REMOTE_TOKEN_FILE_ENV, None)
            environment.update(environment_updates)
            try:
                completed = subprocess.run(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=environment,
                    check=False,
                )
            except OSError as exc:
                failures.append(f"{label}: {exc}")
                continue

            stdout = completed.stdout.decode("utf-8", "replace").rstrip("\r\n")
            stderr = completed.stderr.decode("utf-8", "replace").strip()
            if completed.returncode:
                # Native dialogs normally use a blank, non-zero result for Cancel.
                cancelled = (
                    "User canceled" in stderr
                    or "(-128)" in stderr
                    or (completed.returncode == 1 and label in (
                        "Zenity folder dialog", "KDialog folder dialog"
                    ))
                )
                if cancelled:
                    return None
                detail = stderr[-240:] or f"exited with status {completed.returncode}"
                failures.append(f"{label}: {detail}")
                continue
            if not stdout:
                return None
            try:
                selected = _absolute_user_path(stdout)
            except (OSError, ValueError) as exc:
                raise DirectoryPickerUnavailable(f"folder picker returned an invalid path: {exc}")
            if not os.path.isdir(selected):
                raise DirectoryPickerUnavailable("folder picker returned a folder that does not exist")
            return selected

        detail = "; ".join(failures[-2:]) if failures else "no GUI picker is installed"
        raise DirectoryPickerUnavailable(detail)
    finally:
        _PICKER_LOCK.release()


def _windows_native_cli_candidates(name):
    """Known locations used by each provider's official Windows installer."""
    user = os.path.expanduser("~")
    local = os.environ.get("LOCALAPPDATA", "")
    if name == "claude":
        return [os.path.join(user, ".local", "bin", "claude.exe")]
    if name == "codex":
        install = os.environ.get("CODEX_INSTALL_DIR", "")
        return [
            os.path.join(install, "codex.exe") if install else "",
            os.path.join(install, "bin", "codex.exe") if install else "",
            os.path.join(local, "Programs", "OpenAI", "Codex", "bin", "codex.exe") if local else "",
        ]
    if name == "grok":
        return [os.path.join(GROK_HOME, "bin", "grok.exe")]
    return []


def _windows_powershell_path():
    """Find a real PowerShell executable for safe npm .ps1 shim launches."""
    root = os.environ.get("SystemRoot", r"C:\Windows")
    bundled = os.path.join(
        root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe"
    )
    if os.path.isfile(bundled):
        return os.path.abspath(bundled)
    found = shutil.which("pwsh.exe") or shutil.which("powershell.exe")
    return os.path.abspath(found) if found else None


def _safe_windows_shim(path):
    """Return whether a Windows script launcher has a direct, non-cmd path."""
    extension = os.path.splitext(path)[1].lower()
    if extension == ".cmd":
        companion = os.path.splitext(path)[0] + ".ps1"
        return bool(
            _windows_powershell_path()
            and os.path.isfile(companion)
            and os.path.isfile(WINDOWS_SHIM_BRIDGE)
        )
    return extension not in (".bat", ".cmd")


def _cli_path(name):
    """Resolve exactly what will be launched, preferring native Windows binaries."""
    if _is_windows():
        found = shutil.which(name + ".exe")
        if found:
            return os.path.abspath(found)
        for candidate in _windows_native_cli_candidates(name):
            if candidate and os.path.isfile(candidate):
                return os.path.abspath(candidate)
    found = shutil.which(name)
    if not found:
        return None
    found = os.path.abspath(found)
    if _is_windows() and not _safe_windows_shim(found):
        return None
    return found


def _resolve_cli_command(command):
    command = list(command)
    resolved = _cli_path(command[0])
    if not resolved:
        if _is_windows():
            raise FileNotFoundError(
                f"{command[0]} has no native executable or safe PowerShell shim; "
                "install the provider's official Windows executable"
            )
        return command, {}
    command[0] = resolved

    # npm creates a same-stem .ps1 beside each .cmd shim. A fixed local bridge
    # forces UTF-8 and invokes that script with direct argv, without giving
    # cmd.exe a chance to reinterpret &, %, !, carets, parentheses, or paths.
    if _is_windows() and os.path.splitext(resolved)[1].lower() == ".cmd":
        companion = os.path.splitext(resolved)[0] + ".ps1"
        powershell = _windows_powershell_path()
        if powershell and os.path.isfile(companion):
            # Windows PowerShell 5.1 cannot faithfully forward empty arguments
            # or embedded quotes from a script to a native executable. This app
            # never generates either (Windows paths cannot contain quotes and
            # optional CLI values are omitted), so reject them explicitly.
            for value in command[1:]:
                value = str(value)
                if not value or any(char in value for char in ('"', "\r", "\n", "\0")):
                    raise ValueError(
                        "Windows npm-shim arguments cannot be empty or contain quotes/control characters"
                    )
            return [
                powershell, "-NoLogo", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-File", WINDOWS_SHIM_BRIDGE,
            ], {
                WINDOWS_SHIM_PATH_ENV: companion,
                WINDOWS_SHIM_ARGS_ENV: json.dumps(command[1:], ensure_ascii=False),
            }
        raise OSError(
            f"{resolved} is a batch launcher without a safe PowerShell companion; "
            "install the provider's official Windows executable"
        )
    return command, {}


def _popen_platform_kwargs():
    if _is_windows():
        return {"creationflags": WINDOWS_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _spawn_cli(command, **kwargs):
    command, environment_updates = _resolve_cli_command(command)
    options = _popen_platform_kwargs()
    options.update(kwargs)
    requested_env = options.get("env")
    launch_env = dict(os.environ if requested_env is None else requested_env)
    launch_env.pop(REMOTE_TOKEN_ENV, None)  # never expose the LAN bearer key to an agent
    launch_env.pop(REMOTE_TOKEN_FILE_ENV, None)
    launch_env.update(environment_updates)
    options["env"] = launch_env
    return subprocess.Popen(command, **options)


def _register_process(proc):
    with _ACTIVE_PROCESS_LOCK:
        _ACTIVE_PROCESSES.add(proc)


def _unregister_process(proc):
    with _ACTIVE_PROCESS_LOCK:
        _ACTIVE_PROCESSES.discard(proc)


def _taskkill_path():
    root = os.environ.get("SystemRoot", r"C:\Windows")
    native = os.path.join(root, "System32", "taskkill.exe")
    return native if os.path.isfile(native) else (shutil.which("taskkill.exe") or "taskkill.exe")


def _kill(proc):
    """Stop a provider tree; Windows falls back to the parent if taskkill is denied."""
    if proc.poll() is not None:
        return
    if _is_windows():
        try:
            result = subprocess.run(
                [_taskkill_path(), "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False,
            )
            if result.returncode and proc.poll() is None:
                proc.terminate()
        except (OSError, subprocess.SubprocessError):
            try:
                proc.terminate()
            except Exception:
                pass
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
    try:
        proc.wait(timeout=3)
        return
    except Exception:
        pass
    if not _is_windows():
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
        except Exception:
            pass
    try:
        proc.kill()
    except Exception:
        pass


def _kill_all_processes():
    with _ACTIVE_PROCESS_LOCK:
        active = list(_ACTIVE_PROCESSES)
    for proc in active:
        _kill(proc)


def _run_cli_capture(command, input_data, timeout):
    """Capture a short CLI control request without leaking children on timeout."""
    proc = _spawn_cli(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    _register_process(proc)
    try:
        try:
            stdout, _ = proc.communicate(input=input_data, timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill(proc)
            raise
        return subprocess.CompletedProcess(command, proc.returncode, stdout=stdout, stderr=None)
    finally:
        _unregister_process(proc)


def _pump_output_lines(pipe, output_queue):
    """Read a process pipe on a worker thread; Windows selectors only support sockets."""
    try:
        while True:
            line = pipe.readline()
            if not line:
                break
            output_queue.put(line)
    except (OSError, ValueError):
        pass
    finally:
        output_queue.put(_PIPE_EOF)


def _start_output_reader(pipe):
    output_queue = queue.Queue()
    reader = threading.Thread(
        target=_pump_output_lines, args=(pipe, output_queue),
        name="roundtable-cli-output", daemon=True,
    )
    reader.start()
    return output_queue, reader


# Numeric effort ladder, lowest -> highest. Some Codex models also expose
# "ultra". Claude's special "ultracode" workflow mode is handled separately.
EFFORT_LADDER = ["minimal", "low", "medium", "high", "xhigh", "max", "ultra"]
CLAUDE_MODELS = [
    {"slug": "claude-opus-4-8", "name": "Opus 4.8 (deep)"},
    {"slug": "claude-fable-5", "name": "Fable 5"},
    {"slug": "claude-sonnet-5", "name": "Sonnet 5 (fast)"},
    {"slug": "claude-haiku-4-5-20251001", "name": "Haiku 4.5 (quick)"},
]
_CLAUDE_FULL_LEVELS = ["low", "medium", "high", "xhigh", "max", "ultracode"]
_CLAUDE_FALLBACK_LEVELS = {
    "claude-opus-4-8": _CLAUDE_FULL_LEVELS,
    "claude-fable-5": _CLAUDE_FULL_LEVELS,
    "claude-sonnet-5": _CLAUDE_FULL_LEVELS,
    "claude-haiku-4-5-20251001": [],
}


def _load_claude_model_levels():
    """Ask Claude Code's headless SDK for model capabilities without a model turn.

    The initialize control response also contains account data; only the five
    non-sensitive model capability fields below are retained.
    """
    fallback = {slug: list(values) for slug, values in _CLAUDE_FALLBACK_LEVELS.items()}
    request = {"type": "control_request", "request_id": "effort-catalog",
               "request": {"subtype": "initialize"}}
    cmd = ["claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json",
           "--verbose", "--no-session-persistence"]
    try:
        proc = _run_cli_capture(cmd, (json.dumps(request) + "\n").encode(), timeout=5)
    except (OSError, subprocess.SubprocessError):
        return fallback

    discovered = {}
    ultracode_available = False
    for raw in proc.stdout.decode("utf-8", "replace").splitlines():
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "control_response":
            continue
        response = ((obj.get("response") or {}).get("response") or {})
        ultracode_available = any(
            c.get("name") == "effort" and "ultracode" in (c.get("argumentHint") or "")
            for c in response.get("commands", []) if isinstance(c, dict)
        )
        for model in response.get("models", []):
            if not isinstance(model, dict):
                continue
            values = list(model.get("supportedEffortLevels") or []) if model.get("supportsEffort") else []
            resolved = re.sub(r"\[[^]]+\]$", "", str(model.get("resolvedModel") or ""))
            alias = re.sub(r"\[[^]]+\]$", "", str(model.get("value") or ""))
            if resolved:
                discovered[resolved] = values
            if alias and alias != "default":
                discovered[alias] = values

    if ultracode_available:
        for values in discovered.values():
            if "xhigh" in values and "ultracode" not in values:
                values.append("ultracode")
    fallback.update(discovered)
    return fallback


CLAUDE_MODEL_LEVELS = _load_claude_model_levels()


def _load_codex_models():
    """Read Codex's cache: models, supported levels, and per-model defaults."""
    models, levels, defaults = [], {}, {}
    try:
        with open(os.path.join(CODEX_HOME, "models_cache.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        raw_models = data.get("models", []) if isinstance(data, dict) else []
        for m in raw_models if isinstance(raw_models, list) else []:
            if not isinstance(m, dict):
                continue
            slug = m.get("slug")
            if not slug:
                continue
            models.append({"slug": slug, "name": m.get("display_name", slug)})
            raw_levels = m.get("supported_reasoning_levels", [])
            supported = [x.get("effort") for x in raw_levels if isinstance(x, dict) and x.get("effort")]
            levels[slug] = supported
            default = m.get("default_reasoning_level", "")
            defaults[slug] = default if default in supported else ""
    except (OSError, json.JSONDecodeError, KeyError):
        pass
    return models, levels, defaults


def _load_codex_config():
    try:
        import tomllib
        with open(os.path.join(CODEX_HOME, "config.toml"), "rb") as fh:
            data = tomllib.load(fh)
        return data.get("model", ""), data.get("model_reasoning_effort", "")
    except Exception:
        return "", ""


def _load_grok_models():
    """Read Grok Build's model cache: [{slug,name}], {slug: [levels]}."""
    models, levels = [], {}
    try:
        with open(os.path.join(GROK_HOME, "models_cache.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        raw_models = data.get("models", {})
        entries = raw_models.items() if isinstance(raw_models, dict) else []
        for slug, entry in entries:
            info = entry.get("info", {}) if isinstance(entry, dict) else {}
            if not slug:
                continue
            models.append({"slug": slug, "name": info.get("name") or slug})
            efforts = info.get("reasoning_efforts", [])
            levels[slug] = [x.get("value") or x.get("id") for x in efforts
                            if isinstance(x, dict) and (x.get("value") or x.get("id"))]
    except (OSError, json.JSONDecodeError, KeyError):
        pass
    if not models:
        models = [{"slug": DEFAULT_GROK_MODEL, "name": "Grok 4.5"}]
    levels.setdefault(DEFAULT_GROK_MODEL, ["low", "medium", "high"])
    return models, levels


CODEX_MODELS, CODEX_MODEL_LEVELS, CODEX_MODEL_DEFAULTS = _load_codex_models()
CODEX_CONFIG_MODEL, CODEX_CONFIG_EFFORT = _load_codex_config()
GROK_MODELS, GROK_MODEL_LEVELS = _load_grok_models()


def clamp_effort(requested, supported):
    """Clamp a requested effort down to the nearest level the engine supports."""
    r = (requested or "").lower()
    if not r:
        return ""                       # "default": omit, let the CLI decide
    if r in supported:
        return r
    if r == "ultracode":
        return ""                       # special workflow mode; never approximate it
    if r not in EFFORT_LADDER:
        return ""
    ri = EFFORT_LADDER.index(r)
    known = [s for s in supported if s in EFFORT_LADDER]
    lower = [s for s in known if EFFORT_LADDER.index(s) <= ri]
    if lower:
        return max(lower, key=lambda s: EFFORT_LADDER.index(s))
    return min(known, key=lambda s: EFFORT_LADDER.index(s)) if known else ""


def _codex_levels(model):
    slug = model or DEFAULT_CODEX_MODEL or CODEX_CONFIG_MODEL
    return CODEX_MODEL_LEVELS.get(slug, [])


def _codex_effort(model, requested):
    """Resolve even "default" through the selected model's capability list.

    Codex otherwise inherits the global config effort unchanged when `-m`
    selects another model. For example, an `ultra` default becomes `max`
    inside the CLI for Mini, even though Mini only supports through `xhigh`.
    """
    slug = model or DEFAULT_CODEX_MODEL or CODEX_CONFIG_MODEL
    if requested:
        candidate = requested
    elif model or DEFAULT_CODEX_MODEL:
        # "default" follows the selected model, not another model's global
        # config. Mini, for example, advertises medium as its native default.
        candidate = CODEX_MODEL_DEFAULTS.get(slug) or CODEX_CONFIG_EFFORT
    else:
        candidate = CODEX_CONFIG_EFFORT or CODEX_MODEL_DEFAULTS.get(slug, "")
    resolved = clamp_effort(candidate, _codex_levels(model))
    if candidate and not resolved:
        # Omitting an invalid inherited value would make Codex read that same
        # bad value from config again, so fall back to the model's own default.
        resolved = clamp_effort(CODEX_MODEL_DEFAULTS.get(slug, ""), _codex_levels(model))
    return resolved


def _claude_levels(model):
    return CLAUDE_MODEL_LEVELS.get(model or DEFAULT_CLAUDE_MODEL, [])


def _grok_levels(model):
    slug = model or DEFAULT_GROK_MODEL
    return (GROK_MODEL_LEVELS[slug] if slug in GROK_MODEL_LEVELS
            else ["low", "medium", "high"])


def build_cmd(agent, model, effort="", workdir=None, write=False, prompt_file=None):
    wd = workdir or AGENT_CWD
    if agent == "claude":
        cmd = ["claude", "-p", "--output-format", "stream-json",
               "--include-partial-messages", "--verbose",
               "--add-dir", wd,                       # let Claude read the target project
               # write mode = full agent (edits + Bash/git); off = read-only, no commands
               "--permission-mode", ("bypassPermissions" if write else
                                     ("plan" if _is_windows() else "default")),
               "--model", model or DEFAULT_CLAUDE_MODEL]
        ce = clamp_effort(effort, _claude_levels(model))
        if ce:
            cmd += ["--effort", ce]
        return cmd
    if agent == "codex":
        cmd = ["codex", "exec", "--json", "--skip-git-repo-check", "--color", "never", "-C", wd]
        # write mode = full access (no sandbox, no approvals), matching Claude's bypassPermissions
        cmd += ["--dangerously-bypass-approvals-and-sandbox"] if write else ["--sandbox", "read-only"]
        m = model or DEFAULT_CODEX_MODEL or CODEX_CONFIG_MODEL
        if m:
            cmd += ["-m", m]
        ce = _codex_effort(model, effort)
        if ce:
            cmd += ["-c", f"model_reasoning_effort={ce}"]
        cmd.append("-")  # explicit stdin prompt; supported on every Codex platform
        return cmd
    if agent == "grok":
        if not prompt_file:
            raise ValueError("grok requires a prompt file")
        cmd = ["grok", "--prompt-file", os.path.abspath(prompt_file),
               "--output-format", "streaming-json",
               "--cwd", wd, "--model", model or DEFAULT_GROK_MODEL,
               "--no-memory", "--no-subagents", "--no-plan"]
        # Grok enforces these sandbox modes on supported hosts. Native Windows
        # currently has provider permissions/instructions but no Grok OS sandbox.
        cmd += (["--sandbox", "workspace", "--permission-mode", "bypassPermissions"]
                if write else ["--sandbox", "read-only", "--permission-mode", "dontAsk"])
        ge = clamp_effort(effort, _grok_levels(model))
        if ge:
            cmd += ["--reasoning-effort", ge]
        return cmd
    raise ValueError(f"unknown agent {agent}")


@contextlib.contextmanager
def _prepare_invocation(agent, prompt, model, effort, workdir, write):
    """Prepare portable argv/stdin and own Grok's temporary prompt file."""
    prompt_path = None
    try:
        if agent == "grok":
            fd, prompt_path = tempfile.mkstemp(prefix="ai-roundtable-grok-", suffix=".txt")
            with os.fdopen(fd, "wb") as fh:
                fh.write(prompt.encode("utf-8"))
            command = build_cmd(agent, model, effort, workdir, write, prompt_file=prompt_path)
            yield command, None
        else:
            yield build_cmd(agent, model, effort, workdir, write), prompt.encode("utf-8")
    finally:
        if prompt_path:
            _safe_unlink(prompt_path)


def _short(s, n=160):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _tool_line(name, inp):
    """One terminal-style line describing a Claude tool call."""
    inp = inp if isinstance(inp, dict) else {}
    if name == "Bash":
        return "$ " + _short(inp.get("command", ""), 200)
    if name in ("Read", "Write"):
        return f"{name} {inp.get('file_path', '')}"
    if name in ("Edit", "MultiEdit"):
        return f"Edit {inp.get('file_path', '')}"
    if name == "NotebookEdit":
        return f"Edit {inp.get('notebook_path', '')}"
    if name == "Grep":
        where = inp.get("path") or inp.get("glob") or ""
        return f"Grep /{_short(inp.get('pattern', ''), 80)}/" + (f" in {where}" if where else "")
    if name == "Glob":
        return f"Glob {inp.get('pattern', '')}"
    if name == "WebSearch":
        return "Web search: " + _short(inp.get("query", ""), 120)
    if name == "WebFetch":
        return "Fetch " + _short(inp.get("url", ""), 160)
    if name in ("Task", "Agent"):
        return "Subagent: " + _short(inp.get("description") or inp.get("prompt") or "", 120)
    if name == "TodoWrite":
        return "Update todo list"
    args = _short(json.dumps(inp), 120) if inp else ""
    return (name + " " + args).strip()


def _preview(content, n=120):
    """First line of a tool result / command output, truncated."""
    if isinstance(content, list):
        content = "\n".join(b.get("text", "") for b in content
                            if isinstance(b, dict) and b.get("type") == "text")
    text = str(content or "").strip()
    if not text:
        return ""
    lines = text.splitlines()
    out = _short(lines[0], n)
    if len(lines) > 1:
        out += f"  (+{len(lines) - 1} lines)"
    return out


def parse_event(agent, obj, state):
    """Map one CLI JSON event -> list of client events:
    {type:'delta'} reply text · {type:'activity'} live tool/thinking feed · {type:'error'}."""
    out = []
    if agent == "claude":
        tp = obj.get("type")
        if tp == "stream_event":
            ev = obj.get("event", {})
            if ev.get("type") == "content_block_delta":
                d = ev.get("delta", {})
                if d.get("type") == "text_delta":
                    t = d.get("text", "")
                    if t:
                        state["full"] += t
                        out.append({"type": "delta", "text": t})
                elif d.get("type") == "thinking_delta":
                    t = d.get("thinking", "")
                    if t:
                        out.append({"type": "activity", "kind": "thinking", "text": t})
        elif tp == "assistant":
            for blk in (obj.get("message") or {}).get("content") or []:
                if isinstance(blk, dict) and blk.get("type") == "tool_use":
                    bid = blk.get("id")
                    if bid in state["tools"]:
                        continue                      # message re-emitted; already shown
                    state["tools"][bid] = blk.get("name", "")
                    out.append({"type": "activity", "kind": "tool",
                                "text": _tool_line(blk.get("name", ""), blk.get("input"))})
        elif tp == "user":
            content = (obj.get("message") or {}).get("content")
            for blk in content if isinstance(content, list) else []:
                if isinstance(blk, dict) and blk.get("type") == "tool_result":
                    name = state["tools"].get(blk.get("tool_use_id"), "")
                    err = bool(blk.get("is_error"))
                    p = _preview(blk.get("content"))
                    line = (("✗ " if err else "→ ") + (name + ": " if name else "")
                            + (p or ("failed" if err else "done")))
                    out.append({"type": "activity", "kind": "err" if err else "result", "text": line})
        elif tp == "result" and obj.get("is_error"):
            out.append({"type": "error", "text": "(Claude error) " + str(obj.get("result"))[:800]})
    elif agent == "codex":  # reply text arrives in one agent_message; the rest feeds activity
        tp = obj.get("type")
        it = obj.get("item") if isinstance(obj.get("item"), dict) else {}
        itype = it.get("type")
        if tp == "item.started" and itype == "command_execution":
            state["seen"].add(it.get("id"))
            out.append({"type": "activity", "kind": "tool",
                        "text": "$ " + _short(it.get("command", ""), 200)})
        elif tp == "item.completed":
            if itype == "agent_message":
                t = it.get("text", "") or ""
                if t:
                    sep = "\n\n" if state["full"] else ""
                    state["full"] += sep + t
                    out.append({"type": "delta", "text": sep + t})
            elif itype == "reasoning":
                t = (it.get("text") or "").strip()
                if t:
                    out.append({"type": "activity", "kind": "thinking", "text": t, "block": True})
            elif itype == "command_execution":
                if it.get("id") not in state["seen"]:   # some versions skip item.started
                    out.append({"type": "activity", "kind": "tool",
                                "text": "$ " + _short(it.get("command", ""), 200)})
                code = it.get("exit_code")
                ok = code in (0, None)
                line = "→ exit 0" if code == 0 else ("→ done" if code is None else f"✗ exit {code}")
                p = _preview(it.get("aggregated_output"))
                if p:
                    line += "  " + p
                out.append({"type": "activity", "kind": "result" if ok else "err", "text": line})
            elif itype == "file_change":
                changes = [c for c in (it.get("changes") or []) if isinstance(c, dict)]
                paths = ", ".join(c.get("path", "") for c in changes[:3])
                out.append({"type": "activity", "kind": "tool",
                            "text": "Edited " + paths + ("…" if len(changes) > 3 else "")})
            elif itype == "web_search":
                out.append({"type": "activity", "kind": "tool",
                            "text": "Web search: " + _short(it.get("query", ""), 120)})
            elif itype == "mcp_tool_call":
                out.append({"type": "activity", "kind": "tool",
                            "text": f"MCP {it.get('server', '')}.{it.get('tool', '')}"})
        elif tp in ("error", "turn.failed"):
            msg = obj.get("message") or (obj.get("error") or {}).get("message") or "error"
            state["errored"] = True
            key = str(msg)
            errors = state.setdefault("errors", set())
            if key not in errors:             # Codex emits the same failure as error + turn.failed
                errors.add(key)
                out.append({"type": "error", "text": "(Codex) " + key[:600]})
    else:  # Grok Build — thought/text arrive as token-sized NDJSON fragments
        tp = obj.get("type")
        if tp == "text":
            t = obj.get("data", "") or ""
            if t:
                state["full"] += t
                out.append({"type": "delta", "text": t})
        elif tp == "thought":
            t = obj.get("data", "") or ""
            if t:
                out.append({"type": "activity", "kind": "thinking", "text": t})
        elif tp in ("error", "max_turns_reached"):
            msg = obj.get("message") or obj.get("data") or "maximum turns reached"
            state["errored"] = True
            out.append({"type": "error", "text": "(Grok) " + str(msg)[:600]})
    return out


def _safe_unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _restart_argv():
    return [sys.executable, os.path.join(HERE, "server.py")] + sys.argv[1:]


def _write_remote_token_handoff(token):
    """Store a session signing key in a 0600 file consumed by the replacement."""
    fd, path = tempfile.mkstemp(prefix=_REMOTE_TOKEN_FILE_PREFIX, suffix=".key")
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token)
            handle.flush()
            os.fsync(handle.fileno())
        return path
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(path)
        except OSError:
            pass
        raise


_CONTROL_OPERATION_LOCK = threading.Lock()


def _prepare_restart(remote_token=None, environment_updates=None):
    """Create the private handoff and an isolated environment before shutdown."""
    with _AUTH_LOCK:
        current_secret = REMOTE_SESSION_SECRET
    next_token = current_secret if remote_token is None else remote_token
    handoff = _write_remote_token_handoff(next_token)
    environment = dict(os.environ)
    environment.pop(REMOTE_TOKEN_ENV, None)
    environment.pop(REMOTE_TOKEN_FILE_ENV, None)
    environment[REMOTE_TOKEN_FILE_ENV] = handoff
    environment.update(environment_updates or {})
    return handoff, environment


def _restart_soon(httpd=None, remote_token=None, prepared=None, environment_updates=None):
    """Re-exec this server after a beat, so the HTTP response is delivered first.
    Same process/port; reloads code and every provider's model/effort catalog."""
    handoff, environment = prepared or _prepare_restart(
        remote_token, environment_updates,
    )
    time.sleep(0.4)
    _kill_all_processes()
    if httpd is not None:
        httpd.shutdown()
        httpd.server_close()
    argv = _restart_argv()
    try:
        os.execve(sys.executable, argv, environment)
    finally:
        _safe_unlink(handoff)


def _restart_worker(httpd, remote_token, prepared):
    """Run one claimed restart and always release the control-operation gate."""
    try:
        _restart_soon(httpd, remote_token, prepared)
    finally:
        _safe_unlink(prepared[0])
        _CONTROL_OPERATION_LOCK.release()


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------
class RoundtableHTTPServer(ThreadingHTTPServer):
    """Bound request concurrency so a LAN peer cannot create unlimited threads."""
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, *args, **kwargs):
        self._request_slots = threading.BoundedSemaphore(32)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._request_slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._request_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, *args):
        pass  # quiet

    def _loopback_client(self):
        try:
            return ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            return False

    def _request_host(self):
        """Return the canonical request Host only when its port reaches this server."""
        raw = self.headers.get("Host", "")
        if not raw or len(raw) > 255 or any(char.isspace() for char in raw) or "@" in raw:
            return ""
        try:
            parsed = urlsplit("//" + raw)
            hostname = parsed.hostname or ""
            request_port = parsed.port
        except ValueError:
            return ""
        expected_port = self.server.server_port
        if request_port is None:
            if expected_port != 80:
                return ""
        elif request_port != expected_port:
            return ""
        try:
            return str(ipaddress.ip_address(hostname))
        except ValueError:
            return "localhost" if hostname.casefold() == "localhost" else ""

    def _socket_destination(self):
        """Return the concrete local IP that accepted this connection."""
        try:
            return str(ipaddress.ip_address(self.connection.getsockname()[0]))
        except (OSError, ValueError):
            return ""

    def _valid_lan_host(self):
        """Tie the Host header to the socket destination and a LAN-scoped peer."""
        host = self._request_host()
        destination = self._socket_destination()
        try:
            peer = str(ipaddress.ip_address(self.client_address[0]))
        except ValueError:
            return False
        return (
            bool(host) and host == destination
            and _is_lan_ipv4(destination) and _is_lan_ipv4(peer)
        )

    def _local_control_client(self):
        """Identify a direct browser on this host, without trusting forwarded loopback."""
        try:
            client = str(ipaddress.ip_address(self.client_address[0]))
        except ValueError:
            return False
        host = self._request_host()
        if self._loopback_client():
            return _host_is_loopback(host) and _host_is_loopback(self._socket_destination())
        destination = self._socket_destination()
        return _is_lan_ipv4(client) and client == destination and host == destination

    def _login_client_key(self):
        try:
            return str(ipaddress.ip_address(self.client_address[0]))
        except ValueError:
            return self.client_address[0][:128]

    def _authorize_request(self):
        """In LAN mode, expose only the strict-Host login surface without a session."""
        if not REMOTE_ACCESS_ENABLED:
            # Binding to loopback is not sufficient protection against DNS
            # rebinding: require a literal loopback Host as well.
            if self._local_control_client():
                return True
            parsed = urlsplit(self.path)
            if parsed.path.startswith("/api/"):
                self._send(403, json.dumps({"error": "open the app through its local URL"}))
            else:
                self._send(403, "Open AI Roundtable through its local URL.", "text/plain; charset=utf-8")
            return False
        parsed = urlsplit(self.path)

        # The server computer keeps control without a LAN session, but only
        # through a literal address belonging to this host.
        if self._local_control_client():
            return True

        # Reject DNS rebinding before serving even the public login shell.
        if not self._valid_lan_host():
            if parsed.path.startswith("/api/"):
                self._send(403, json.dumps({"error": "open the app through its LAN address"}))
            else:
                self._send(403, "Open AI Roundtable through its LAN address.",
                           "text/plain; charset=utf-8")
            return False

        public_request = (
            (self.command == "GET" and parsed.path in (
                "/", "/index.html", "/api/auth/status",
            ))
            or (self.command == "POST" and parsed.path == "/api/auth/login")
        )
        if public_request:
            return True
        if _valid_remote_session(self.headers.get("X-AIConvo-Session", "")):
            return True

        if parsed.path.startswith("/api/"):
            self._send(401, json.dumps({
                "error": "LAN login required", "code": "lan_login_required",
            }))
        else:
            self._send(
                401,
                "AI Roundtable LAN login required.",
                "text/plain; charset=utf-8",
            )
        return False

    def _allow_mutation(self):
        """Require a non-simple header and reject a mismatched browser Origin."""
        if self.headers.get("X-AIConvo-Request") != "1":
            self._send(403, json.dumps({"error": "protected app request required"}))
            return False
        origin = self.headers.get("Origin", "")
        host = self.headers.get("Host", "")
        if not origin:  # permits deliberate non-browser clients with the protected header
            return True
        try:
            parsed = urlsplit(origin)
        except ValueError:
            parsed = None
        if parsed and parsed.scheme == "http" and parsed.netloc == host:
            return True
        self._send(403, json.dumps({"error": "same-origin request required"}))
        return False

    def _json_content_type(self):
        return self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() \
            == "application/json"

    def _send(self, code, body, ctype="application/json", headers=None):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self._send_security_headers()
        for name, value in (headers or {}).items():
            self.send_header(name, str(value))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        self.wfile.flush()

    def _send_security_headers(self):
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")

    def _json_body(self, max_bytes=1024 * 1024):
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length < 0 or length > max_bytes:
                return None
            return json.loads(self.rfile.read(length) or "{}")
        except (OSError, ValueError, json.JSONDecodeError):
            return None

    def _conv_id(self):
        return self.path[len("/api/conversations/"):]

    def do_GET(self):
        if not self._authorize_request():
            return
        request_path = urlsplit(self.path).path
        if request_path in ("/", "/index.html"):
            try:
                with open(os.path.join(HERE, "index.html"), "rb") as fh:
                    self._send(200, fh.read(), "text/html; charset=utf-8")
            except OSError:
                self._send(500, "index.html not found", "text/plain")
        elif request_path == "/api/auth/status":
            self._send(200, json.dumps({"passwordConfigured": _password_configured()}))
        elif request_path == "/api/config":
            self._send(200, json.dumps({
                "userName": DISPLAY["user"],
                "platform": _platform_key(),
                "folderPicker": self._local_control_client(),
                "remoteEnabled": REMOTE_ACCESS_ENABLED,
                "remoteControl": self._local_control_client(),
                "passwordConfigured": _password_configured(),
                "serverInstance": SERVER_INSTANCE_ID,
                "remoteUrls": (_remote_urls(port=self.server.server_port)
                               if self._local_control_client() and REMOTE_ACCESS_ENABLED else []),
                "claudeModel": DEFAULT_CLAUDE_MODEL,
                "codexModel": DEFAULT_CODEX_MODEL,
                "grokModel": DEFAULT_GROK_MODEL,
                # checked per-request so a fresh install shows up on page reload
                "clis": {name: bool(_cli_path(name)) for name in AI_KEYS},
                "cwd": AGENT_CWD,
                "claudeModels": [{"slug": m["slug"], "name": m["name"],
                                  "levels": CLAUDE_MODEL_LEVELS.get(m["slug"], [])} for m in CLAUDE_MODELS],
                "codexModels": [{"slug": m["slug"], "name": m["name"],
                                 "levels": CODEX_MODEL_LEVELS.get(m["slug"], []),
                                 "defaultEffort": _codex_effort(m["slug"], "")} for m in CODEX_MODELS],
                "codexDefault": DEFAULT_CODEX_MODEL or CODEX_CONFIG_MODEL,
                "codexConfiguredEffort": _codex_effort("", ""),
                "grokModels": [{"slug": m["slug"], "name": m["name"],
                                "levels": GROK_MODEL_LEVELS.get(m["slug"], [])} for m in GROK_MODELS],
            }))
        elif self.path.startswith("/api/checkdir?"):
            from urllib.parse import urlparse, parse_qs
            raw = parse_qs(urlparse(self.path).query).get("path", [""])[0]
            p = os.path.abspath(os.path.expanduser(raw)) if raw.strip() else ""
            self._send(200, json.dumps({"ok": bool(p) and os.path.isdir(p), "resolved": p}))
        elif request_path == "/api/conversations":
            self._send(200, json.dumps(list_convs()))
        elif request_path.startswith("/api/conversations/"):
            doc = load_conv(self._conv_id())
            self._send(200, json.dumps(doc)) if doc else self._send(404, json.dumps({"error": "not found"}))
        else:
            self._send(404, "not found", "text/plain")

    def _handle_remote_access(self):
        if not self._local_control_client():
            return self._send(403, json.dumps({
                "error": "LAN access can be changed only from the server computer"
            }))
        if not _CONTROL_OPERATION_LOCK.acquire(blocking=False):
            return self._send(409, json.dumps({
                "error": "a LAN access change or server restart is already in progress"
            }))

        restart_scheduled = False
        try:
            body = self._json_body()
            if not isinstance(body, dict):
                return self._send(400, json.dumps({"error": "a JSON object is required"}))
            enabled_supplied = "enabled" in body
            password_changed = "password" in body
            password_session_secret = ""
            if enabled_supplied and not isinstance(body["enabled"], bool):
                return self._send(400, json.dumps({"error": "enabled must be true or false"}))
            if not enabled_supplied and not password_changed:
                return self._send(400, json.dumps({
                    "error": "provide enabled or a new password"
                }))
            if password_changed:
                try:
                    with _AUTH_LOCK:
                        configure_lan_password(body["password"])
                        password_session_secret = _rotate_remote_sessions()
                except ValueError as exc:
                    return self._send(400, json.dumps({"error": str(exc)}))
                except OSError:
                    return self._send(500, json.dumps({"error": "could not save LAN password"}))

            common = {
                "localUrl": f"http://127.0.0.1:{self.server.server_port}/",
                "passwordConfigured": _password_configured(),
            }

            # A password-only request deliberately preserves the current bind
            # state. This prevents an older local browser tab from accidentally
            # turning LAN access on or off while changing its password.
            if not enabled_supplied:
                return self._send(200, json.dumps({
                    "ok": True, "enabled": REMOTE_ACCESS_ENABLED, "restarting": False,
                    "urls": (_remote_urls(port=self.server.server_port)
                             if REMOTE_ACCESS_ENABLED else []),
                    **common,
                }))

            enabled = body["enabled"]
            if enabled and not _password_configured():
                return self._send(400, json.dumps({
                    "error": "set a LAN password before enabling LAN access",
                    "code": "password_required",
                }))
            if enabled == REMOTE_ACCESS_ENABLED:
                return self._send(200, json.dumps({
                    "ok": True, "enabled": enabled, "restarting": False,
                    "urls": _remote_urls(port=self.server.server_port) if enabled else [],
                    **common,
                }))

            session_secret = password_session_secret or _rotate_remote_sessions()
            target_host = "0.0.0.0" if enabled else "127.0.0.1"
            urls = (_remote_urls(self.server.server_port, target_host) if enabled else [])
            try:
                prepared = _prepare_restart(
                    session_secret, {"ROUNDTABLE_HOST": target_host},
                )
            except OSError:
                return self._send(500, json.dumps({
                    "error": "could not prepare the server restart; LAN access was not changed"
                }))

            worker = threading.Thread(
                target=_restart_worker, args=(self.server, session_secret, prepared),
                name="roundtable-remote-restart", daemon=False,
            )
            try:
                worker.start()
            except RuntimeError:
                _safe_unlink(prepared[0])
                return self._send(500, json.dumps({
                    "error": "could not start the server restart; LAN access was not changed"
                }))
            restart_scheduled = True
            self._send(200, json.dumps({
                "ok": True, "enabled": enabled, "restarting": True, "urls": urls,
                **common,
            }))
        finally:
            if not restart_scheduled:
                _CONTROL_OPERATION_LOCK.release()

    def _handle_restart(self):
        if not self._local_control_client():
            return self._send(403, json.dumps({
                "error": "the server can be restarted only from the server computer"
            }))
        if not _CONTROL_OPERATION_LOCK.acquire(blocking=False):
            return self._send(409, json.dumps({
                "error": "a LAN access change or server restart is already in progress"
            }))
        restart_scheduled = False
        try:
            try:
                prepared = _prepare_restart()
            except OSError:
                return self._send(500, json.dumps({
                    "error": "could not prepare the server restart"
                }))
            worker = threading.Thread(
                target=_restart_worker, args=(self.server, None, prepared),
                name="roundtable-restart", daemon=False,
            )
            try:
                worker.start()
            except RuntimeError:
                _safe_unlink(prepared[0])
                return self._send(500, json.dumps({"error": "could not start the server restart"}))
            restart_scheduled = True
            self._send(200, json.dumps({"ok": True}))
        finally:
            if not restart_scheduled:
                _CONTROL_OPERATION_LOCK.release()

    def do_POST(self):
        if not self._authorize_request() or not self._allow_mutation():
            return
        if not self._json_content_type():
            return self._send(415, json.dumps({"error": "application/json is required"}))
        request_path = urlsplit(self.path).path
        if request_path == "/api/auth/login":
            if not REMOTE_ACCESS_ENABLED:
                return self._send(409, json.dumps({
                    "error": "LAN access is off", "code": "lan_access_off",
                }))
            if not _password_configured():
                return self._send(409, json.dumps({
                    "error": "set a LAN password on the server computer first",
                    "code": "password_not_configured",
                }))
            body = self._json_body(max_bytes=4096)
            if not isinstance(body, dict) or not isinstance(body.get("password"), str):
                return self._send(400, json.dumps({"error": "password is required"}))
            client = self._login_client_key()
            retry_after = _login_rate_limit(client)
            if retry_after:
                return self._send(429, json.dumps({
                    "error": "too many login attempts; try again shortly",
                    "code": "login_rate_limited", "retryAfter": retry_after,
                }), headers={"Retry-After": retry_after})
            if not _LOGIN_SCRYPT_SLOTS.acquire(blocking=False):
                return self._send(429, json.dumps({
                    "error": "too many login checks are already running; try again shortly",
                    "code": "login_busy", "retryAfter": 1,
                }), headers={"Retry-After": 1})
            # Keep verification and issuance in one auth generation. A password
            # replacement cannot slip between them and mint an old-password
            # session under the newly rotated signing key.
            try:
                session_token = _authenticate_lan_password(body["password"])
            finally:
                _LOGIN_SCRYPT_SLOTS.release()
            if not session_token:
                return self._send(401, json.dumps({
                    "error": "incorrect password", "code": "invalid_password",
                }))
            _clear_login_rate_limit(client)
            return self._send(200, json.dumps({
                "ok": True,
                "sessionToken": session_token,
                "expiresIn": REMOTE_SESSION_TTL,
            }))
        if request_path == "/api/ask":
            self.handle_ask()
        elif request_path == "/api/remote":
            self._handle_remote_access()
        elif request_path == "/api/pickdir":
            if not self._local_control_client():
                return self._send(403, json.dumps({
                    "error": "The folder picker is available only from a browser on the server computer. "
                             "Type the server folder path manually."
                }))
            body = self._json_body()
            if not isinstance(body, dict) or not isinstance(body.get("initial", ""), str):
                return self._send(400, json.dumps({"error": "initial must be a path string"}))
            try:
                selected = pick_directory(body.get("initial", ""))
            except DirectoryPickerBusy as exc:
                return self._send(409, json.dumps({"error": str(exc)}))
            except DirectoryPickerUnavailable as exc:
                return self._send(501, json.dumps({
                    "error": "Could not open a folder picker. Type the path manually. " + str(exc)
                }))
            if selected is None:
                return self._send(200, json.dumps({"ok": False, "cancelled": True}))
            self._send(200, json.dumps({"ok": True, "path": selected}))
        elif request_path == "/api/restart":
            self._handle_restart()
        elif request_path == "/api/conversations":
            body = self._json_body()
            if body is None:
                return self._send(400, json.dumps({"error": "bad json"}))
            now = time.time()
            msgs = body.get("messages") or []
            doc = {"id": "c" + uuid.uuid4().hex[:12],
                   "title": body.get("title") or derive_title(msgs),
                   "createdAt": now, "updatedAt": now, "pinned": False, "messages": msgs}
            save_conv(doc)
            self._send(200, json.dumps(doc))
        else:
            self._send(404, "not found", "text/plain")

    def do_PUT(self):
        if not self._authorize_request() or not self._allow_mutation():
            return
        if not self._json_content_type():
            return self._send(415, json.dumps({"error": "application/json is required"}))
        if self.path == "/api/config":
            body = self._json_body()
            if not isinstance(body, dict) or "userName" not in body:
                return self._send(400, json.dumps({"error": "userName is required"}))
            try:
                name = update_user_name(body["userName"])
            except ValueError as exc:
                return self._send(400, json.dumps({"error": str(exc)}))
            except OSError:
                return self._send(500, json.dumps({"error": "could not save config"}))
            return self._send(200, json.dumps({"ok": True, "userName": name}))
        if not self.path.startswith("/api/conversations/"):
            return self._send(404, "not found", "text/plain")
        cid = self._conv_id()
        if not _ID_RE.match(cid):
            return self._send(400, json.dumps({"error": "bad id"}))
        body = self._json_body()
        if body is None:
            return self._send(400, json.dumps({"error": "bad json"}))
        existing = load_conv(cid) or {}
        now = time.time()
        has_msgs = isinstance(body.get("messages"), list)   # partial updates (e.g. pin-only) keep the rest
        msgs = body["messages"] if has_msgs else existing.get("messages", [])
        doc = {"id": cid,
               "title": body.get("title") or existing.get("title") or derive_title(msgs),
               "createdAt": existing.get("createdAt", now),
               "updatedAt": now if has_msgs else existing.get("updatedAt", now),
               "pinned": bool(body.get("pinned", existing.get("pinned", False))),
               "messages": msgs}
        save_conv(doc)
        self._send(200, json.dumps(doc))

    def do_DELETE(self):
        if not self._authorize_request() or not self._allow_mutation():
            return
        if not self.path.startswith("/api/conversations/"):
            return self._send(404, "not found", "text/plain")
        cid = self._conv_id()
        if _ID_RE.match(cid):
            _safe_unlink(_conv_path(cid))
        self._send(200, json.dumps({"ok": True}))

    def handle_ask(self):
        payload = self._json_body()
        if payload is None:
            return self._send(400, json.dumps({"type": "error", "text": "bad request"}))
        agent = (payload.get("agent") or "").lower()
        if agent not in AI_KEYS:
            return self._send(400, json.dumps({"type": "error", "text": f"unknown agent {agent}"}))
        try:
            active_agents = _normalize_agent_keys(payload.get("activeAgents"))
            if agent not in active_agents:
                raise ValueError(f"{agent} must be included in activeAgents")
        except ValueError as exc:
            return self._send(400, json.dumps({"type": "error", "text": str(exc)}))
        transcript = payload.get("transcript") or []
        model = payload.get({"claude": "claudeModel", "codex": "codexModel", "grok": "grokModel"}[agent])
        raw = (payload.get("cwd") or "").strip()
        workdir = os.path.abspath(os.path.expanduser(raw)) if raw else AGENT_CWD
        write = bool(payload.get("write"))
        self.stream_agent(agent, build_prompt(agent, transcript, write, active_agents), model or "",
                          payload.get("effort") or "", workdir, write)

    # --- streaming NDJSON, killed on client disconnect ---------------------
    def stream_agent(self, agent, prompt, model, effort="", workdir=None, write=False):
        workdir = workdir or AGENT_CWD
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self._send_security_headers()
        self.end_headers()

        def emit(ev):
            try:
                self.wfile.write((json.dumps(ev) + "\n").encode())
                self.wfile.flush()
                return True
            except OSError:
                return False  # client went away (barge-in)

        if not os.path.isdir(workdir):
            emit({"type": "error", "text": f"(working directory not found: {workdir})"})
            emit({"type": "done", "text": ""})
            return

        with _prepare_invocation(agent, prompt, model, effort, workdir, write) as (command, stdin_data):
            with tempfile.TemporaryFile("w+b") as errf:
                try:
                    proc = _spawn_cli(
                        command,
                        stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
                        stdout=subprocess.PIPE, stderr=errf, cwd=workdir,
                    )
                except FileNotFoundError:
                    emit({"type": "error", "text": f"(`{agent}` CLI not found on PATH)"})
                    emit({"type": "done", "text": ""})
                    return
                except (OSError, ValueError) as exc:
                    emit({"type": "error", "text": f"(`{agent}` could not start: {_short(exc, 240)})"})
                    emit({"type": "done", "text": ""})
                    return

                _register_process(proc)
                reader = None
                try:
                    if stdin_data is not None:
                        try:
                            proc.stdin.write(stdin_data)
                        except OSError:
                            pass
                        finally:
                            try:
                                proc.stdin.close()
                            except OSError:
                                pass

                    state = {"full": "", "tools": {}, "seen": set()}
                    buf = b""
                    aborted = False

                    def feed(data):
                        nonlocal buf, aborted
                        buf += data
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            text = line.decode("utf-8", "replace").strip()
                            if not text:
                                continue
                            try:
                                obj = json.loads(text)
                            except json.JSONDecodeError:
                                continue
                            for event in parse_event(agent, obj, state):
                                if not emit(event):
                                    aborted = True
                                    return

                    output_queue, reader = _start_output_reader(proc.stdout)
                    try:
                        while not aborted:
                            try:
                                chunk = output_queue.get(timeout=1.0)
                            except queue.Empty:
                                if not emit({"type": "ping"}):  # detects disconnect during silence
                                    aborted = True
                                    break
                                if proc.poll() is not None and not reader.is_alive():
                                    break
                                continue
                            if chunk is _PIPE_EOF:
                                break
                            feed(chunk)
                        if not aborted and buf.strip():
                            feed(b"\n")  # flush any final record without a newline
                    finally:
                        _kill(proc)
                        reader.join(timeout=1)
                        try:
                            proc.stdout.close()
                        except OSError:
                            pass

                    if aborted:
                        return  # client is gone; the process tree and temp prompt are already gone

                    if not state["full"].strip() and not state.get("errored"):
                        err = ""
                        try:
                            errf.flush()
                            errf.seek(0)
                            err = errf.read().decode("utf-8", "replace").strip()
                        except OSError:
                            pass
                        emit({"type": "error", "text":
                              f"({DISPLAY[agent]} produced no output) {err[-600:]}"})
                    emit({"type": "done", "text": state["full"]})
                finally:
                    _kill(proc)
                    _unregister_process(proc)


def main():
    server = RoundtableHTTPServer((HOST, PORT), Handler)
    print("=" * 60, flush=True)
    print("  AI Roundtable  —  you + Claude + Codex + Grok", flush=True)
    print("=" * 60, flush=True)
    if REMOTE_ACCESS_ENABLED:
        if HOST in ("", "0.0.0.0"):
            print(f"  This computer: http://127.0.0.1:{server.server_port}/", flush=True)
        else:
            print("  This computer: use a LAN access link below", flush=True)
        urls = _remote_urls(port=server.server_port)
        for index, url in enumerate(urls):
            label = "LAN access:" if index == 0 else "           "
            print(f"  {label} {url}", flush=True)
        if not urls:
            print(f"  LAN access:  http://<this-computer-ip>:{server.server_port}/", flush=True)
        if not _password_configured():
            print("  LAN password: not configured; remote use is locked until one is set locally.",
                  flush=True)
        else:
            print("  LAN traffic is password-protected but not encrypted.", flush=True)
    else:
        print(f"  This computer: http://{HOST}:{server.server_port}/", flush=True)
        print("  LAN access:   off (enable it from the web UI)", flush=True)
    print(f"  Platform:     {_platform_key()}", flush=True)
    print(f"  Agent CWD:    {AGENT_CWD}", flush=True)
    print(f"  Claude model: {DEFAULT_CLAUDE_MODEL}", flush=True)
    print(f"  Codex model:  {DEFAULT_CODEX_MODEL or '(codex default)'}", flush=True)
    print(f"  Grok model:   {DEFAULT_GROK_MODEL}", flush=True)
    print("  Ctrl-C to stop.", flush=True)
    print("=" * 60, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye.")
    finally:
        _kill_all_processes()
        server.server_close()


if __name__ == "__main__":
    main()
