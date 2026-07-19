#!/usr/bin/env python3
"""
AI Roundtable — a lightweight local web app for a shared brainstorm between you,
Claude Code, OpenAI Codex, and Grok Build.

The app owns ONE shared transcript. Every turn, the full labeled conversation is
handed to whichever agent is replying, so all four participants see everything
and can address / question / build on each other.

Replies stream live (NDJSON over a long-lived POST). If you barge in, the browser
aborts the request and the server kills the agent's process group/tree so it stops
generating.

No third-party dependencies: run `python3 server.py` (`py -3 server.py` on Windows),
then open the printed URL.
Auth is reused from your existing `claude`, `codex`, and `grok` CLI logins.
"""

import contextlib
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
def build_prompt(self_key, transcript, write=False):
    """Render the shared transcript into a single prompt for `self_key`."""
    self_name = DISPLAY[self_key]
    user_name = DISPLAY["user"]
    other_ais = [DISPLAY[k] for k in AI_KEYS if k != self_key]
    peers = " and ".join(other_ais)

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

    return f"""You are {self_name}, one of four participants in a live collaborative \
brainstorm happening inside a shared chat app.

Participants:
- {user_name} — the human participant all three assistants are helping.
- Claude — an AI assistant (Anthropic / Claude Code).
- Codex — an AI assistant (OpenAI Codex).
- Grok — an AI assistant (xAI / Grok Build).

Everyone sees every message. This is a genuine four-way collaboration, not \
parallel monologues: build on what others said, agree or disagree with specific \
reasoning, ask {user_name}, {peers}, or any combination of them direct questions, and raise follow-ups. Address \
people by name when it helps move the idea forward.

Rules:
- Reply ONLY as {self_name}. Never write lines for {user_name}, {peers}, or anyone else.
- Do NOT prefix your message with your own name — the app already labels it.
- Be conversational and concise by default; go deep only when the topic warrants it. \
A single sentence is a fine reply when that's all that's needed.
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
    if environment_updates:
        requested_env = options.get("env")
        launch_env = dict(os.environ if requested_env is None else requested_env)
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


def _restart_soon(httpd=None):
    """Re-exec this server after a beat, so the HTTP response is delivered first.
    Same process/port; reloads code and every provider's model/effort catalog."""
    time.sleep(0.4)
    _kill_all_processes()
    if httpd is not None:
        httpd.shutdown()
        httpd.server_close()
    argv = _restart_argv()
    os.execv(sys.executable, argv)


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # quiet

    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        self.wfile.flush()

    def _json_body(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(length) or "{}")
        except (ValueError, json.JSONDecodeError):
            return None

    def _conv_id(self):
        return self.path[len("/api/conversations/"):]

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            try:
                with open(os.path.join(HERE, "index.html"), "rb") as fh:
                    self._send(200, fh.read(), "text/html; charset=utf-8")
            except OSError:
                self._send(500, "index.html not found", "text/plain")
        elif self.path == "/api/config":
            self._send(200, json.dumps({
                "userName": DISPLAY["user"],
                "platform": _platform_key(),
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
        elif self.path == "/api/conversations":
            self._send(200, json.dumps(list_convs()))
        elif self.path.startswith("/api/conversations/"):
            doc = load_conv(self._conv_id())
            self._send(200, json.dumps(doc)) if doc else self._send(404, json.dumps({"error": "not found"}))
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self):
        if self.path == "/api/ask":
            self.handle_ask()
        elif self.path == "/api/restart":
            self._send(200, json.dumps({"ok": True}))
            threading.Thread(target=_restart_soon, args=(self.server,),
                             name="roundtable-restart", daemon=False).start()
        elif self.path == "/api/conversations":
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
        transcript = payload.get("transcript") or []
        model = payload.get({"claude": "claudeModel", "codex": "codexModel", "grok": "grokModel"}[agent])
        raw = (payload.get("cwd") or "").strip()
        workdir = os.path.abspath(os.path.expanduser(raw)) if raw else AGENT_CWD
        write = bool(payload.get("write"))
        self.stream_agent(agent, build_prompt(agent, transcript, write), model or "",
                          payload.get("effort") or "", workdir, write)

    # --- streaming NDJSON, killed on client disconnect ---------------------
    def stream_agent(self, agent, prompt, model, effort="", workdir=None, write=False):
        workdir = workdir or AGENT_CWD
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
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
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print("=" * 60, flush=True)
    print("  AI Roundtable  —  you + Claude + Codex + Grok", flush=True)
    print("=" * 60, flush=True)
    print(f"  Open:         http://{HOST}:{PORT}/", flush=True)
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
