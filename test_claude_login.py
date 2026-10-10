#!/usr/bin/env python3
"""Automatic Claude registration with fake CLI output; never a real login."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


class ClaudeLogin(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hub-claude-login-test-")
        self.env = patch.dict(os.environ, {
            "CODEX_HOME": str(Path(self.temp.name, "codex")),
            "CLAUDE_CONFIG_DIR": str(Path(self.temp.name, "shared-claude")),
            "ACCOUNT_HUB_STATE_DIR": str(Path(self.temp.name, "private")),
            "ACCOUNT_HUB_CONFIG_DIR": str(Path(self.temp.name, "config")),
            "ANTHROPIC_API_KEY": "fake-existing-api-key",
            "ANTHROPIC_AUTH_TOKEN": "fake-existing-auth-token",
            "CLAUDE_CODE_OAUTH_TOKEN": "fake-existing-oauth-token",
        })
        self.env.start()
        spec = importlib.util.spec_from_file_location("claude_login_hub", Path(__file__).with_name("switcher.py"))
        self.S = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.S)
        self.S.IS_MAC = False
        self.S.notify = lambda *args: None
        self.current = self.payload("person-one", "live")
        self.S.BACKENDS["claude"].write_live(self.current)
        self.profile = self.S.make_profile("claude", "Existing", self.current)
        self.S.write_profile(self.profile)
        self.S._select_profile("claude", self.profile["id"])
        self.homes = []
        self.new = self.payload("person-two", "fresh")

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    @staticmethod
    def payload(person, token):
        return {"store": "file", "credentials": {"claudeAiOauth": {
                    "accessToken": token, "refreshToken": "refresh-" + token,
                    "subscriptionType": "team", "expiresAt": 9999999999999}},
                "oauth_account": {"accountUuid": person, "emailAddress": person + "@example.invalid",
                                  "organizationUuid": "same-team", "organizationName": "Same team"}}

    def official_login(self, argv, **kwargs):
        home = Path(kwargs["env"]["CLAUDE_CONFIG_DIR"])
        self.homes.append(home)
        self.assertNotEqual(home, Path(self.S.BACKENDS["claude"].home))
        self.assertFalse((home / ".credentials.json").exists())
        self.assertEqual(home.stat().st_mode & 0o777, 0o700)
        self.assertEqual(argv[-3:], ["auth", "login", "--claudeai"])
        self.assertEqual(kwargs.get("timeout"), 900)
        for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
            self.assertNotIn(name, kwargs["env"])
        (home / ".credentials.json").write_text(json.dumps(self.new["credentials"]))
        (home / ".claude.json").write_text(json.dumps({"oauthAccount": self.new["oauth_account"]}))
        return subprocess.CompletedProcess(argv, 0)

    def test_login_registers_uuid_and_saves_automatically_without_changing_live_account(self):
        with patch.object(self.S.subprocess, "run", side_effect=self.official_login):
            result = self.S.login_claude(label="New Claude")
        profiles = self.S.list_profiles("claude")
        self.assertEqual(len(profiles), 2)
        added = next(p for p in profiles if p["label"] == "New Claude")
        self.assertEqual(added["payload"], self.new)
        self.assertNotEqual(added["id"], self.profile["id"])
        self.assertEqual(self.S.BACKENDS["claude"].read_live(), self.current)
        self.assertEqual(self.S._selection()["claude"], self.profile["id"])
        self.assertIn("saved login", result)
        self.assertTrue(all(not home.exists() for home in self.homes))

    def test_people_in_shared_team_and_repeated_names_never_replace_each_other(self):
        with patch.object(self.S.subprocess, "run", side_effect=self.official_login):
            self.S.login_claude(label="Team")
            self.new = self.payload("person-three", "third")
            self.S.login_claude(label="Team")
        profiles = self.S.list_profiles("claude")
        self.assertEqual(len(profiles), 3)
        self.assertEqual(len({p["id"] for p in profiles}), 3)

    def test_timeout_cancel_and_failed_cli_do_not_register_partial_credentials(self):
        for failure in (subprocess.TimeoutExpired(["claude", "auth", "login"], 900), KeyboardInterrupt(), None):
            def unfinished(argv, **kwargs):
                self.official_login(argv, **kwargs)
                if failure is not None:
                    raise failure
                return subprocess.CompletedProcess(argv, 1)
            with self.subTest(failure=type(failure).__name__), \
                    patch.object(self.S.subprocess, "run", side_effect=unfinished):
                with self.assertRaises(self.S.SwitchError):
                    self.S.login_claude(label="Incomplete")
            self.assertEqual(len(self.S.list_profiles("claude")), 1)
            self.assertEqual(self.S.BACKENDS["claude"].read_live(), self.current)
            self.assertTrue(all(not home.exists() for home in self.homes))

    def test_unrecognized_credentials_do_not_register_a_wrong_account(self):
        self.new = {"store": "file", "credentials": {"apiKey": "fake-key"}, "oauth_account": {}}
        with patch.object(self.S.subprocess, "run", side_effect=self.official_login):
            with self.assertRaises(self.S.SwitchError):
                self.S.login_claude()
        self.assertEqual(len(self.S.list_profiles("claude")), 1)

    def test_macos_beta_does_not_read_or_overwrite_shared_keychain(self):
        self.S.IS_MAC = True
        with patch.object(self.S.subprocess, "run") as run:
            with self.assertRaises(self.S.SwitchError) as caught:
                self.S.login_claude(label="Mac")
        self.assertIn("macOS", str(caught.exception))
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
