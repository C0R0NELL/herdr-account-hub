#!/usr/bin/env python3
"""Global account switching: isolated stores and a simulated Codex daemon."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import types
import tempfile
import unittest
import uuid
from unittest.mock import patch

from tests.test_account_records import login


class MachineSwitch(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hub-machine-test-")
        self.home = Path(self.temp.name, "shared Codex home")
        self.home.mkdir(mode=0o700)
        self.socket = self.home / "control.sock"
        self.socket.touch(mode=0o600)
        self.env = patch.dict(os.environ, {
            "CODEX_HOME": str(self.home),
            "CODEX_SQLITE_HOME": str(Path(self.temp.name, "custom database")),
            "ACCOUNT_HUB_STATE_DIR": str(Path(self.temp.name, "plugin")),
            "ACCOUNT_HUB_CONFIG_DIR": self.temp.name,
        })
        self.env.start()
        spec = importlib.util.spec_from_file_location("machine_hub", Path(__file__).resolve().parents[1] / "switcher.py")
        self.S = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.S)
        S = self.S
        S.notify = lambda *a: None
        S.proven_payload = lambda kind, profile: profile["payload"]
        S._http_json = S._post_json = lambda *a, **k: self.fail("unexpected HTTP")
        self.profiles = [S.make_profile("codex", user, login(user, user, user))
                         for user in ("one", "two", "three")]
        for profile in self.profiles:
            S.write_profile(profile)
        S.BACKENDS["codex"].write_live(self.profiles[0]["payload"])
        S._select_profile("codex", self.profiles[0]["id"])
        self.server_payload = copy.deepcopy(self.profiles[0]["payload"])
        self.threads = {str(uuid.uuid4()): {"name": name, "cwd": self.temp.name,
                       "status": {"type": "idle"}}
                        for name in ("Custom first title", "Custom second title")}
        self.pid = 12000
        self.running = True
        self.restarts = 0
        self.fail_restart = self.wrong_account = self.lose_thread = False
        self.credential_store = "file"
        self.commands = []
        self.command_options = []
        self.methods = []
        self.closed = []

        fixture = self
        class RPC:
            def __init__(self, *a, **k): pass
            def close(self): fixture.closed.append(True)
            def call(self, method, params=None, **kwargs):
                fixture.methods.append((method, params))
                if method == "config/read":
                    return {"config": {"cli_auth_credentials_store": fixture.credential_store,
                                       "model_provider": "openai"}}
                if method == "account/read":
                    email = S.BACKENDS["codex"].identity(fixture.server_payload)["email"]
                    return {"account": {"type": "chatgpt", "email": email, "planType": "team"}}
                if method == "thread/loaded/list":
                    ids = list(fixture.threads)
                    if fixture.lose_thread and fixture.restarts == 1:
                        ids = ids[:1]
                    return {"data": ids[:1] if not (params or {}).get("cursor") else ids[1:],
                            "nextCursor": "second" if len(ids) > 1 and not (params or {}).get("cursor") else None}
                if method == "thread/read":
                    thread_id = params["threadId"]
                    return {"thread": dict(fixture.threads[thread_id], id=thread_id)}
                fixture.fail("unsupported RPC " + method)

        def command(argv, **kwargs):
            self.commands.append(list(argv))
            self.command_options.append(kwargs)
            if argv[-1] == "--version":
                return subprocess.CompletedProcess(argv, 0, "codex-cli 0.162.0", "")
            action = argv[-1]
            if action == "restart":
                self.restarts += 1
                if self.fail_restart and self.restarts == 1:
                    self.running = False
                    return subprocess.CompletedProcess(argv, 1, "", "secret provider detail")
                self.pid += 1
                self.running = True
                self.threads = {key: thread for key, thread in self.threads.items()
                                if not thread.get("ephemeral")}
                self.server_payload = S.BACKENDS["codex"].read_live()
                if self.wrong_account and self.restarts == 1:
                    self.server_payload = copy.deepcopy(self.profiles[0]["payload"])
            elif action != "version":
                self.fail("unsupported command " + repr(argv))
            info = {"status": "running" if self.running else "notRunning", "backend": "pid",
                    "pid": self.pid, "socketPath": str(self.socket),
                    "appServerVersion": "0.162.0"}
            if action == "restart": info["status"] = "restarted"
            return subprocess.CompletedProcess(argv, 0, json.dumps(info), "")
        self.rpc_patch = patch.object(S, "CodexRPC", RPC)
        self.rpc_patch.start()
        S._run = command

    def tearDown(self):
        self.rpc_patch.stop()
        self.env.stop()
        self.temp.cleanup()

    def test_one_switch_updates_machine_and_reconnects_both_chats(self):
        before = copy.deepcopy(self.threads)
        result = self.S.switch("codex", self.profiles[1]["id"])
        self.assertEqual(self.restarts, 1, "switch must invalidate the running auth cache")
        self.assertEqual(self.server_payload, self.profiles[1]["payload"])
        self.assertEqual(self.threads, before)
        self.assertEqual(self.S._selection()["codex"], self.profiles[1]["id"])
        self.assertIn("2 chats", result)
        self.assertNotIn("UUID", result)
        self.assertNotIn("account/login/start", [m for m, p in self.methods])
        self.assertNotIn("thread/name/set", [m for m, p in self.methods])
        self.assertFalse(any("codex-run" in argv for argv in self.commands))

    def test_not_running_installs_login_without_starting_daemon(self):
        self.running = False
        self.S.switch("codex", self.profiles[2]["id"])
        self.assertEqual(self.restarts, 0)
        self.assertEqual(self.S.BACKENDS["codex"].read_live(), self.profiles[2]["payload"])

    def test_temporary_chat_refusal_identifies_title_directory_and_id_before_writes(self):
        thread_id = next(iter(self.threads))
        thread = self.threads[thread_id]
        thread.update(ephemeral=True, name="Draft deployment", cwd="/work/setupvibe",
                      preview="PRIVATE PROMPT MUST NOT BE SHOWN")
        before = self.S.BACKENDS["codex"].read_live()
        with self.assertRaises(self.S.SwitchError) as caught:
            self.S.switch("codex", self.profiles[1]["id"])
        message = str(caught.exception)
        self.assertIn("[1] setupvibe: Draft deployment", message)
        self.assertIn(thread_id[:8], message)
        self.assertIn("Close these chats and retry; account unchanged", message)
        self.assertNotIn("PRIVATE PROMPT", message)
        self.assertEqual(self.S.BACKENDS["codex"].read_live(), before)
        self.assertEqual(self.S._selection()["codex"], self.profiles[0]["id"])
        self.assertEqual(self.restarts, 0)
        self.assertTrue(self.closed, "the diagnostic connection must close on refusal")

    def test_untitled_temporary_chat_remains_identifiable_without_using_preview(self):
        thread_id = next(iter(self.threads))
        self.threads[thread_id].update(ephemeral=True, name=None, cwd="/work/setupvibe",
                                       preview="SECRET PROMPT")
        with self.assertRaises(self.S.SwitchError) as caught:
            self.S.switch("codex", self.profiles[1]["id"])
        self.assertIn(f"[1] setupvibe: Untitled ({thread_id[:8]})", str(caught.exception))
        self.assertEqual(self.restarts, 0)

    def test_temporary_chat_identifier_is_bounded_and_has_no_terminal_controls(self):
        thread_id = next(iter(self.threads))
        self.threads[thread_id].update(ephemeral=True, name="\x1b[31mDraft\n" + "x" * 200,
                                       cwd="/work/\x1b[31mproject\n" + "y" * 200)
        with self.assertRaises(self.S.SwitchError) as caught:
            self.S.switch("codex", self.profiles[1]["id"])
        message = str(caught.exception)
        self.assertNotIn("\x1b", message)
        self.assertLessEqual(message.count("\n"), 2, "labels must not inject extra lines")
        self.assertLessEqual(len(message), 180)
        self.assertIn(thread_id[:8], message)

    def test_ephemeral_idle_chat_is_not_assumed_safe_to_discard(self):
        thread_id = next(iter(self.threads))
        self.threads[thread_id].update(ephemeral=True, name=None, preview="", path=None,
                                       status={"type": "idle"})
        with self.assertRaises(self.S.SwitchError):
            self.S.switch("codex", self.profiles[1]["id"])
        self.assertEqual(self.restarts, 0)

    def test_temporary_chat_list_reaches_picker_at_eighty_columns(self):
        thread_id = next(iter(self.threads))
        self.threads[thread_id].update(ephemeral=True, name=None, cwd="/work/setupvibe")
        texts, _ = self.picker([10, 27], width=80)
        self.assertTrue(any(f"[1] setupvibe: Untitled ({thread_id[:8]})" in text
                            for text in texts))
        self.assertEqual(self.restarts, 0)

    def test_all_temporary_chats_are_numbered_in_stable_order(self):
        for number, thread_id in enumerate(sorted(self.threads)):
            self.threads[thread_id].update(ephemeral=True, name=f"Draft {number}", cwd="/work/project")
        with self.assertRaises(self.S.SwitchError) as caught:
            self.S.switch("codex", self.profiles[1]["id"])
        message = str(caught.exception)
        for number, thread_id in enumerate(sorted(self.threads), start=1):
            self.assertIn(f"[{number}] project: Draft {number - 1} ({thread_id[:8]})", message)
        self.assertIn("2 temporary Codex chats", message)
        self.assertEqual(self.restarts, 0)

    def test_persisted_chats_are_not_included_in_temporary_list(self):
        first, second = sorted(self.threads)
        self.threads[first].update(ephemeral=False, name="Saved chat")
        self.threads[second].update(ephemeral=True, name="Draft", cwd="/work/project")
        with self.assertRaises(self.S.SwitchError) as caught:
            self.S.switch("codex", self.profiles[1]["id"])
        message = str(caught.exception)
        self.assertIn("1 temporary Codex chat;", message)
        self.assertIn(f"[1] project: Draft ({second[:8]})", message)
        self.assertNotIn("Saved chat", message)
        self.assertNotIn(first[:8], message)

    def test_force_discards_only_confirmed_temporary_chats_and_preserves_saved_chats(self):
        saved = copy.deepcopy(self.threads)
        temporary_id = str(uuid.uuid4())
        self.threads[temporary_id] = {"name": "Draft", "cwd": self.temp.name, "ephemeral": True}
        with self.assertRaises(self.S.TemporaryChatsError) as blocked:
            self.S.switch("codex", self.profiles[1]["id"])
        result = self.S.switch("codex", self.profiles[1]["id"],
                               discard_temporary=blocked.exception.thread_ids)
        self.assertEqual(self.threads, saved)
        self.assertEqual(self.restarts, 1)
        self.assertEqual(self.server_payload, self.profiles[1]["payload"])
        self.assertIn("2 chats reloaded", result)
        self.assertIn("1 temporary chat closed", result)
        self.assertFalse(any(method in ("thread/delete", "thread/archive", "thread/unsubscribe")
                             for method, _ in self.methods))

    def test_force_requires_new_confirmation_for_a_new_temporary_chat(self):
        temporary_id = str(uuid.uuid4())
        self.threads[temporary_id] = {"name": "Draft", "cwd": self.temp.name, "ephemeral": True}
        with self.assertRaises(self.S.TemporaryChatsError) as blocked:
            self.S.switch("codex", self.profiles[1]["id"])
        new_id = str(uuid.uuid4())
        self.threads[new_id] = {"name": "New draft", "cwd": self.temp.name, "ephemeral": True}
        with self.assertRaises(self.S.TemporaryChatsError) as refreshed:
            self.S.switch("codex", self.profiles[1]["id"],
                          discard_temporary=blocked.exception.thread_ids)
        self.assertEqual(set(refreshed.exception.thread_ids), {temporary_id, new_id})
        self.assertEqual(self.restarts, 0)
        self.assertEqual(self.S.BACKENDS["codex"].read_live(), self.profiles[0]["payload"])

    def test_force_rechecks_after_target_auth_verification_before_writing(self):
        temporary_id = str(uuid.uuid4())
        self.threads[temporary_id] = {"name": "Draft", "cwd": self.temp.name, "ephemeral": True}
        def verify(kind, profile):
            self.threads[str(uuid.uuid4())] = {"name": "New draft", "cwd": self.temp.name, "ephemeral": True}
            return profile["payload"]
        self.S.proven_payload = verify
        with self.assertRaises(self.S.TemporaryChatsError):
            self.S.switch("codex", self.profiles[1]["id"], discard_temporary=[temporary_id])
        self.assertEqual(self.restarts, 0)
        self.assertEqual(self.S.BACKENDS["codex"].read_live(), self.profiles[0]["payload"])

    def test_new_temporary_chat_before_restart_restores_login_and_requests_confirmation(self):
        temporary_id = str(uuid.uuid4())
        new_id = str(uuid.uuid4())
        self.threads[temporary_id] = {"name": "Draft", "cwd": self.temp.name, "ephemeral": True}
        backend = self.S.BACKENDS["codex"]
        write = backend.write_live
        def concurrent_chat(payload):
            write(payload)
            self.threads[new_id] = {"name": "New draft", "cwd": self.temp.name, "ephemeral": True}
        backend.write_live = concurrent_chat
        with self.assertRaises(self.S.TemporaryChatsError) as blocked:
            self.S.switch("codex", self.profiles[1]["id"], discard_temporary=[temporary_id])
        self.assertEqual(set(blocked.exception.thread_ids), {temporary_id, new_id})
        self.assertEqual(self.restarts, 0)
        self.assertEqual(backend.read_live(), self.profiles[0]["payload"])
        self.assertEqual(self.S._selection()["codex"], self.profiles[0]["id"])

    def test_forced_switch_failure_reports_irreversible_loss_after_rollback(self):
        temporary_id = str(uuid.uuid4())
        self.threads[temporary_id] = {"name": "Draft", "cwd": self.temp.name, "ephemeral": True}
        self.wrong_account = True
        with self.assertRaises(self.S.SwitchError) as failed:
            self.S.switch("codex", self.profiles[1]["id"], discard_temporary=[temporary_id])
        self.assertIn("previous login restored", str(failed.exception))
        self.assertIn("temporary chats may already be closed", str(failed.exception))
        self.assertNotIn(temporary_id, self.threads)
        self.assertEqual(self.restarts, 2)
        self.assertEqual(self.server_payload, self.profiles[0]["payload"])

    def test_forced_switch_failed_rollback_also_warns_about_temporary_chats(self):
        temporary_id = str(uuid.uuid4())
        self.threads[temporary_id] = {"name": "Draft", "cwd": self.temp.name, "ephemeral": True}
        command = self.S._run
        def refused(argv, **kwargs):
            if argv[-1] == "restart":
                self.restarts += 1
                return subprocess.CompletedProcess(argv, 1, "", "confidential details")
            return command(argv, **kwargs)
        self.S._run = refused
        with self.assertRaises(self.S.SwitchError) as failed:
            self.S.switch("codex", self.profiles[1]["id"], discard_temporary=[temporary_id])
        self.assertIn("could not be confirmed", str(failed.exception))
        self.assertIn("temporary chats may already be closed", str(failed.exception))
        self.assertNotIn("confidential", str(failed.exception))

    def test_force_preserves_a_confirmed_chat_that_became_persistent(self):
        temporary_id = str(uuid.uuid4())
        self.threads[temporary_id] = {"name": "Now saved", "cwd": self.temp.name, "ephemeral": False}
        result = self.S.switch("codex", self.profiles[1]["id"], discard_temporary=[temporary_id])
        self.assertIn(temporary_id, self.threads)
        self.assertIn("3 chats reloaded", result)

    def test_force_without_a_running_daemon_only_installs_future_login(self):
        self.running = False
        self.S.switch("codex", self.profiles[1]["id"], discard_temporary=[str(uuid.uuid4())])
        self.assertEqual(self.restarts, 0)

    def test_switching_same_saved_account_still_clears_unconfirmed_cache(self):
        self.server_payload = self.profiles[1]["payload"]
        self.S.switch("codex", self.profiles[0]["id"])
        self.assertEqual(self.restarts, 1)
        self.assertEqual(self.server_payload, self.profiles[0]["payload"])

    def test_failed_restart_restores_previous_login_and_runtime(self):
        self.fail_restart = True
        with self.assertRaises(self.S.SwitchError) as caught:
            self.S.switch("codex", self.profiles[1]["id"])
        self.assertEqual(self.S.BACKENDS["codex"].read_live(), self.profiles[0]["payload"])
        self.assertEqual(self.server_payload, self.profiles[0]["payload"])
        self.assertEqual(self.S._selection()["codex"], self.profiles[0]["id"])
        self.assertEqual(self.restarts, 2)
        self.assertNotIn("secret provider detail", str(caught.exception))

    def test_wrong_account_after_restart_never_reports_success(self):
        self.wrong_account = True
        with self.assertRaises(self.S.SwitchError):
            self.S.switch("codex", self.profiles[1]["id"])
        self.assertEqual(self.S._selection()["codex"], self.profiles[0]["id"])
        self.assertEqual(self.server_payload, self.profiles[0]["payload"])

    def test_keyring_configuration_is_refused_before_overwriting_auth_file(self):
        self.credential_store = "keyring"
        with self.assertRaises(self.S.SwitchError):
            self.S.switch("codex", self.profiles[1]["id"])
        self.assertEqual(self.S.BACKENDS["codex"].read_live(), self.profiles[0]["payload"])
        self.assertEqual(self.restarts, 0)

    def test_api_null_defaults_are_supported(self):
        self.credential_store = None
        self.S.switch("codex", self.profiles[1]["id"])
        self.assertEqual(self.restarts, 1)

    def test_missing_chat_is_reported_and_previous_account_reloaded(self):
        self.lose_thread = True
        self.S.CODEX_RECONNECT_TIMEOUT_S = .001
        with self.assertRaises(self.S.SwitchError):
            self.S.switch("codex", self.profiles[1]["id"])
        self.assertEqual(self.restarts, 2)
        self.assertEqual(self.server_payload, self.profiles[0]["payload"])

    def test_stale_application_never_claims_that_memory_was_updated(self):
        self.S.switch("codex", self.profiles[1]["id"])
        self.assertEqual(self.S.codex_application_status(), "applied")
        self.S.BACKENDS["codex"].write_live(self.profiles[2]["payload"])
        self.assertEqual(self.S.codex_application_status(), "reload unverified")

    def test_passive_probe_failure_does_not_change_login(self):
        command = self.S._run
        def timed_out(argv, **kwargs):
            if argv[-1] == "version":
                return subprocess.CompletedProcess(argv, 1, "", "timed out probing control socket")
            return command(argv, **kwargs)
        self.S._run = timed_out
        with self.assertRaises(self.S.SwitchError):
            self.S.switch("codex", self.profiles[1]["id"])
        self.assertEqual(self.S.BACKENDS["codex"].read_live(), self.profiles[0]["payload"])
        self.assertEqual(self.restarts, 0)

    def test_absent_socket_installs_for_future_chats_without_starting_anything(self):
        command = self.S._run
        def absent(argv, **kwargs):
            if argv[-1] == "version":
                return subprocess.CompletedProcess(argv, 1, "", "failed to connect to /private/control: No such file or directory (os error 2)")
            return command(argv, **kwargs)
        self.S._run = absent
        self.S.switch("codex", self.profiles[1]["id"])
        self.assertEqual(self.restarts, 0)
        self.assertEqual(self.S.codex_application_status(), "ready for new chats")

    def test_reloading_same_account_never_restores_spent_tokens_after_failure(self):
        fresh = login("one", "rotated", "one")
        def renewed(kind, profile):
            profile["payload"] = fresh
            self.S.write_profile(profile)
            return fresh
        self.S.proven_payload = renewed
        self.fail_restart = True
        with self.assertRaises(self.S.SwitchError):
            self.S.switch("codex", self.profiles[0]["id"])
        self.assertEqual(self.server_payload, fresh)
        self.assertEqual(self.S.get_profile("codex", self.profiles[0]["id"])["payload"], fresh)

    def test_switch_current_account_uses_rotated_live_tokens_before_verification(self):
        fresh = login("one", "renewed-by-codex", "one")
        self.S.BACKENDS["codex"].write_live(fresh)
        checked = []
        def verify(kind, profile):
            checked.append(copy.deepcopy(profile["payload"]))
            if profile["payload"] != fresh:
                raise self.S.SwitchError("saved refresh token already spent")
            return fresh
        self.S.proven_payload = verify
        self.S.switch("codex", self.profiles[0]["id"])
        self.assertEqual(checked, [fresh])
        self.assertEqual(self.S.get_profile("codex", self.profiles[0]["id"])["payload"], fresh)
        self.assertEqual(self.restarts, 1)

    def test_failed_rollback_is_explicit_and_never_says_previous_reload_succeeded(self):
        command = self.S._run
        def refused(argv, **kwargs):
            if argv[-1] == "restart":
                self.restarts += 1
                return subprocess.CompletedProcess(argv, 1, "", "confidential details")
            return command(argv, **kwargs)
        self.S._run = refused
        with self.assertRaises(self.S.SwitchError) as caught:
            self.S.switch("codex", self.profiles[1]["id"])
        self.assertIn("could not be confirmed", str(caught.exception))
        self.assertNotIn("confidential", str(caught.exception))
        self.assertEqual(self.S._selection()["codex"], self.profiles[0]["id"])

    def picker(self, answer, action=10, width=120):
        texts, coordinates = [], []
        keys = iter([258, action, ord("q")])
        answers = iter(answer) if isinstance(answer, list) else None
        class Screen:
            def getmaxyx(self): return (30, width)
            def timeout(self, *a): pass
            def keypad(self, *a): pass
            def erase(self): pass
            def refresh(self): pass
            def touchwin(self): pass
            def clearok(self, *args): pass
            def box(self): pass
            def addnstr(self, y, x, text, count, *a): texts.append(text[:count])
            def getch(self): return next(keys)
        class Dialog(Screen):
            def getch(self):
                value = next(answers) if answers is not None else answer
                return value if isinstance(value, int) else ord(value)
        def newwin(*args):
            coordinates.append(args)
            return Dialog()
        curses = types.SimpleNamespace(
            curs_set=lambda *a: None, has_colors=lambda: False,
            def_prog_mode=lambda: None, endwin=lambda: None, reset_prog_mode=lambda: None,
            wrapper=lambda run: run(Screen()), newwin=newwin,
            KEY_DOWN=258, KEY_UP=259, KEY_ENTER=343, KEY_MOUSE=409,
            A_BOLD=1, A_DIM=2, A_UNDERLINE=4, A_REVERSE=8, A_NORMAL=0)
        self.S.usage_rows = lambda *a, **k: []
        with patch.object(self.S.BACKENDS["claude"], "read_live", return_value=None), \
                patch.object(self.S.BACKENDS["claude"], "present", return_value=False), \
                patch.object(self.S, "_sync_picker_size", return_value=False), \
                patch.object(self.S, "_emit_donate_link"), \
                patch.dict("sys.modules", {"curses": curses}):
            self.S.cmd_ui([])
        return texts, coordinates

    def test_enter_shows_centered_english_dialog_and_escape_cancels(self):
        before = self.S.BACKENDS["codex"].read_live()
        texts, coordinates = self.picker(27)
        self.assertEqual(self.restarts, 0)
        self.assertEqual(self.S.BACKENDS["codex"].read_live(), before)
        self.assertTrue(any("Switch Codex account" in text for text in texts))
        self.assertIn("enter | esc", texts)
        self.assertIn(f"Switch to {self.S.list_profiles('codex')[1]['label']} ?", texts)
        height, width, top, left = coordinates[0]
        self.assertEqual((top, left), ((30-height)//2, (120-width)//2))

    def test_enter_and_enter_apply_global_reload_without_asking_for_chat_ids(self):
        texts, _ = self.picker(10)
        self.assertEqual(self.restarts, 1)
        self.assertTrue(any("2 chats reloaded" in text for text in texts))
        self.assertFalse(any("Conversation UUID" in text for text in texts))

    def test_picker_explains_daemon_wait_without_clipping_at_80_columns(self):
        settings = self.home / "app-server-daemon" / "settings.json"
        settings.parent.mkdir()
        settings.write_text(json.dumps({"shutdownGraceSeconds": 300}))
        texts, _ = self.picker(10, width=80)
        progress = " ".join(text.rstrip() for text in texts
                            if text.startswith(("Switching Codex account", "(Daemon")))
        self.assertEqual(progress, "Switching Codex account and reloading chats... (Daemon may wait for active work)")
        restarts = [options for argv, options in zip(self.commands, self.command_options)
                    if argv[-1] == "restart"]
        self.assertEqual(len(restarts), 1)
        self.assertEqual(restarts[0]["timeout"], 390,
                         "the plugin must allow the configured grace plus restart overhead")

    def test_restart_wait_matches_official_grace_defaults_and_bounds(self):
        settings = self.home / "app-server-daemon" / "settings.json"
        settings.parent.mkdir()
        cases = [(None, 60), ({}, 60),
                 ({"shutdownGraceSeconds": 0}, 0),
                 ({"shutdownGraceSeconds": 180}, 180),
                 ({"shutdownGraceSeconds": 300}, 300),
                 ({"shutdownGraceSeconds": 301}, 60),
                 ({"shutdownGraceSeconds": -1}, 60),
                 ({"shutdownGraceSeconds": True}, 60),
                 ({"shutdownGraceSeconds": "300"}, 60),
                 ({"shutdownGraceSeconds": 300.0}, 60),
                 ([300], 60)]
        for stored, expected in cases:
            with self.subTest(settings=stored):
                if stored is None:
                    settings.unlink(missing_ok=True)
                else:
                    settings.write_text(json.dumps(stored))
                self.S.switch("codex", self.profiles[1]["id"])
                self.assertEqual(self.command_options[-1]["timeout"], expected + 90)

    def test_old_yes_key_does_not_apply_an_account_change(self):
        self.picker("y")
        self.assertEqual(self.restarts, 0)

    def test_sign_in_action_requires_confirmation_and_addresses_selected_uuid(self):
        with patch.object(self.S, "login_codex", return_value="Codex: saved login") as sign_in:
            self.picker(27, action=ord("a"))
            sign_in.assert_not_called()
            self.picker(10, action=ord("a"))
            sign_in.assert_called_once_with(
                profile_id=self.S.list_profiles("codex")[1]["id"], label=None, browser=False)
        self.assertEqual(self.restarts, 0, "sign-in waits for Enter before applying an account")


if __name__ == "__main__":
    unittest.main()
