#!/usr/bin/env python3
"""Account registration must never revoke the shared Codex login."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tests.test_account_records import login


class _LoginFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hub-login-test-")
        self.home = Path(self.temp.name, "shared-history")
        self.home.mkdir(mode=0o700)
        self.env = patch.dict(os.environ, {
            "CODEX_HOME": str(self.home), "CODEX_SQLITE_HOME": str(self.home),
            "ACCOUNT_HUB_STATE_DIR": str(Path(self.temp.name, "private")),
            "ACCOUNT_HUB_CONFIG_DIR": str(Path(self.temp.name, "config")),
        })
        self.env.start()
        spec = importlib.util.spec_from_file_location("login_hub", Path(__file__).resolve().parents[1] / "switcher.py")
        self.S = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.S)
        self.S.notify = lambda *a: None
        self.current = login("one", "working", "one")
        self.S.BACKENDS["codex"].write_live(self.current)
        (self.home / "custom-history-marker").write_text("custom titles and existing history")
        self.profile = self.S.make_profile("codex", "One", self.current)
        self.S.write_profile(self.profile)
        self.S._select_profile("codex", self.profile["id"])
        self.new = login("two", "fresh", "two")
        self.login_homes = []

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def official_login(self, argv, **kwargs):
        home = Path(kwargs["env"]["CODEX_HOME"])
        self.login_homes.append(home)
        # The real CLI revokes whichever auth file exists before its login.
        self.assertFalse((home / "auth.json").exists(), "login would revoke an existing account")
        self.assertNotEqual(home, self.home)
        self.assertEqual(home.stat().st_mode & 0o777, 0o700)
        self.assertEqual(Path(kwargs["env"]["CODEX_SQLITE_HOME"]), home)
        self.assertIn("login", argv)
        self.assertIn('cli_auth_credentials_store="file"', argv)
        self.assertEqual(kwargs.get("timeout"), 15 * 60)
        self.assertNotIn("working", str(argv))
        (home / "auth.json").write_text(json.dumps(self.new["auth"]))
        return subprocess.CompletedProcess(argv, 0)

class CodexLogin(_LoginFixture):
    def test_add_new_skips_an_email_already_saved_without_replacing_it(self):
        self.new = login("one", "another-grant", "another-session")
        with patch.object(self.S.subprocess, "run", side_effect=self.official_login):
            message = self.S.login_codex(label="One again")
        profiles = self.S.list_profiles("codex")
        self.assertEqual(len(profiles), 1)
        self.assertIn("already saved", message)
        self.assertEqual(self.S.get_profile("codex", self.profile["id"]), self.profile)
        self.assertEqual(self.S.BACKENDS["codex"].read_live(), self.current)
        self.assertEqual(self.S._selection()["codex"], self.profile["id"])

    def test_add_accounts_uses_empty_temporary_login_store_and_keeps_shared_history(self):
        before = self.S.BACKENDS["codex"].read_live()
        with patch.object(self.S.subprocess, "run", side_effect=self.official_login):
            self.S.login_codex(label="Two")
            self.new = login("three", "third", "three")
            self.S.login_codex(label="Three")
        profiles = self.S.list_profiles("codex")
        self.assertEqual(len(profiles), 3)
        self.assertEqual(len({p["id"] for p in profiles}), 3)
        self.assertEqual(self.S.BACKENDS["codex"].read_live(), before)
        self.assertEqual(self.S._selection()["codex"], self.profile["id"])
        self.assertEqual((self.home / "custom-history-marker").read_text(), "custom titles and existing history")
        self.assertTrue(all(not home.exists() for home in self.login_homes))

    def test_reauthentication_addresses_only_selected_uuid_and_does_not_apply_it(self):
        target = self.S.make_profile("codex", "Two", login("two", "old", "two"))
        self.S.write_profile(target)
        with patch.object(self.S.subprocess, "run", side_effect=self.official_login):
            self.S.login_codex(profile_id=target["id"])
        after = self.S.get_profile("codex", target["id"])
        self.assertEqual(after["id"], target["id"])
        self.assertEqual(after["slug"], target["slug"])
        self.assertEqual(after["label"], "Two")
        self.assertEqual(after["payload"], self.new)
        self.assertEqual(self.S.get_profile("codex", self.profile["id"])["payload"], self.current)
        self.assertEqual(self.S._selection()["codex"], self.profile["id"])
        self.assertEqual(self.S.BACKENDS["codex"].read_live(), self.current)

    def test_wrong_person_in_same_team_does_not_replace_registration(self):
        with patch.object(self.S.subprocess, "run", side_effect=self.official_login):
            with self.assertRaises(self.S.SwitchError):
                self.S.login_codex(profile_id=self.profile["id"])
        self.assertEqual(self.S.get_profile("codex", self.profile["id"])["payload"], self.current)
        self.assertEqual(self.S.BACKENDS["codex"].read_live(), self.current)

    def test_cancelled_login_does_not_write_a_profile_or_selection(self):
        with patch.object(self.S.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)):
            with self.assertRaises(self.S.SwitchError):
                self.S.login_codex(label="Two")
        self.assertEqual(len(self.S.list_profiles("codex")), 1)
        self.assertEqual(self.S.BACKENDS["codex"].read_live(), self.current)

    def test_timeout_or_interrupt_never_registers_partial_login_and_cleans_temp_store(self):
        for failure in (subprocess.TimeoutExpired(["codex", "login"], 900), KeyboardInterrupt()):
            def unfinished(argv, **kwargs):
                self.official_login(argv, **kwargs)
                raise failure
            with self.subTest(failure=type(failure).__name__), \
                    patch.object(self.S.subprocess, "run", side_effect=unfinished):
                with self.assertRaises(self.S.SwitchError) as caught:
                    self.S.login_codex(label="Incomplete")
            self.assertNotIn("fresh", str(caught.exception))
            self.assertEqual(len(self.S.list_profiles("codex")), 1)
            self.assertEqual(self.S.BACKENDS["codex"].read_live(), self.current)
            self.assertTrue(all(not home.exists() for home in self.login_homes))

    def test_timeout_stops_a_real_local_cli_process_and_returns_without_a_registration(self):
        marker = Path(self.temp.name, "fake-login.pid")
        fake = Path(self.temp.name, "fake-codex")
        fake.write_text("#!" + sys.executable + "\nimport os, time\n"
                        "from pathlib import Path\n"
                        "Path(" + repr(str(marker)) + ").write_text(str(os.getpid()))\n"
                        "time.sleep(60)\n")
        fake.chmod(0o700)
        with patch.object(self.S, "CODEX", str(fake)), \
                patch.object(self.S, "LOGIN_TIMEOUT_S", 0.2):
            with self.assertRaises(self.S.SwitchError) as caught:
                self.S.login_codex(label="Incomplete")
        self.assertIn("timed out", str(caught.exception))
        self.assertTrue(marker.exists(), "local CLI must have started")
        with self.assertRaises(ProcessLookupError):
            os.kill(int(marker.read_text()), 0)
        self.assertEqual(len(self.S.list_profiles("codex")), 1)
        self.assertEqual(list(Path(self.S.STATE_DIR).glob("codex-login-*")), [])
        self.assertEqual(self.S.BACKENDS["codex"].read_live(), self.current)

    def test_duplicate_names_display_saved_time_in_picker(self):
        first = self.S.get_profile("codex", self.profile["id"])
        first["saved_at"] = 1791584064
        self.S.write_profile(first)
        second = self.S.make_profile("codex", "One", login("one", "new", "new-session"))
        second["saved_at"] = 1791584809
        self.S.write_profile(second)
        rows = [row[2] for row in self.S._rows() if row[0] == "profile" and row[1] == "codex"]
        rendered = [self.S._profile_line("codex", row, 120) for row in rows]
        self.assertTrue(all("saved" in text and "UTC" in text for text in rendered))
        self.assertNotEqual(rendered[0], rendered[1])

    def test_renewal_persists_rfc3339_timestamp_accepted_by_codex(self):
        from datetime import datetime
        self.S._post_json = lambda *a: {"access_token": "renewed", "refresh_token": "rotated"}
        payload = self.S.renew_profile("codex", self.profile, force=True)
        stamp = payload["auth"]["last_refresh"]
        self.assertIsNotNone(datetime.fromisoformat(stamp.replace("Z", "+00:00")).tzinfo)

    def test_refusal_preserves_provider_code_and_reauthentication_guidance(self):
        import io
        import urllib.error
        self.S.fetch_usage = lambda *a: (_ for _ in ()).throw(urllib.error.HTTPError(
            "https://example.invalid", 401, "refused", {}, io.BytesIO(b'{}')))
        codes = ("refresh_token_invalidated", "token_invalidated", "token_revoked",
                 "refresh_token_reused", "refresh_token_expired", "invalid_grant",
                 "invalid_token", "token_expired")
        for code in codes:
            with self.subTest(code=code):
                body = json.dumps({"error": {"code": code, "message": "sensitive detail"}}).encode()
                error = urllib.error.HTTPError("https://example.invalid", 401, "refused", {},
                                               io.BytesIO(body))
                with patch.object(self.S, "_post_json", side_effect=error):
                    with self.assertRaises(self.S.SwitchError) as caught:
                        self.S.proven_payload("codex", self.profile)
                self.assertEqual(str(caught.exception),
                    f"Codex: One: {code} — active account unchanged; "
                    "press a to sign in to this registration again")
                key = "codex:" + self.profile["id"]
                self.assertEqual(self.S._usage_cache()[key]["login_problem"], code)
                self.assertEqual(self.S.BACKENDS["codex"].read_live(), self.current)
                self.assertEqual(self.S._selection()["codex"], self.profile["id"])

    def test_refusal_without_known_provider_code_shows_http_status_without_raw_body(self):
        import io
        import urllib.error
        bodies = (b'{}', b'not JSON',
                  b'{"error":{"code":"sensitive-code","message":"sensitive detail"}}')
        self.S.fetch_usage = lambda *a: (_ for _ in ()).throw(urllib.error.HTTPError(
            "https://example.invalid", 401, "refused", {}, io.BytesIO(b'{}')))
        for body in bodies:
            with self.subTest(body=body):
                error = urllib.error.HTTPError("https://example.invalid", 401, "refused", {},
                                               io.BytesIO(body))
                with patch.object(self.S, "_post_json", side_effect=error):
                    with self.assertRaises(self.S.SwitchError) as caught:
                        self.S.proven_payload("codex", self.profile)
                self.assertEqual(str(caught.exception),
                    "Codex: One: HTTP 401 — active account unchanged; "
                    "press a to sign in to this registration again")


@unittest.skipUnless(os.environ.get("ACCOUNT_HUB_TEST_REAL_CODEX") == "1",
                     "optional local-only test of official CLI revocation")
class RealCodexLogin(_LoginFixture):
    # Keep the native check separate from inherited fixture tests.
    def test_native_login_revokes_existing_store_but_plugin_login_preserves_it(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        revoked = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                revoked.append(self.path)
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()
        fixture = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=fixture.serve_forever, daemon=True).start()
        self.addCleanup(fixture.server_close)
        self.addCleanup(fixture.shutdown)
        real_run = subprocess.run
        with patch.dict(os.environ, {
            "CODEX_REVOKE_TOKEN_URL_OVERRIDE": f"http://127.0.0.1:{fixture.server_port}/oauth/revoke",
            "HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9",
            "ALL_PROXY": "http://127.0.0.1:9", "NO_PROXY": "127.0.0.1,localhost",
        }):
            # The following auth is fictitious and lives in this test's temp home.
            result = real_run([self.S.CODEX, "login", "--device-auth", "-c",
                               'cli_auth_credentials_store="file"'],
                              cwd=self.temp.name, env=os.environ.copy(),
                              capture_output=True, text=True, timeout=20)
            self.assertNotEqual(result.returncode, 0, "device sign-in must be blocked by the local-only test")
            self.assertIn("/oauth/revoke", revoked)
            self.S.BACKENDS["codex"].write_live(self.current)
            revoked.clear()
            def run_locally(argv, **kwargs):
                kwargs.pop("timeout", None)
                return real_run(argv, **kwargs, capture_output=True, text=True, timeout=20)
            with patch.object(self.S.subprocess, "run", side_effect=run_locally):
                with self.assertRaises(self.S.SwitchError):
                    self.S.login_codex(label="New account")
            self.assertEqual(revoked, [], "staged login must never revoke the working grant")
            self.assertEqual(self.S.BACKENDS["codex"].read_live(), self.current)


if __name__ == "__main__":
    unittest.main()
