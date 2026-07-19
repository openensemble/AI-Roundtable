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
    def test_every_agent_gets_the_four_participant_transcript(self):
        transcript = [
            {"name": "user", "text": "Compare the options."},
            {"name": "grok", "text": "I prefer option B."},
        ]
        for agent in server.AI_KEYS:
            with self.subTest(agent=agent):
                prompt = server.build_prompt(agent, transcript)
                self.assertIn(f"You are {server.DISPLAY[agent]}", prompt)
                self.assertIn("one of four participants", prompt)
                self.assertIn("- Claude", prompt)
                self.assertIn("- Codex", prompt)
                self.assertIn("- Grok", prompt)
                self.assertIn("You: Compare the options.", prompt)
                self.assertIn("Grok: I prefer option B.", prompt)

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
        self.assertIn("ask Ada Lovelace", prompt)
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
                    self.assertEqual({"claude", "codex", "grok"}, set(initial["clis"]))

                    request = urllib.request.Request(
                        base + "/api/config", method="PUT",
                        headers={"Content-Type": "application/json"},
                        data=json.dumps({"userName": "  Grace Hopper  "}).encode(),
                    )
                    with urllib.request.urlopen(request) as response:
                        saved = json.load(response)
                    self.assertEqual("Grace Hopper", saved["userName"])
                    self.assertEqual({"userName": "Grace Hopper"}, server._load_app_config(path))

                    bad_request = urllib.request.Request(
                        base + "/api/config", method="PUT",
                        headers={"Content-Type": "application/json"},
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
            command, shell = server._resolve_cli_command(["codex", "exec", "--json"])
        self.assertEqual(powershell, command[0])
        self.assertEqual(server.WINDOWS_SHIM_BRIDGE, command[command.index("-File") + 1])
        self.assertEqual(ps1, command[command.index("-File") + 2])
        self.assertEqual(["exec", "--json"], command[-2:])
        self.assertFalse(shell)

        with (mock.patch.object(server, "_is_windows", return_value=True),
              mock.patch.object(server, "_cli_path", return_value=exe)):
            command, shell = server._resolve_cli_command(["codex", "exec"])
        self.assertEqual(exe, command[0])
        self.assertFalse(shell)

    def test_windows_rejects_batch_launcher_without_safe_companion(self):
        shim = os.path.abspath(os.path.join(tempfile.gettempdir(), "unsafe.cmd"))
        with (mock.patch.object(server, "_is_windows", return_value=True),
              mock.patch.object(server, "_cli_path", return_value=None)):
            with self.assertRaisesRegex(FileNotFoundError, "official Windows executable"):
                server._resolve_cli_command([shim, "&calc.exe"])

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
        with (mock.patch.object(server.time, "sleep") as sleep,
              mock.patch.object(server, "_kill_all_processes") as kill_all,
              mock.patch.object(server, "_restart_argv", return_value=argv),
              mock.patch.object(server.sys, "executable", executable),
              mock.patch.object(server.os, "execv", side_effect=SystemExit) as execv):
            with self.assertRaises(SystemExit):
                server._restart_soon(httpd)
        sleep.assert_called_once_with(0.4)
        kill_all.assert_called_once_with()
        httpd.shutdown.assert_called_once_with()
        httpd.server_close.assert_called_once_with()
        execv.assert_called_once_with(executable, argv)

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
            "percent%PATH%value", "bang!value", 'embedded"quote', "Zażółć 你好",
            "trailing\\",
        ]
        with tempfile.TemporaryDirectory(prefix="roundtable cmd & ") as directory:
            script = os.path.join(directory, "echo_args.py")
            shim = os.path.join(directory, "roundtable-echo.cmd")
            ps1 = os.path.join(directory, "roundtable-echo.ps1")
            sentinel = os.path.join(directory, "must-not-exist.txt")
            expected.append(f'& echo compromised > "{sentinel}"')
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
