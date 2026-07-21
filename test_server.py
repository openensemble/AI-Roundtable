import http.client
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

import server


def event_state():
    return {"full": "", "tools": {}, "seen": set()}


class PromptTests(unittest.TestCase):
    def test_omitted_roster_keeps_all_agents_active_for_compatibility(self):
        transcript = [
            {"name": "user", "text": "Compare the options."},
            {"name": "grok", "text": "I prefer option B."},
        ]
        for agent in server.AI_KEYS:
            with self.subTest(agent=agent):
                prompt = server.build_prompt(agent, transcript)
                self.assertIn(f"You are {server.DISPLAY[agent]}", prompt)
                self.assertIn("- Claude — an AI assistant; participating in this round", prompt)
                self.assertIn("- Codex — an AI assistant; participating in this round", prompt)
                self.assertIn("- Grok — an AI assistant; participating in this round", prompt)
                self.assertIn("You: Compare the options.", prompt)
                self.assertIn("Grok: I prefer option B.", prompt)

    def test_prompt_marks_unselected_agent_inactive(self):
        prompt = server.build_prompt(
            "claude",
            [{"name": "grok", "text": "An earlier message."}],
            active_agents=["claude", "codex"],
        )
        self.assertIn("Claude — an AI assistant; participating in this round", prompt)
        self.assertIn("Codex — an AI assistant; participating in this round", prompt)
        self.assertIn("Grok — an AI assistant; not participating in this round; will not reply", prompt)
        self.assertIn("Do not address Grok, ask them questions, wait for them", prompt)
        self.assertIn("treat those earlier messages as context only", prompt)
        self.assertIn("Collaborate with You and Codex", prompt)
        self.assertNotIn("one of four participants", prompt)
        self.assertNotIn("four-way collaboration", prompt)

    def test_solo_agent_prompt_has_no_phantom_peers(self):
        prompt = server.build_prompt("codex", [], active_agents=["codex"])
        self.assertIn("Collaborate with You:", prompt)
        self.assertIn("Do not address Claude and Grok", prompt)
        self.assertNotIn("Collaborate with You and", prompt)

    def test_agent_roster_is_validated_and_deduplicated(self):
        self.assertEqual(
            ["codex", "claude"],
            server._normalize_agent_keys(["CODEX", "claude", "codex"]),
        )
        for invalid in ("claude", ["claude", "unknown"], [None]):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                server._normalize_agent_keys(invalid)
        with self.assertRaises(ValueError):
            server.build_prompt("claude", [], active_agents=["codex"])

    def test_write_permission_text_tracks_edit_mode(self):
        self.assertIn("read-only", server.build_prompt("grok", [], write=False))
        self.assertIn("full read/write access", server.build_prompt("grok", [], write=True))

    def test_configured_name_labels_modern_and_legacy_human_messages(self):
        transcript = [
            {"name": "user", "text": "New role key."},
            {"name": "old-local-label", "text": "Old role key."},
            {"name": "claude", "text": "Assistant reply."},
        ]
        with mock.patch.dict(server.DISPLAY, {"user": "Ada Lovelace"}):
            prompt = server.build_prompt("grok", transcript)
        self.assertIn("- Ada Lovelace — the human participant", prompt)
        self.assertIn("Ada Lovelace: New role key.", prompt)
        self.assertIn("Ada Lovelace: Old role key.", prompt)
        self.assertIn("Collaborate with Ada Lovelace", prompt)
        self.assertNotIn("old-local-label", prompt)


class UserConfigTests(unittest.TestCase):
    def test_name_validation_and_config_round_trip(self):
        self.assertEqual("Ada Lovelace", server._clean_user_name("  Ada   Lovelace  "))
        self.assertEqual("Zoë O’Connor", server._clean_user_name("Zoë O’Connor"))
        for invalid in (None, "", "   ", "Claude", "line\nbreak", "x" * 65):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                server._clean_user_name(invalid)

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.json")
            with (mock.patch.dict(server.APP_CONFIG, {}, clear=True),
                  mock.patch.dict(server.DISPLAY, {"user": server.DEFAULT_USER_NAME})):
                self.assertEqual("Ada Lovelace", server.update_user_name(" Ada Lovelace ", path))
                self.assertEqual("Ada Lovelace", server.DISPLAY["user"])
                self.assertEqual({"userName": "Ada Lovelace"}, server._load_app_config(path))
                server.update_user_name("You", path)
                with open(path, encoding="utf-8") as fh:
                    self.assertEqual({}, json.load(fh))

    def test_bad_config_falls_back_and_failed_write_preserves_active_name(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("not json")
            self.assertEqual({}, server._load_app_config(path))

        with (mock.patch.dict(server.APP_CONFIG, {"userName": "Existing"}, clear=True),
              mock.patch.dict(server.DISPLAY, {"user": "Existing"}),
              mock.patch.object(server, "_write_app_config", side_effect=OSError("disk full"))):
            with self.assertRaises(OSError):
                server.update_user_name("Replacement")
            self.assertEqual({"userName": "Existing"}, server.APP_CONFIG)
            self.assertEqual("Existing", server.DISPLAY["user"])

    def test_config_api_reports_clis_and_persists_name(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.json")
            with (mock.patch.object(server, "CONFIG_PATH", path),
                  mock.patch.dict(server.APP_CONFIG, {}, clear=True),
                  mock.patch.dict(server.DISPLAY, {"user": server.DEFAULT_USER_NAME})):
                httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
                thread = threading.Thread(target=httpd.serve_forever, daemon=True)
                thread.start()
                base = f"http://127.0.0.1:{httpd.server_port}"
                try:
                    with urllib.request.urlopen(base + "/api/config") as response:
                        initial = json.load(response)
                    self.assertEqual("You", initial["userName"])
                    self.assertTrue(initial["folderPicker"])
                    self.assertEqual({"claude", "codex", "grok"}, set(initial["clis"]))

                    request = urllib.request.Request(
                        base + "/api/config", method="PUT",
                        headers={"Content-Type": "application/json", "X-AIConvo-Request": "1"},
                        data=json.dumps({"userName": "  Grace Hopper  "}).encode(),
                    )
                    with urllib.request.urlopen(request) as response:
                        saved = json.load(response)
                    self.assertEqual("Grace Hopper", saved["userName"])
                    self.assertEqual({"userName": "Grace Hopper"}, server._load_app_config(path))

                    bad_request = urllib.request.Request(
                        base + "/api/config", method="PUT",
                        headers={"Content-Type": "application/json", "X-AIConvo-Request": "1"},
                        data=json.dumps({"userName": "Grok"}).encode(),
                    )
                    with self.assertRaises(urllib.error.HTTPError) as caught:
                        urllib.request.urlopen(bad_request)
                    self.assertEqual(400, caught.exception.code)
                    self.assertEqual("Grace Hopper", server.DISPLAY["user"])
                finally:
                    httpd.shutdown()
                    httpd.server_close()
                    thread.join(timeout=2)


class AskRosterTests(unittest.TestCase):
    def test_ask_passes_the_request_roster_into_the_prompt(self):
        captured = {}

        def fake_stream(handler, agent, prompt, model, effort, workdir, write):
            captured.update(agent=agent, prompt=prompt)
            handler._send(200, json.dumps({"ok": True}))

        with mock.patch.object(server.Handler, "stream_agent", new=fake_stream):
            httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{httpd.server_port}"
            try:
                request = urllib.request.Request(
                    base + "/api/ask", method="POST",
                    headers={"Content-Type": "application/json", "X-AIConvo-Request": "1"},
                    data=json.dumps({
                        "agent": "claude",
                        "activeAgents": ["claude", "codex"],
                        "transcript": [{"name": "user", "text": "Discuss this."}],
                    }).encode(),
                )
                with urllib.request.urlopen(request) as response:
                    self.assertEqual(200, response.status)
                self.assertEqual("claude", captured["agent"])
                self.assertIn("Codex — an AI assistant; participating in this round", captured["prompt"])
                self.assertIn("Grok — an AI assistant; not participating in this round", captured["prompt"])

                bad_request = urllib.request.Request(
                    base + "/api/ask", method="POST",
                    headers={"Content-Type": "application/json", "X-AIConvo-Request": "1"},
                    data=json.dumps({
                        "agent": "claude", "activeAgents": ["claude", "grok-impostor"]
                    }).encode(),
                )
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(bad_request)
                self.assertEqual(400, caught.exception.code)
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)


class DirectoryPickerTests(unittest.TestCase):
    def test_picker_initial_uses_nearest_existing_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            nested = os.path.join(directory, "missing", "deeper")
            self.assertEqual(directory, server._picker_initial_directory(nested))

    def test_windows_picker_passes_initial_path_through_the_environment(self):
        with tempfile.TemporaryDirectory(prefix="workspace 'quoted' ") as directory:
            powershell = os.path.abspath(os.path.join(tempfile.gettempdir(), "powershell.exe"))
            with (mock.patch.object(server, "_platform_key", return_value="windows"),
                  mock.patch.object(server, "_windows_powershell_path", return_value=powershell)):
                label, command, updates = next(server._directory_picker_candidates(directory))
            self.assertEqual("Windows folder dialog", label)
            self.assertEqual(powershell, command[0])
            self.assertIn("-STA", command)
            self.assertNotIn(directory, command)
            self.assertEqual(directory, updates[server._PICKER_INITIAL_ENV])

    def test_picker_success_cancel_and_backend_fallback(self):
        with tempfile.TemporaryDirectory(prefix="picked workspace ") as directory:
            candidates = [
                ("Broken dialog", ["broken-picker"], {}),
                ("Working dialog", ["working-picker"], {}),
            ]
            results = [
                subprocess.CompletedProcess(["broken-picker"], 2, b"", b""),
                subprocess.CompletedProcess(
                    ["working-picker"], 0, os.fsencode(directory) + b"\n", b""
                ),
            ]
            with (mock.patch.object(server, "_directory_picker_candidates", return_value=candidates),
                  mock.patch.object(server.subprocess, "run", side_effect=results)):
                self.assertEqual(directory, server.pick_directory(directory))

            cancel = subprocess.CompletedProcess(["picker"], 1, b"", b"")
            with (mock.patch.object(
                      server, "_directory_picker_candidates",
                      return_value=[("Zenity folder dialog", ["picker"], {})]),
                  mock.patch.object(server.subprocess, "run", return_value=cancel)):
                self.assertIsNone(server.pick_directory(directory))

    def test_picker_reports_unavailable_backends(self):
        failed = subprocess.CompletedProcess(["picker"], 2, b"", b"headless session")
        with (mock.patch.object(
                  server, "_directory_picker_candidates",
                  return_value=[("Test dialog", ["picker"], {})]),
              mock.patch.object(server.subprocess, "run", return_value=failed)):
            with self.assertRaises(server.DirectoryPickerUnavailable) as caught:
                server.pick_directory("")
        self.assertIn("headless session", str(caught.exception))

    def test_picker_http_api_handles_success_cancel_errors_and_bad_input(self):
        with tempfile.TemporaryDirectory() as directory:
            outcomes = [
                directory,
                None,
                server.DirectoryPickerUnavailable("headless session"),
            ]
            with mock.patch.object(server, "pick_directory", side_effect=outcomes) as picker:
                httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
                thread = threading.Thread(target=httpd.serve_forever, daemon=True)
                thread.start()
                base = f"http://127.0.0.1:{httpd.server_port}"

                def post(body, content_type="application/json"):
                    request = urllib.request.Request(
                        base + "/api/pickdir", method="POST",
                        headers={"Content-Type": content_type, "X-AIConvo-Request": "1"},
                        data=json.dumps(body).encode(),
                    )
                    with urllib.request.urlopen(request) as response:
                        return response.status, json.load(response)

                try:
                    status, result = post({"initial": directory})
                    self.assertEqual((200, True, directory),
                                     (status, result["ok"], result["path"]))
                    status, result = post({"initial": directory})
                    self.assertEqual((200, True), (status, result["cancelled"]))

                    with self.assertRaises(urllib.error.HTTPError) as unavailable:
                        post({"initial": directory})
                    self.assertEqual(501, unavailable.exception.code)

                    with self.assertRaises(urllib.error.HTTPError) as invalid:
                        post({"initial": 123})
                    self.assertEqual(400, invalid.exception.code)
                    with self.assertRaises(urllib.error.HTTPError) as wrong_type:
                        post({"initial": directory}, "text/plain")
                    self.assertEqual(415, wrong_type.exception.code)
                    with mock.patch.object(server.Handler, "_loopback_client", return_value=False):
                        with self.assertRaises(urllib.error.HTTPError) as remote:
                            post({"initial": directory})
                    self.assertEqual(403, remote.exception.code)
                    self.assertEqual(3, picker.call_count)
                finally:
                    httpd.shutdown()
                    httpd.server_close()
                    thread.join(timeout=2)


class RemoteAccessTests(unittest.TestCase):
    def test_loopback_detection_and_lan_urls_contain_no_credentials(self):
        self.assertTrue(server._host_is_loopback("127.0.0.1"))
        self.assertTrue(server._host_is_loopback("::1"))
        self.assertTrue(server._host_is_loopback("localhost"))
        self.assertFalse(server._host_is_loopback("0.0.0.0"))
        self.assertFalse(server._host_is_loopback("192.168.1.20"))
        with mock.patch.object(server, "_lan_ipv4_addresses",
                               return_value=["10.0.0.8", "192.168.1.20"]):
            urls = server._remote_urls(9001, "0.0.0.0")
            self.assertEqual([
                "http://10.0.0.8:9001/",
                "http://192.168.1.20:9001/",
            ], urls)
            for url in urls:
                self.assertNotIn("?", url)
                self.assertNotIn("#", url)
                self.assertNotIn("token", url.casefold())
        with mock.patch.object(server, "HOST", "192.168.1.77"):
            self.assertEqual(
                ["http://192.168.1.77:9001/"],
                server._remote_urls(9001),
            )

    def test_bind_hostnames_resolve_once_and_ipv6_fails_clearly(self):
        resolved = [(server.socket.AF_INET, server.socket.SOCK_STREAM, 6, "",
                     ("192.168.1.77", 0))]
        with mock.patch.object(server.socket, "getaddrinfo", return_value=resolved):
            self.assertEqual("192.168.1.77", server._canonical_bind_host("my-pc.local"))
        self.assertEqual("127.0.0.1", server._canonical_bind_host("localhost"))
        with self.assertRaisesRegex(ValueError, "IPv6"):
            server._canonical_bind_host("::1")

    def test_lan_host_must_match_socket_destination_and_peer_scope(self):
        handler = object.__new__(server.Handler)
        handler.client_address = ("192.0.2.50", 4242)
        with (mock.patch.object(handler, "_request_host", return_value="192.0.2.81"),
              mock.patch.object(handler, "_socket_destination", return_value="192.0.2.81")):
            self.assertTrue(handler._valid_lan_host())
        with (mock.patch.object(handler, "_request_host", return_value="192.0.2.81"),
              mock.patch.object(handler, "_socket_destination", return_value="198.51.100.8")):
            self.assertFalse(handler._valid_lan_host())
        handler.client_address = ("8.8.8.8", 4242)
        with (mock.patch.object(handler, "_request_host", return_value="192.0.2.81"),
              mock.patch.object(handler, "_socket_destination", return_value="192.0.2.81")):
            self.assertFalse(handler._valid_lan_host())

    def test_password_verifier_is_salted_persisted_without_plaintext_and_exact(self):
        password = "correct horse battery staple"
        salt = b"s" * server.LAN_PASSWORD_SALT_BYTES
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.json")
            config = {}
            with (mock.patch.object(server, "APP_CONFIG", config),
                  mock.patch.object(server.secrets, "token_bytes", return_value=salt)):
                self.assertTrue(server.configure_lan_password(password, path))
                with open(path, encoding="utf-8") as handle:
                    persisted_text = handle.read()
                persisted = json.loads(persisted_text)

                self.assertNotIn(password, persisted_text)
                self.assertEqual({"salt", "verifier"},
                                 set(persisted[server.LAN_PASSWORD_CONFIG_KEY]))
                self.assertTrue(server._password_configured(config))
                self.assertTrue(server._verify_lan_password(password))
                self.assertFalse(server._verify_lan_password(password + "!"))

                stored_salt, verifier = server._password_record(config)
                self.assertEqual(salt, stored_salt)
                self.assertNotEqual(
                    verifier,
                    server._derive_password_verifier(
                        password, b"t" * server.LAN_PASSWORD_SALT_BYTES,
                    ),
                )

        malformed = {server.LAN_PASSWORD_CONFIG_KEY: {
            "salt": "not-valid-base64!", "verifier": "also-invalid!",
        }}
        self.assertIsNone(server._password_record(malformed))
        self.assertFalse(server._password_configured(malformed))

    def test_password_validation_rejects_unsafe_or_unbounded_values(self):
        invalid = [
            None,
            "",
            " " * server.LAN_PASSWORD_MIN_CHARS,
            "short",
            "password\n",
            "x" * (server.LAN_PASSWORD_MAX_CHARS + 1),
        ]
        for value in invalid:
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValueError):
                    server._clean_lan_password(value)
        self.assertEqual(" eight chars ", server._clean_lan_password(" eight chars "))

    def test_signed_sessions_are_distinct_expire_and_rotate(self):
        issued = 10_000
        with (mock.patch.object(server, "REMOTE_SESSION_SECRET", "old-signing-secret"),
              mock.patch.object(server.secrets, "token_urlsafe",
                                side_effect=["nonce-one", "nonce-two", "new-signing-secret"])):
            first = server._issue_remote_session(issued)
            second = server._issue_remote_session(issued)
            self.assertNotEqual(first, second)
            self.assertTrue(server._valid_remote_session(first, issued))
            self.assertTrue(server._valid_remote_session(
                first, issued + server.REMOTE_SESSION_TTL - 1,
            ))
            self.assertFalse(server._valid_remote_session(
                first, issued + server.REMOTE_SESSION_TTL,
            ))
            self.assertFalse(server._valid_remote_session("correct horse battery staple", issued))

            encoded, _signature = first.split(".", 1)
            tampered = encoded + "." + server._b64encode(b"\0" * 32)
            self.assertFalse(server._valid_remote_session(tampered, issued))

            self.assertEqual("new-signing-secret", server._rotate_remote_sessions())
            self.assertFalse(server._valid_remote_session(first, issued))

    def test_password_rotation_cannot_issue_an_old_password_session_on_the_new_key(self):
        password = "old-password"
        salt = b"s" * server.LAN_PASSWORD_SALT_BYTES
        verifier = b"v" * server.LAN_PASSWORD_DKLEN
        config = {server.LAN_PASSWORD_CONFIG_KEY: {
            "salt": server._b64encode(salt),
            "verifier": server._b64encode(verifier),
        }}
        deriving = threading.Event()
        release_derivation = threading.Event()
        rotation_started = threading.Event()
        rotation_done = threading.Event()
        issued = []

        def slow_derive(value, supplied_salt):
            self.assertEqual((password, salt), (value, supplied_salt))
            deriving.set()
            self.assertTrue(release_derivation.wait(2))
            return verifier

        def authenticate():
            issued.append(server._authenticate_lan_password(password))

        def rotate():
            rotation_started.set()
            with server._AUTH_LOCK:
                server._rotate_remote_sessions()
            rotation_done.set()

        with (mock.patch.object(server, "APP_CONFIG", config),
              mock.patch.object(server, "REMOTE_SESSION_SECRET", "old-secret"),
              mock.patch.object(server, "_derive_password_verifier", side_effect=slow_derive)):
            login_thread = threading.Thread(target=authenticate)
            login_thread.start()
            self.assertTrue(deriving.wait(1))
            rotation_thread = threading.Thread(target=rotate)
            rotation_thread.start()
            self.assertTrue(rotation_started.wait(1))
            self.assertFalse(rotation_done.wait(.05))
            release_derivation.set()
            login_thread.join(2)
            rotation_thread.join(2)
            self.assertFalse(login_thread.is_alive())
            self.assertFalse(rotation_thread.is_alive())
            self.assertTrue(issued[0])
            self.assertFalse(server._valid_remote_session(issued[0]))

    def test_login_rate_limit_is_per_client_and_can_be_cleared(self):
        with (mock.patch.object(server, "_LOGIN_ATTEMPTS", {}),
              mock.patch.object(server, "_LOGIN_GLOBAL_ATTEMPTS", [])):
            for offset in range(server.LOGIN_RATE_MAX_ATTEMPTS):
                self.assertEqual(0, server._login_rate_limit("192.0.2.10", 100 + offset))
            self.assertGreater(server._login_rate_limit("192.0.2.10", 105), 0)
            self.assertEqual(0, server._login_rate_limit("192.0.2.11", 105))
            server._clear_login_rate_limit("192.0.2.10")
            self.assertEqual(0, server._login_rate_limit("192.0.2.10", 106))

        with (mock.patch.object(server, "_LOGIN_ATTEMPTS", {}),
              mock.patch.object(server, "_LOGIN_GLOBAL_ATTEMPTS", [])):
            for index in range(server.LOGIN_RATE_GLOBAL_MAX_ATTEMPTS):
                self.assertEqual(0, server._login_rate_limit(f"192.0.2.{index + 1}", 200))
            self.assertGreater(server._login_rate_limit("198.51.100.1", 200), 0)
            self.assertLessEqual(len(server._LOGIN_ATTEMPTS),
                                 server.LOGIN_RATE_GLOBAL_MAX_ATTEMPTS)
            self.assertEqual(0, server._login_rate_limit(
                "198.51.100.1", 200 + server.LOGIN_RATE_WINDOW + 1,
            ))
            self.assertEqual(["198.51.100.1"], list(server._LOGIN_ATTEMPTS))

    def test_remote_login_issues_separate_session_and_protects_apis(self):
        with (mock.patch.object(server, "REMOTE_ACCESS_ENABLED", True),
              mock.patch.object(server.Handler, "_local_control_client", return_value=False),
              mock.patch.object(server.Handler, "_valid_lan_host", return_value=True),
              mock.patch.object(server, "_password_configured", return_value=True),
              mock.patch.object(server, "_verify_lan_password",
                                side_effect=lambda value: value == "phone-password") as verify,
              mock.patch.object(server, "_issue_remote_session",
                                return_value="opaque-phone-session"),
              mock.patch.object(server, "_valid_remote_session",
                                side_effect=lambda value: value == "opaque-phone-session"),
              mock.patch.object(server, "_login_rate_limit", return_value=0),
              mock.patch.object(server, "_clear_login_rate_limit")):
            httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            port = httpd.server_port
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            origin = f"http://127.0.0.1:{port}"

            def request(method, path, body=None, headers=None):
                connection.request(method, path, body=body, headers=headers or {})
                response = connection.getresponse()
                raw = response.read()
                payload = (json.loads(raw) if raw and
                           response.getheader("Content-Type", "").startswith("application/json")
                           else raw)
                return response, payload

            try:
                shell, _ = request("GET", "/")
                self.assertEqual(200, shell.status)
                self.assertEqual("DENY", shell.getheader("X-Frame-Options"))

                unauthorized, error = request("GET", "/api/config")
                self.assertEqual(401, unauthorized.status)
                self.assertEqual("lan_login_required", error["code"])

                protected = {
                    "X-AIConvo-Request": "1",
                    "Origin": origin,
                    "Content-Type": "application/json",
                }
                simple, error = request(
                    "POST", "/api/auth/login",
                    json.dumps({"password": "phone-password"}),
                    {"Content-Type": "text/plain", "Origin": "http://malicious.invalid"},
                )
                self.assertEqual(403, simple.status)
                self.assertIn("protected app request", error["error"])

                cross_origin, error = request(
                    "POST", "/api/auth/login",
                    json.dumps({"password": "phone-password"}),
                    {**protected, "Origin": "http://malicious.invalid"},
                )
                self.assertEqual(403, cross_origin.status)
                self.assertIn("same-origin", error["error"])

                wrong_type, error = request(
                    "POST", "/api/auth/login",
                    json.dumps({"password": "phone-password"}),
                    {**protected, "Content-Type": "text/plain"},
                )
                self.assertEqual(415, wrong_type.status)

                rejected, error = request(
                    "POST", "/api/auth/login",
                    json.dumps({"password": "wrong-password"}), protected,
                )
                self.assertEqual(401, rejected.status)
                self.assertEqual("invalid_password", error["code"])
                self.assertNotIn("sessionToken", error)

                logged_in, login = request(
                    "POST", "/api/auth/login",
                    json.dumps({"password": "phone-password"}), protected,
                )
                self.assertEqual(200, logged_in.status)
                self.assertEqual("no-store", logged_in.getheader("Cache-Control"))
                self.assertEqual("opaque-phone-session", login["sessionToken"])
                self.assertNotIn("phone-password", json.dumps(login))
                self.assertEqual(2, verify.call_count)

                password_is_not_session, _ = request("GET", "/api/config", headers={
                    "X-AIConvo-Session": "phone-password",
                })
                self.assertEqual(401, password_is_not_session.status)

                allowed, config = request("GET", "/api/config", headers={
                    "X-AIConvo-Session": "opaque-phone-session",
                })
                self.assertEqual(200, allowed.status)
                self.assertTrue(config["remoteEnabled"])
                self.assertFalse(config["remoteControl"])
                self.assertEqual([], config["remoteUrls"])

                local_only, _ = request(
                    "POST", "/api/restart", json.dumps({}), {
                        **protected, "X-AIConvo-Session": "opaque-phone-session",
                    },
                )
                self.assertEqual(403, local_only.status)
            finally:
                connection.close()
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)

    def test_remote_without_password_fails_closed(self):
        with (mock.patch.object(server, "REMOTE_ACCESS_ENABLED", True),
              mock.patch.object(server.Handler, "_local_control_client", return_value=False),
              mock.patch.object(server.Handler, "_valid_lan_host", return_value=True),
              mock.patch.object(server, "_password_configured", return_value=False)):
            httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            connection = http.client.HTTPConnection(
                "127.0.0.1", httpd.server_port, timeout=3,
            )
            origin = f"http://127.0.0.1:{httpd.server_port}"
            try:
                connection.request("GET", "/api/auth/status")
                status = connection.getresponse()
                self.assertEqual(200, status.status)
                self.assertFalse(json.load(status)["passwordConfigured"])

                connection.request(
                    "POST", "/api/auth/login",
                    body=json.dumps({"password": "phone-password"}),
                    headers={
                        "Content-Type": "application/json",
                        "X-AIConvo-Request": "1",
                        "Origin": origin,
                    },
                )
                login = connection.getresponse()
                self.assertEqual(409, login.status)
                self.assertEqual("password_not_configured", json.load(login)["code"])

                connection.request("GET", "/api/config")
                protected = connection.getresponse()
                self.assertEqual(401, protected.status)
                protected.read()
            finally:
                connection.close()
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)

    def test_hostile_webpage_cannot_post_to_loopback_agent_api(self):
        with mock.patch.object(server.Handler, "stream_agent") as stream:
            httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            connection = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=3)
            try:
                payload = json.dumps({
                    "agent": "claude", "cwd": tempfile.gettempdir(), "write": True,
                    "transcript": [{"name": "user", "text": "Run a command."}],
                })
                connection.request(
                    "POST", "/api/ask", body=payload,
                    headers={"Content-Type": "text/plain", "Origin": "http://malicious.invalid"},
                )
                response = connection.getresponse()
                self.assertEqual(403, response.status)
                self.assertIn("protected app request", json.load(response)["error"])
                stream.assert_not_called()
            finally:
                connection.close()
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)

    def test_loopback_server_rejects_dns_rebinding_host(self):
        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        connection = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=3)
        try:
            connection.request("GET", "/api/config", headers={
                "Host": "roundtable.attacker.invalid"
            })
            response = connection.getresponse()
            self.assertEqual(403, response.status)
            self.assertIn("local URL", json.load(response)["error"])
        finally:
            connection.close()
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)

    def test_provider_process_never_inherits_session_handoff_secrets(self):
        with (mock.patch.dict(server.os.environ,
                              {server.REMOTE_TOKEN_ENV: "must-not-reach-agent",
                               server.REMOTE_TOKEN_FILE_ENV: "/tmp/must-not-reach-agent",
                               "AICONVO_TEST_SENTINEL": "kept"}, clear=False),
              mock.patch.object(server, "_resolve_cli_command",
                                return_value=(["agent"], {})),
              mock.patch.object(server, "_popen_platform_kwargs", return_value={}),
              mock.patch.object(server.subprocess, "Popen") as popen):
            server._spawn_cli(["agent"], stdout=subprocess.PIPE)
        environment = popen.call_args.kwargs["env"]
        self.assertNotIn(server.REMOTE_TOKEN_ENV, environment)
        self.assertNotIn(server.REMOTE_TOKEN_FILE_ENV, environment)
        self.assertEqual("kept", environment["AICONVO_TEST_SENTINEL"])

    def test_restart_session_secret_handoff_is_private_consumed_and_deleted(self):
        path = server._write_remote_token_handoff("handoff-secret")
        try:
            if os.name != "nt":
                self.assertEqual(0, os.stat(path).st_mode & 0o077)
            with mock.patch.dict(server.os.environ, {
                server.REMOTE_TOKEN_FILE_ENV: path,
                server.REMOTE_TOKEN_ENV: "obsolete-direct-secret",
            }, clear=False):
                self.assertEqual("handoff-secret", server._consume_remote_token_handoff())
                self.assertNotIn(server.REMOTE_TOKEN_FILE_ENV, server.os.environ)
                self.assertNotIn(server.REMOTE_TOKEN_ENV, server.os.environ)
            self.assertFalse(os.path.exists(path))
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_local_remote_toggle_requires_password_and_uses_plain_urls(self):
        restarted = threading.Event()
        restart_secrets = []
        restart_environments = []
        password_state = {"configured": False, "values": []}

        def fake_restart(httpd, remote_token=None, prepared=None):
            restart_secrets.append(remote_token)
            restart_environments.append(prepared[1])
            restarted.set()

        def fake_configure(value):
            server._clean_lan_password(value)
            password_state["configured"] = True
            password_state["values"].append(value)
            return True

        with (mock.patch.object(server, "REMOTE_ACCESS_ENABLED", False),
              mock.patch.object(server, "_password_configured",
                                side_effect=lambda: password_state["configured"]),
              mock.patch.object(server, "configure_lan_password",
                                side_effect=fake_configure),
              mock.patch.object(server, "_rotate_remote_sessions",
                                side_effect=["enable-secret", "change-secret", "disable-secret"]),
              mock.patch.object(server, "_remote_urls",
                                return_value=["http://192.168.1.5:8765/"]),
              mock.patch.object(server, "_restart_soon", side_effect=fake_restart),
              mock.patch.dict(server.os.environ, {}, clear=False)):
            httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()

            def toggle(body):
                return urllib.request.urlopen(urllib.request.Request(
                    f"http://127.0.0.1:{httpd.server_port}/api/remote", method="POST",
                    headers={"Content-Type": "application/json", "X-AIConvo-Request": "1"},
                    data=json.dumps(body).encode(),
                ))

            try:
                with self.assertRaises(urllib.error.HTTPError) as missing:
                    toggle({"enabled": True})
                self.assertEqual(400, missing.exception.code)
                self.assertEqual("password_required", json.load(missing.exception)["code"])

                with self.assertRaises(urllib.error.HTTPError) as short:
                    toggle({"enabled": True, "password": "short"})
                self.assertEqual(400, short.exception.code)
                self.assertEqual([], restart_secrets)

                with toggle({"enabled": True, "password": "phone-password"}) as response:
                    result = json.load(response)
                self.assertTrue(result["restarting"])
                self.assertEqual(["http://192.168.1.5:8765/"], result["urls"])
                self.assertTrue(result["passwordConfigured"])
                self.assertNotIn("accessToken", result)
                self.assertNotIn("sessionToken", result)
                self.assertNotIn("phone-password", json.dumps(result))
                self.assertEqual("0.0.0.0", restart_environments[-1]["ROUNDTABLE_HOST"])
                self.assertNotIn(server.REMOTE_TOKEN_ENV, server.os.environ)
                self.assertTrue(restarted.wait(1))
                self.assertEqual(["enable-secret"], restart_secrets)
                self.assertEqual(["phone-password"], password_state["values"])
                deadline = time.time() + 1
                while server._CONTROL_OPERATION_LOCK.locked() and time.time() < deadline:
                    time.sleep(.01)

                restarted.clear()
                with mock.patch.object(server, "REMOTE_ACCESS_ENABLED", True):
                    with toggle({"password": "replacement-password"}) as response:
                        changed = json.load(response)
                    self.assertFalse(changed["restarting"])
                    self.assertTrue(changed["enabled"])
                    self.assertTrue(changed["passwordConfigured"])
                    self.assertEqual(["enable-secret"], restart_secrets)

                    with toggle({"enabled": False}) as response:
                        disabled = json.load(response)
                self.assertTrue(disabled["restarting"])
                self.assertEqual("127.0.0.1", restart_environments[-1]["ROUNDTABLE_HOST"])
                self.assertNotIn(server.REMOTE_TOKEN_ENV, server.os.environ)
                self.assertTrue(restarted.wait(1))
                self.assertEqual(["enable-secret", "disable-secret"], restart_secrets)
                self.assertEqual(
                    ["phone-password", "replacement-password"],
                    password_state["values"],
                )
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)

    def test_control_endpoints_reject_overlapping_restart_or_lan_change(self):
        self.assertTrue(server._CONTROL_OPERATION_LOCK.acquire(blocking=False))
        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            for path, body in (("/api/restart", {}), ("/api/remote", {"enabled": False})):
                with self.subTest(path=path):
                    request = urllib.request.Request(
                        f"http://127.0.0.1:{httpd.server_port}{path}", method="POST",
                        headers={"Content-Type": "application/json", "X-AIConvo-Request": "1"},
                        data=json.dumps(body).encode(),
                    )
                    with self.assertRaises(urllib.error.HTTPError) as caught:
                        urllib.request.urlopen(request)
                    self.assertEqual(409, caught.exception.code)
                    self.assertIn("already in progress", json.load(caught.exception)["error"])
        finally:
            server._CONTROL_OPERATION_LOCK.release()
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)


class CommandTests(unittest.TestCase):
    def test_claude_catalog_loader_uses_sdk_capabilities_and_special_mode(self):
        response = {
            "type": "control_response",
            "response": {"response": {
                "models": [
                    {"value": "fable", "resolvedModel": "claude-fable-5",
                     "supportsEffort": True,
                     "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"]},
                    {"value": "haiku", "resolvedModel": "claude-haiku-4-5-20251001"},
                ],
                "commands": [{"name": "effort",
                              "argumentHint": "<low|medium|high|xhigh|max|ultracode|auto>"}],
                "account": {"email": "must-not-be-retained@example.invalid"},
            }},
        }
        completed = subprocess.CompletedProcess(
            ["claude"], 0, stdout=(json.dumps(response) + "\n").encode()
        )
        with mock.patch.object(server, "_run_cli_capture", return_value=completed) as capture:
            levels = server._load_claude_model_levels()
        command, input_data = capture.call_args.args[:2]
        self.assertEqual("claude", command[0])
        self.assertIn(b'"subtype": "initialize"', input_data)
        self.assertEqual(5, capture.call_args.kwargs["timeout"])
        self.assertEqual(
            ["low", "medium", "high", "xhigh", "max", "ultracode"],
            levels["claude-fable-5"],
        )
        self.assertEqual([], levels["claude-haiku-4-5-20251001"])
        self.assertNotIn("account", levels)

    def test_existing_claude_and_codex_commands_remain_distinct(self):
        claude = server.build_cmd("claude", "claude-opus-4-8", "high", "/tmp/work")
        codex = server.build_cmd("codex", "gpt-test", "medium", "/tmp/work")
        self.assertEqual("claude", claude[0])
        self.assertIn("--add-dir", claude)
        self.assertEqual(["codex", "exec"], codex[:2])
        self.assertIn("read-only", codex)

    def test_claude_efforts_are_model_specific(self):
        for model in ("claude-opus-4-8", "claude-fable-5", "claude-sonnet-5"):
            with self.subTest(model=model):
                self.assertEqual(
                    ["low", "medium", "high", "xhigh", "max", "ultracode"],
                    server._claude_levels(model),
                )
        self.assertEqual([], server._claude_levels("claude-haiku-4-5-20251001"))

    def test_claude_ultracode_is_passed_only_when_supported(self):
        fable = server.build_cmd("claude", "claude-fable-5", "ultracode", "/tmp/work")
        haiku = server.build_cmd(
            "claude", "claude-haiku-4-5-20251001", "ultracode", "/tmp/work"
        )
        self.assertEqual("ultracode", fable[fable.index("--effort") + 1])
        self.assertNotIn("--effort", haiku)

    def test_haiku_omits_even_numeric_effort_requests(self):
        haiku = server.build_cmd("claude", "claude-haiku-4-5-20251001", "max", "/tmp/work")
        self.assertNotIn("--effort", haiku)

    def test_grok_read_only_command_uses_prompt_file_and_hard_sandbox(self):
        prompt_file = os.path.join(tempfile.gettempdir(), "roundtable prompt.txt")
        cmd = server.build_cmd(
            "grok", "grok-4.5", "medium", "/tmp/work", write=False,
            prompt_file=prompt_file,
        )
        self.assertEqual("grok", cmd[0])
        self.assertEqual(os.path.abspath(prompt_file), cmd[cmd.index("--prompt-file") + 1])
        self.assertIn("streaming-json", cmd)
        self.assertEqual("/tmp/work", cmd[cmd.index("--cwd") + 1])
        self.assertEqual("grok-4.5", cmd[cmd.index("--model") + 1])
        self.assertEqual("read-only", cmd[cmd.index("--sandbox") + 1])
        self.assertEqual("dontAsk", cmd[cmd.index("--permission-mode") + 1])
        self.assertEqual("medium", cmd[cmd.index("--reasoning-effort") + 1])
        self.assertIn("--no-memory", cmd)
        self.assertIn("--no-subagents", cmd)

    def test_grok_edit_command_is_workspace_scoped(self):
        cmd = server.build_cmd(
            "grok", "", "xhigh", "/tmp/work", write=True,
            prompt_file=os.path.join(tempfile.gettempdir(), "prompt.txt"),
        )
        self.assertEqual("workspace", cmd[cmd.index("--sandbox") + 1])
        self.assertEqual("bypassPermissions", cmd[cmd.index("--permission-mode") + 1])
        # Grok 4.5 tops out at high, so unsupported higher requests are clamped.
        self.assertEqual("high", cmd[cmd.index("--reasoning-effort") + 1])

    def test_unknown_agent_is_rejected(self):
        with self.assertRaises(ValueError):
            server.build_cmd("unknown", "")

    def test_codex_env_default_and_effort_validation_use_the_same_model(self):
        levels = {"env-model": ["low", "medium", "high", "xhigh", "max"],
                  "config-model": ["low", "medium", "high", "xhigh", "max", "ultra"]}
        with (mock.patch.object(server, "DEFAULT_CODEX_MODEL", "env-model"),
              mock.patch.object(server, "CODEX_CONFIG_MODEL", "config-model"),
              mock.patch.object(server, "CODEX_MODEL_LEVELS", levels)):
            cmd = server.build_cmd("codex", "", "ultra", "/tmp/work")
        self.assertEqual("env-model", cmd[cmd.index("-m") + 1])
        self.assertIn("model_reasoning_effort=max", cmd)

    def test_codex_default_effort_follows_the_selected_model(self):
        levels = {
            "gpt-default": ["low", "medium", "high", "xhigh", "max", "ultra"],
            "gpt-mini": ["low", "medium", "high", "xhigh"],
        }
        defaults = {"gpt-default": "low", "gpt-mini": "medium"}
        with (mock.patch.object(server, "DEFAULT_CODEX_MODEL", ""),
              mock.patch.object(server, "CODEX_CONFIG_MODEL", "gpt-default"),
              mock.patch.object(server, "CODEX_CONFIG_EFFORT", "ultra"),
              mock.patch.object(server, "CODEX_MODEL_LEVELS", levels),
              mock.patch.object(server, "CODEX_MODEL_DEFAULTS", defaults)):
            mini = server.build_cmd("codex", "gpt-mini", "", "/tmp/work")
            configured = server.build_cmd("codex", "", "", "/tmp/work")
            explicit_configured = server.build_cmd("codex", "gpt-default", "", "/tmp/work")
            explicit = server.build_cmd("codex", "gpt-mini", "max", "/tmp/work")
        self.assertIn("model_reasoning_effort=medium", mini)
        self.assertIn("model_reasoning_effort=ultra", configured)
        self.assertIn("model_reasoning_effort=low", explicit_configured)
        self.assertIn("model_reasoning_effort=xhigh", explicit)
        self.assertNotIn("model_reasoning_effort=max", mini)

    def test_empty_or_future_effort_metadata_is_safe(self):
        self.assertEqual("", server.clamp_effort("low", ["future-level"]))
        with (mock.patch.object(server, "DEFAULT_CODEX_MODEL", "no-effort"),
              mock.patch.object(server, "CODEX_MODEL_LEVELS", {"no-effort": []})):
            cmd = server.build_cmd("codex", "", "high", "/tmp/work")
        self.assertFalse(any(x.startswith("model_reasoning_effort=") for x in cmd))

    def test_invalid_inherited_codex_effort_uses_model_default(self):
        with (mock.patch.object(server, "DEFAULT_CODEX_MODEL", ""),
              mock.patch.object(server, "CODEX_CONFIG_MODEL", "known-model"),
              mock.patch.object(server, "CODEX_CONFIG_EFFORT", "future-level"),
              mock.patch.object(server, "CODEX_MODEL_LEVELS",
                                {"known-model": ["low", "medium", "high"]}),
              mock.patch.object(server, "CODEX_MODEL_DEFAULTS", {"known-model": "medium"})):
            cmd = server.build_cmd("codex", "", "", "/tmp/work")
        self.assertIn("model_reasoning_effort=medium", cmd)

    def test_unknown_codex_model_does_not_guess_effort_levels(self):
        with (mock.patch.object(server, "DEFAULT_CODEX_MODEL", ""),
              mock.patch.object(server, "CODEX_CONFIG_MODEL", "known-model"),
              mock.patch.object(server, "CODEX_CONFIG_EFFORT", "ultra"),
              mock.patch.object(server, "CODEX_MODEL_LEVELS", {"known-model": ["high"]}),
              mock.patch.object(server, "CODEX_MODEL_DEFAULTS", {"known-model": "high"})):
            cmd = server.build_cmd("codex", "future-model", "max", "/tmp/work")
        self.assertFalse(any(x.startswith("model_reasoning_effort=") for x in cmd))


class ProcessPortabilityTests(unittest.TestCase):
    def test_platform_detection_and_popen_kwargs(self):
        with (mock.patch.object(server.os, "name", "nt"),
              mock.patch.object(server.sys, "platform", "win32")):
            self.assertTrue(server._is_windows())
            self.assertEqual("windows", server._platform_key())
            self.assertEqual(
                {"creationflags": server.WINDOWS_NEW_PROCESS_GROUP},
                server._popen_platform_kwargs(),
            )

        with (mock.patch.object(server.os, "name", "posix"),
              mock.patch.object(server.sys, "platform", "darwin")):
            self.assertFalse(server._is_windows())
            self.assertEqual("macos", server._platform_key())
            self.assertEqual({"start_new_session": True}, server._popen_platform_kwargs())

        with (mock.patch.object(server.os, "name", "posix"),
              mock.patch.object(server.sys, "platform", "linux")):
            self.assertEqual("linux", server._platform_key())

    def test_windows_cli_resolution_prefers_exe_and_falls_back_to_cmd(self):
        exe = os.path.abspath(os.path.join(tempfile.gettempdir(), "native codex.exe"))
        shim = os.path.abspath(os.path.join(tempfile.gettempdir(), "codex.cmd"))
        ps1 = os.path.splitext(shim)[0] + ".ps1"
        powershell = os.path.abspath(os.path.join(tempfile.gettempdir(), "powershell.exe"))

        with (mock.patch.object(server, "_is_windows", return_value=True),
              mock.patch.object(server, "_windows_native_cli_candidates", return_value=[]),
              mock.patch.object(server.shutil, "which",
                                side_effect=lambda name: exe if name == "codex.exe" else shim)):
            self.assertEqual(exe, server._cli_path("codex"))

        def only_shim(name):
            return shim if name == "codex" else None

        with (mock.patch.object(server, "_is_windows", return_value=True),
              mock.patch.object(server, "_windows_native_cli_candidates", return_value=[]),
              mock.patch.object(server.shutil, "which", side_effect=only_shim),
              mock.patch.object(server, "_windows_powershell_path", return_value=powershell),
              mock.patch.object(server.os.path, "isfile",
                                side_effect=lambda path: path in (ps1, server.WINDOWS_SHIM_BRIDGE))):
            self.assertEqual(shim, server._cli_path("codex"))
            command, environment = server._resolve_cli_command(["codex", "exec", "--json"])
        self.assertEqual(powershell, command[0])
        self.assertEqual(server.WINDOWS_SHIM_BRIDGE, command[command.index("-File") + 1])
        self.assertEqual(server.WINDOWS_SHIM_BRIDGE, command[-1])
        self.assertEqual(ps1, environment[server.WINDOWS_SHIM_PATH_ENV])
        self.assertEqual(
            ["exec", "--json"],
            json.loads(environment[server.WINDOWS_SHIM_ARGS_ENV]),
        )

        with (mock.patch.object(server, "_is_windows", return_value=True),
              mock.patch.object(server, "_cli_path", return_value=exe)):
            command, environment = server._resolve_cli_command(["codex", "exec"])
        self.assertEqual(exe, command[0])
        self.assertEqual({}, environment)

    def test_windows_rejects_batch_launcher_without_safe_companion(self):
        shim = os.path.abspath(os.path.join(tempfile.gettempdir(), "unsafe.cmd"))
        with (mock.patch.object(server, "_is_windows", return_value=True),
              mock.patch.object(server, "_cli_path", return_value=None)):
            with self.assertRaisesRegex(FileNotFoundError, "official Windows executable"):
                server._resolve_cli_command([shim, "&calc.exe"])

    def test_windows_powershell_shim_rejects_unrepresentable_arguments(self):
        shim = os.path.abspath(os.path.join(tempfile.gettempdir(), "agent.cmd"))
        companion = os.path.splitext(shim)[0] + ".ps1"
        powershell = os.path.abspath(os.path.join(tempfile.gettempdir(), "powershell.exe"))
        for value in ("", 'embedded"quote', "line\nbreak", "nul\0value"):
            with (self.subTest(value=value),
                  mock.patch.object(server, "_is_windows", return_value=True),
                  mock.patch.object(server, "_cli_path", return_value=shim),
                  mock.patch.object(server, "_windows_powershell_path", return_value=powershell),
                  mock.patch.object(server.os.path, "isfile",
                                    side_effect=lambda path: path == companion)):
                with self.assertRaisesRegex(ValueError, "cannot be empty"):
                    server._resolve_cli_command([shim, value])

    def test_prepare_invocation_writes_utf8_prompt_and_always_cleans_up(self):
        prompt = "Zażółć gęślą jaźń — 你好 🚀"
        prompt_path = None
        with server._prepare_invocation(
            "grok", prompt, "grok-4.5", "medium", tempfile.gettempdir(), False
        ) as (command, stdin_data):
            prompt_path = command[command.index("--prompt-file") + 1]
            self.assertIsNone(stdin_data)
            self.assertTrue(os.path.isfile(prompt_path))
            with open(prompt_path, "rb") as fh:
                self.assertEqual(prompt.encode("utf-8"), fh.read())
        self.assertIsNotNone(prompt_path)
        self.assertFalse(os.path.exists(prompt_path))

        failed_path = None
        with self.assertRaisesRegex(RuntimeError, "spawn failed"):
            with server._prepare_invocation(
                "grok", prompt, "grok-4.5", "medium", tempfile.gettempdir(), False
            ) as (command, _):
                failed_path = command[command.index("--prompt-file") + 1]
                raise RuntimeError("spawn failed")
        self.assertIsNotNone(failed_path)
        self.assertFalse(os.path.exists(failed_path))

        with server._prepare_invocation(
            "codex", prompt, "gpt-test", "medium", tempfile.gettempdir(), False
        ) as (command, stdin_data):
            self.assertEqual(prompt.encode("utf-8"), stdin_data)
            self.assertEqual("-", command[-1])

    def test_output_reader_preserves_crlf_and_final_partial_line(self):
        pipe = io.BytesIO("first\r\nsecond\n最後".encode("utf-8"))
        output_queue, reader = server._start_output_reader(pipe)
        reader.join(timeout=2)
        self.assertFalse(reader.is_alive())

        output = []
        while True:
            item = output_queue.get_nowait()
            if item is server._PIPE_EOF:
                break
            output.append(item)
        self.assertEqual(
            [b"first\r\n", b"second\n", "最後".encode("utf-8")],
            output,
        )

    def test_windows_kill_uses_taskkill_tree(self):
        proc = mock.Mock(pid=4321)
        proc.poll.return_value = None
        proc.wait.return_value = 0
        completed = subprocess.CompletedProcess(["taskkill.exe"], 0)
        with (mock.patch.object(server, "_is_windows", return_value=True),
              mock.patch.object(server, "_taskkill_path", return_value="taskkill.exe"),
              mock.patch.object(server.subprocess, "run", return_value=completed) as run):
            server._kill(proc)
        run.assert_called_once_with(
            ["taskkill.exe", "/PID", "4321", "/T", "/F"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=5, check=False,
        )
        proc.wait.assert_called_once_with(timeout=3)
        proc.terminate.assert_not_called()
        proc.kill.assert_not_called()

    def test_windows_failed_taskkill_falls_back_to_terminate(self):
        proc = mock.Mock(pid=9876)
        proc.poll.return_value = None
        proc.wait.return_value = 0
        completed = subprocess.CompletedProcess(["taskkill.exe"], 1)
        with (mock.patch.object(server, "_is_windows", return_value=True),
              mock.patch.object(server, "_taskkill_path", return_value="taskkill.exe"),
              mock.patch.object(server.subprocess, "run", return_value=completed)):
            server._kill(proc)
        proc.terminate.assert_called_once_with()
        proc.kill.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "native Windows process-tree behavior")
    def test_real_windows_kill_terminates_child_tree(self):
        def pid_is_running(pid):
            result = subprocess.run(
                ["tasklist.exe", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
            )
            return str(pid).encode("ascii") in result.stdout

        with tempfile.TemporaryDirectory(prefix="roundtable tree ") as directory:
            pid_file = os.path.join(directory, "child.pid")
            parent_code = (
                "import pathlib, subprocess, sys, time; "
                "child=subprocess.Popen([sys.executable, '-c', "
                "'import time; time.sleep(60)']); "
                "pathlib.Path(sys.argv[1]).write_text(str(child.pid), encoding='ascii'); "
                "time.sleep(60)"
            )
            parent = server._spawn_cli(
                [sys.executable, "-c", parent_code, pid_file],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            child_pid = None
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and not os.path.isfile(pid_file):
                    time.sleep(0.05)
                self.assertTrue(os.path.isfile(pid_file), "child PID was not published")
                with open(pid_file, encoding="ascii") as fh:
                    child_pid = int(fh.read())
                self.assertTrue(pid_is_running(child_pid))

                server._kill(parent)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and pid_is_running(child_pid):
                    time.sleep(0.05)
                self.assertFalse(pid_is_running(child_pid), "taskkill left the child running")
            finally:
                server._kill(parent)
                if child_pid and pid_is_running(child_pid):
                    subprocess.run(
                        ["taskkill.exe", "/PID", str(child_pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                    )

    def test_posix_kill_targets_the_process_group(self):
        proc = mock.Mock(pid=2468)
        proc.poll.return_value = None
        proc.wait.return_value = 0
        with (mock.patch.object(server, "_is_windows", return_value=False),
              mock.patch.object(server.os, "getpgid", return_value=1357,
                                create=True) as getpgid,
              mock.patch.object(server.os, "killpg", create=True) as killpg):
            server._kill(proc)
        getpgid.assert_called_once_with(2468)
        killpg.assert_called_once_with(1357, server.signal.SIGTERM)
        proc.wait.assert_called_once_with(timeout=3)
        proc.terminate.assert_not_called()

    def test_restart_argv_preserves_paths_with_spaces_and_cli_arguments(self):
        executable = os.path.join(os.sep, "Program Files", "Python", "python.exe")
        root = os.path.join(os.sep, "Users", "Test User", "AI Roundtable")
        with (mock.patch.object(server.sys, "executable", executable),
              mock.patch.object(server, "HERE", root),
              mock.patch.object(server.sys, "argv", ["server.py", "9012", "--future-flag"])):
            self.assertEqual(
                [executable, os.path.join(root, "server.py"), "9012", "--future-flag"],
                server._restart_argv(),
            )

    def test_restart_stops_agents_closes_listener_and_reexecs(self):
        httpd = mock.Mock()
        executable = os.path.abspath(os.path.join(tempfile.gettempdir(), "python executable"))
        argv = [executable, os.path.abspath("server.py"), "9012"]
        environment = {"TEST_RESTART": "1"}
        prepared = (os.path.join(tempfile.gettempdir(), "already-consumed.key"), environment)
        with (mock.patch.object(server.time, "sleep") as sleep,
              mock.patch.object(server, "_kill_all_processes") as kill_all,
              mock.patch.object(server, "_restart_argv", return_value=argv),
              mock.patch.object(server.sys, "executable", executable),
              mock.patch.object(server.os, "execve", side_effect=SystemExit) as execve):
            with self.assertRaises(SystemExit):
                server._restart_soon(httpd, prepared=prepared)
        sleep.assert_called_once_with(0.4)
        kill_all.assert_called_once_with()
        httpd.shutdown.assert_called_once_with()
        httpd.server_close.assert_called_once_with()
        execve.assert_called_once_with(executable, argv, environment)

    def test_provider_loaders_use_configured_home_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            codex_home = os.path.join(directory, "Codex Home")
            grok_home = os.path.join(directory, "Grok Home")
            os.makedirs(codex_home)
            os.makedirs(grok_home)
            with open(os.path.join(codex_home, "models_cache.json"), "w", encoding="utf-8") as fh:
                json.dump({"models": [{
                    "slug": "gpt-windows", "display_name": "GPT Windows",
                    "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}],
                    "default_reasoning_level": "high",
                }]}, fh)
            with open(os.path.join(codex_home, "config.toml"), "w", encoding="utf-8") as fh:
                fh.write('model = "gpt-windows"\nmodel_reasoning_effort = "high"\n')
            with open(os.path.join(grok_home, "models_cache.json"), "w", encoding="utf-8") as fh:
                json.dump({"models": {"grok-windows": {"info": {
                    "name": "Grok Windows",
                    "reasoning_efforts": [{"value": "low"}, {"value": "high"}],
                }}}}, fh)

            with mock.patch.object(server, "CODEX_HOME", codex_home):
                models, levels, defaults = server._load_codex_models()
                config_model, config_effort = server._load_codex_config()
            self.assertEqual([{"slug": "gpt-windows", "name": "GPT Windows"}], models)
            self.assertEqual({"gpt-windows": ["low", "high"]}, levels)
            self.assertEqual({"gpt-windows": "high"}, defaults)
            self.assertEqual(("gpt-windows", "high"), (config_model, config_effort))

            with mock.patch.object(server, "GROK_HOME", grok_home):
                models, levels = server._load_grok_models()
            self.assertEqual([{"slug": "grok-windows", "name": "Grok Windows"}], models)
            self.assertEqual(["low", "high"], levels["grok-windows"])
            self.assertEqual(["low", "medium", "high"], levels[server.DEFAULT_GROK_MODEL])

    @unittest.skipUnless(os.name == "nt", "native Windows .cmd behavior")
    def test_real_cmd_shim_preserves_argument_boundaries(self):
        expected = [
            "plain", "space value", "amp&ersand", "paren(value)", "caret^value",
            "percent%PATH%value", "bang!value", "Zażółć 你好", "trailing\\",
        ]
        with tempfile.TemporaryDirectory(prefix="roundtable cmd & ") as directory:
            script = os.path.join(directory, "echo_args.py")
            shim = os.path.join(directory, "roundtable-echo.cmd")
            ps1 = os.path.join(directory, "roundtable-echo.ps1")
            sentinel = os.path.join(directory, "must-not-exist.txt")
            expected.append(f"& echo compromised > {sentinel}")
            with open(script, "w", encoding="utf-8") as fh:
                fh.write(
                    "import json, sys\n"
                    "data = (json.dumps(sys.argv[1:], ensure_ascii=False) + '\\n').encode('utf-8')\n"
                    "sys.stdout.buffer.write(data)\n"
                )
            with open(shim, "w", encoding="utf-8", newline="") as fh:
                fh.write('@echo off\r\necho unsafe batch shim must not run\r\nexit /b 91\r\n')
            with open(ps1, "w", encoding="utf-8", newline="") as fh:
                escaped_python = sys.executable.replace("'", "''")
                fh.write(f"& '{escaped_python}' (Join-Path $PSScriptRoot 'echo_args.py') @args\r\n")
            proc = server._spawn_cli(
                [shim, *expected], stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            stdout, stderr = proc.communicate(timeout=10)
            self.assertEqual(0, proc.returncode, stderr.decode("utf-8", "replace"))
            self.assertEqual(expected, json.loads(stdout.decode("utf-8")))
            self.assertFalse(os.path.exists(sentinel))

class EventParserTests(unittest.TestCase):
    def test_grok_thought_and_fragmented_text(self):
        state = event_state()
        thought = server.parse_event("grok", {"type": "thought", "data": "Considering"}, state)
        first = server.parse_event("grok", {"type": "text", "data": "GROK_"}, state)
        second = server.parse_event("grok", {"type": "text", "data": "OK"}, state)
        end = server.parse_event("grok", {"type": "end", "stopReason": "EndTurn"}, state)

        self.assertEqual([{"type": "activity", "kind": "thinking", "text": "Considering"}], thought)
        self.assertEqual([{"type": "delta", "text": "GROK_"}], first)
        self.assertEqual([{"type": "delta", "text": "OK"}], second)
        self.assertEqual("GROK_OK", state["full"])
        self.assertEqual([], end)

    def test_grok_error_is_labeled_and_marks_state(self):
        state = event_state()
        events = server.parse_event("grok", {"type": "error", "message": "bad model"}, state)
        self.assertTrue(state["errored"])
        self.assertEqual([{"type": "error", "text": "(Grok) bad model"}], events)

    def test_existing_codex_message_parser_still_works(self):
        state = event_state()
        events = server.parse_event(
            "codex",
            {"type": "item.completed", "item": {"type": "agent_message", "text": "hello"}},
            state,
        )
        self.assertEqual([{"type": "delta", "text": "hello"}], events)
        self.assertEqual("hello", state["full"])

    def test_duplicate_codex_failure_is_emitted_once(self):
        state = event_state()
        message = '{"error":{"message":"bad effort"}}'
        first = server.parse_event("codex", {"type": "error", "message": message}, state)
        second = server.parse_event(
            "codex", {"type": "turn.failed", "error": {"message": message}}, state
        )
        self.assertEqual([{"type": "error", "text": "(Codex) " + message}], first)
        self.assertEqual([], second)


if __name__ == "__main__":
    unittest.main()
