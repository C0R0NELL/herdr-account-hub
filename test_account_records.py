#!/usr/bin/env python3
"""UUID registration regressions. All logins and storage are fictitious."""
import base64
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch


def login(user="one", credential="first", session="session-one"):
    claims = {"email": user + "@example.invalid", "sid": session,
              "https://api.openai.com/auth": {
                  "chatgpt_account_id": "shared-team",
                  "chatgpt_user_id": user, "chatgpt_plan_type": "team"}}
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return {"auth": {"tokens": {
        "account_id": "shared-team", "id_token": "fake." + encoded + ".fake",
        "access_token": credential, "refresh_token": credential + "-refresh"}}}


class AccountRecords(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="account-hub-records-")
        with patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": self.temp.name,
                                     "HERDR_PLUGIN_CONFIG_DIR": self.temp.name}):
            spec = importlib.util.spec_from_file_location(
                "isolated_switcher", Path(__file__).with_name("switcher.py"))
            self.S = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.S)
        S = self.S

        class MemoryCodex(S.CodexBackend):
            def __init__(self):
                self.store = None
                self.writes = 0

            def present(self):
                return True

            def read_live(self):
                return copy.deepcopy(self.store)

            def write_live(self, payload):
                self.writes += 1
                self.store = copy.deepcopy(payload)

        class EmptyBackend(S.Backend):
            kind = "claude"
            title = "Empty"

            def present(self):
                return False

            def read_live(self):
                return None

        self.backend = MemoryCodex()
        S.BACKENDS = {"codex": self.backend, "claude": EmptyBackend()}
        S.stamp_badges = lambda: None
        S.notify = lambda *a: None
        S.proven_payload = lambda kind, profile: profile["payload"]
        S.fetch_usage = lambda *a: self.fail("a credential test reached HTTP")
        S._post_json = S.fetch_usage
        S._http_json = S.fetch_usage

    def tearDown(self):
        self.temp.cleanup()

    def register(self, payload=None, label="Work"):
        self.backend.store = payload or login()
        before = {p.get("id") for p in self.S.list_profiles("codex")}
        self.S.save_live("codex", label)
        new = [p for p in self.S.list_profiles("codex") if p.get("id") not in before]
        self.assertEqual(len(new), 1, "each registration must append one record")
        return new[0]

    def test_three_people_in_one_workspace_have_independent_credentials(self):
        for user in ("one", "two", "three"):
            self.backend.store = login(user, user + "-credential", user + "-session")
            self.S.save_live("codex", "Work")
        profiles = self.S.list_profiles("codex")
        self.assertEqual(len(profiles), 3)
        self.assertEqual(len({p["id"] for p in profiles}), 3)
        for profile in profiles:
            self.assertEqual(uuid.UUID(profile["id"]).version, 4)
            path = Path(self.S.PROFILES_DIR, "codex", profile["id"] + ".json")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(profile["identity"]["account_id"], "shared-team")
            self.assertEqual(profile["payload"], login(
                profile["identity"]["user_id"],
                profile["identity"]["user_id"] + "-credential",
                profile["identity"]["user_id"] + "-session"))
        self.assertEqual(Path(self.S.PROFILES_DIR).stat().st_mode & 0o777, 0o700)

    def test_registering_the_same_identity_twice_always_appends(self):
        first = self.register()
        second = self.register()
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(first["payload"], second["payload"])
        self.assertEqual(self.S.active_profile("codex")["id"], second["id"])

    def test_selection_is_persisted_by_uuid_and_only_one_record_is_active(self):
        first = self.register()
        second = self.register()
        self.S.switch("codex", first["id"])
        rows = self.S.usage_rows(["codex"], refresh=False)
        self.assertEqual([r["id"] for r in rows if r["active"]], [first["id"]])
        self.assertEqual(self.S.get_profile("codex", second["id"])["payload"], second["payload"])
        saved = json.loads(Path(self.S.SELECTION).read_text())
        self.assertEqual(saved["codex"], first["id"])
        self.assertEqual(Path(self.S.SELECTION).stat().st_mode & 0o777, 0o600)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.S.cmd_list(["--kind", "codex"])
        listing = json.loads(output.getvalue())
        self.assertEqual([r["id"] for r in listing if r["active"]], [first["id"]])
        self.assertNotIn("access_token", output.getvalue())
        self.assertNotIn("refresh_token", output.getvalue())

    def test_duplicate_labels_are_ambiguous_but_uuid_is_unambiguous(self):
        first = self.register()
        self.register()
        with self.assertRaises(self.S.SwitchError):
            self.S.get_profile("codex", "Work")
        self.assertEqual(self.S.get_profile("codex", first["id"])["id"], first["id"])

    def test_switch_same_identity_installs_selected_records_credentials(self):
        first = self.register(login(credential="first", session="one"))
        second = self.register(login(credential="second", session="two"))
        message = self.S.switch("codex", first["id"])
        self.assertIn("switched", message)
        self.assertEqual(self.backend.store, first["payload"])
        self.assertEqual(self.S.active_profile("codex")["id"], first["id"])
        self.assertEqual(self.S.get_profile("codex", second["id"])["payload"], second["payload"])

    def test_sync_only_updates_explicitly_selected_record(self):
        first = self.register()
        second = self.register()
        self.backend.store = login(credential="rotated")
        self.assertTrue(self.S.sync_live_profile("codex"))
        self.assertEqual(self.S.get_profile("codex", first["id"])["payload"], first["payload"])
        self.assertEqual(self.S.get_profile("codex", second["id"])["payload"], self.backend.store)

    def test_other_person_in_same_workspace_never_replaces_selected_record(self):
        profile = self.register()
        self.backend.store = login("two", "other", "other-session")
        self.assertFalse(self.S.sync_live_profile("codex"))
        self.assertIsNone(self.S.active_profile("codex"))
        self.assertEqual(self.S.get_profile("codex", profile["id"])["payload"], profile["payload"])

    def test_fresh_login_requires_explicit_reauthentication_even_same_identity(self):
        profile = self.register()
        self.backend.store = login(credential="re-login", session="different-session")
        self.assertFalse(self.S.sync_live_profile("codex"))
        self.assertEqual(self.S.get_profile("codex", profile["id"])["payload"], profile["payload"])

    def test_no_selection_means_no_implicit_identity_upsert(self):
        profile = self.register()
        Path(self.S.SELECTION).unlink()
        self.backend.store = login(credential="rotated")
        self.assertFalse(self.S.sync_live_profile("codex"))
        self.assertIsNone(self.S.active_profile("codex"))
        self.assertEqual(self.S.get_profile("codex", profile["id"])["payload"], profile["payload"])

    def test_reauthentication_only_replaces_explicit_uuid(self):
        first = self.register()
        second = self.register()
        self.backend.store = login("two", "new-login", "new-session")
        self.S.reauthenticate("codex", first["id"])
        changed = self.S.get_profile("codex", first["id"])
        self.assertEqual(changed["id"], first["id"])
        self.assertEqual(changed["label"], first["label"])
        self.assertEqual(changed["payload"], self.backend.store)
        self.assertEqual(self.S.active_profile("codex")["id"], first["id"])
        self.assertEqual(self.S.get_profile("codex", second["id"]), second)
        self.assertEqual(self.backend.writes, 0, "reauth captures, it must not alter the live login")

    def test_reauthentication_rejects_label_and_missing_uuid_without_writes(self):
        profile = self.register()
        for key in ("Work", "", str(uuid.uuid4()), "../../auth"):
            with self.assertRaises(self.S.SwitchError):
                self.S.reauthenticate("codex", key)
        self.assertEqual(self.S.get_profile("codex", profile["id"]), profile)

    def test_rename_preserves_uuid_selection_and_credential_path(self):
        profile = self.register()
        path = Path(self.S.PROFILES_DIR, "codex", profile["id"] + ".json")
        key = "codex:" + profile["id"]
        self.S._remember_usage(key, [{"label": "session", "percent": 40}])
        cache = self.S._usage_cache()
        self.S.rename_profile("codex", profile["id"], "New label")
        renamed = self.S.get_profile("codex", profile["id"])
        self.assertEqual(renamed["label"], "New label")
        self.assertEqual(renamed["payload"], profile["payload"])
        self.assertTrue(path.exists())
        self.assertEqual(self.S.active_profile("codex")["id"], profile["id"])
        self.assertEqual(self.S._usage_cache(), cache)

    def test_reauthentication_invalidates_only_its_usage_and_staged_tokens(self):
        first = self.register()
        second = self.register()
        a, b = "codex:" + first["id"], "codex:" + second["id"]
        self.S._remember_usage(a, [{"label": "session", "percent": 20}])
        self.S._remember_usage(b, [{"label": "session", "percent": 70}])
        other_cache = self.S._usage_cache()[b]
        self.S._stage("codex", first["id"], login(credential="old-staged"))
        self.S._stage("codex", second["id"], login(credential="other-staged"))
        self.backend.store = login("two", "new", "new")
        self.S.reauthenticate("codex", first["id"])
        self.assertNotIn(a, self.S._usage_cache())
        self.assertEqual(self.S._usage_cache()[b], other_cache)
        self.assertFalse(Path(self.S._staged_path("codex", first["id"])).exists())
        self.assertTrue(Path(self.S._staged_path("codex", second["id"])).exists())

    def test_deleting_one_uuid_leaves_other_records_and_live_login_alone(self):
        first = self.register()
        second = self.register()
        self.assertTrue(self.S.delete_profile("codex", second["id"]))
        self.assertIsNone(self.S.active_profile("codex"))
        self.assertEqual(self.S.get_profile("codex", first["id"]), first)
        self.assertEqual(self.backend.store, second["payload"])
        self.assertFalse(self.S.delete_profile("codex", "../../auth"))

    def test_failed_switch_does_not_change_selected_uuid(self):
        first = self.register(login(credential="one", session="one"), "One")
        second = self.register(login(credential="two", session="two"), "Two")
        self.backend.write_live = lambda payload: None
        with self.assertRaises(self.S.SwitchError):
            self.S.switch("codex", first["id"])
        self.assertEqual(self.S.active_profile("codex")["id"], second["id"])

    def test_codex_without_session_claims_does_not_guess_after_token_replacement(self):
        payload = login()
        payload["auth"]["tokens"]["id_token"] = ""
        first = self.register(payload)
        replacement = copy.deepcopy(payload)
        replacement["auth"]["tokens"]["access_token"] = "replaced-access"
        replacement["auth"]["tokens"]["refresh_token"] = "replaced-refresh"
        self.backend.store = replacement
        self.assertFalse(self.S.sync_live_profile("codex"))
        self.assertEqual(self.S.get_profile("codex", first["id"])["payload"], payload)

    def test_existing_profile_is_read_without_rewriting_or_moving_credentials(self):
        directory = Path(self.S.PROFILES_DIR, "codex")
        directory.mkdir(parents=True)
        path = directory / "work.json"
        path.write_text(json.dumps({"kind": "codex", "slug": "work", "label": "Work",
                                    "payload": login(), "saved_at": 1}))
        before = path.read_bytes()
        first = self.S.list_profiles("codex")[0]
        second = self.S.list_profiles("codex")[0]
        self.assertEqual(first["id"], second["id"])
        uuid.UUID(first["id"])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(directory.iterdir()), [path])


if __name__ == "__main__":
    unittest.main()
