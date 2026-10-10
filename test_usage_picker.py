#!/usr/bin/env python3
"""Account-list navigation and usage isolation with fictitious logins only."""
import importlib.util
import copy
import io
import os
from pathlib import Path
import tempfile
import types
import unittest
import urllib.error
from unittest.mock import patch

from test_account_records import login


class UsagePicker(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hub-usage-picker-")
        self.env = patch.dict(os.environ, {
            "CODEX_HOME": str(Path(self.temp.name, "codex")),
            "CLAUDE_CONFIG_DIR": str(Path(self.temp.name, "claude")),
            "ACCOUNT_HUB_STATE_DIR": str(Path(self.temp.name, "state")),
            "ACCOUNT_HUB_CONFIG_DIR": self.temp.name,
        })
        self.env.start()
        spec = importlib.util.spec_from_file_location(
            "usage_picker_hub", Path(__file__).with_name("switcher.py"))
        self.S = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.S)
        S = self.S
        S.notify = lambda *args: None
        S.codex_application_status = lambda: "ready for new chats"
        S.BACKENDS["claude"].present = lambda: False
        S.BACKENDS["claude"].read_live = lambda: None
        self.profiles = []
        for number, name in enumerate(("Alpha", "Bravo", "Charlie", "Delta", "Echo")):
            profile = S.make_profile("codex", name, login(name, name, name))
            profile["created_at"] = number + 1
            S.write_profile(profile)
            self.profiles.append(profile)
        self.reads = []
        self.percent = {p["label"]: 10 + n * 15 for n, p in enumerate(self.profiles)}

        def http(url, headers):
            credential = headers["Authorization"].removeprefix("Bearer ")
            self.reads.append((credential, headers.get("chatgpt-account-id")))
            return {"rate_limit": {"primary_window": {
                "used_percent": self.percent[credential], "limit_window_seconds": 18000,
                "reset_after_seconds": 3600}}}

        S._http_json = http
        S._post_json = lambda *args: self.fail("fixture unexpectedly attempted renewal")

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def picker(self, actions, height=12, read_usage=True, width=140):
        frames = []
        keys = iter(actions)
        test = self

        class Screen:
            def __init__(self, size=None):
                self.size = size or (height, width)
                self.erase()
            def getmaxyx(self): return self.size
            def timeout(self, *args): pass
            def keypad(self, *args): pass
            def box(self): pass
            def touchwin(self): pass
            def erase(self): self.lines, self.selected, self.paints = {}, [], []
            def refresh(self):
                frames.append({"lines": dict(self.lines), "selected": list(self.selected),
                               "paints": list(self.paints), "modal": self.size != (height, width)})
            def addnstr(self, y, x, text, count, attr=0):
                test.assertTrue(0 <= y < self.size[0] and 0 <= x < self.size[1])
                text = text[:count]
                old = self.lines.get(y, "").ljust(self.size[1])
                self.lines[y] = old[:x] + text + old[x + len(text):]
                self.paints.append((y, x, text, attr))
                if attr & 8:
                    self.selected.append(text)
            def getch(self):
                action = next(keys)
                if callable(action):
                    return action()
                return action

        curses = types.SimpleNamespace(
            curs_set=lambda *args: None, has_colors=lambda: False,
            wrapper=lambda run: run(Screen()), KEY_DOWN=258, KEY_UP=259,
            newwin=lambda h, w, y, x: Screen((h, w)),
            KEY_ENTER=343, A_BOLD=1, A_DIM=2, A_UNDERLINE=4,
            A_REVERSE=8, A_NORMAL=0)
        with patch.dict("sys.modules", {"curses": curses}):
            if read_usage:
                self.S.cmd_ui([])
            else:
                with patch.object(self.S, "usage_rows", return_value=[]):
                    self.S.cmd_ui([])
        return frames

    def test_scroll_reaches_fifth_account_in_small_picker(self):
        frames = self.picker([258] * 4 + [ord("q")], height=9)
        self.assertTrue(any("Echo" in text for text in frames[-1]["selected"]), frames[-1])
        self.assertIn("5 accounts", frames[-1]["lines"][0])

    def test_expanded_usage_scrolls_to_selected_account(self):
        frames = self.picker([ord("d")] + [258] * 4 + [ord("q")], height=14)
        self.assertTrue(any("Echo" in text for text in frames[-1]["selected"]), frames[-1])
        self.assertTrue(any("70%" in text for text in frames[-1]["lines"].values()), frames[-1])

    def test_refresh_reordering_preserves_selected_uuid(self):
        legacy = self.profiles[0]
        legacy.pop("created_at")
        legacy["saved_at"] = 0.5
        self.S.write_profile(legacy)

        def move_legacy_record():
            legacy["created_at"] = 100
            self.S.write_profile(legacy)
            return -1

        frames = self.picker([258, move_legacy_record, ord("q")], read_usage=False)
        self.assertTrue(any("Bravo" in text for text in frames[-1]["selected"]), frames[-1])

    def test_legacy_record_does_not_move_when_its_credentials_are_renewed(self):
        legacy = self.profiles[0]
        legacy.pop("created_at")
        legacy["saved_at"] = 0.5
        self.S.write_profile(legacy)
        before = [profile["id"] for profile in self.S.list_profiles("codex")]
        legacy["saved_at"] = 100
        self.S.write_profile(legacy)
        self.assertEqual([profile["id"] for profile in self.S.list_profiles("codex")], before)

    def test_new_registrations_are_read_without_waiting_for_old_cache_ttl(self):
        extra = self.profiles[-2:]
        for profile in extra:
            Path(self.S.PROFILES_DIR, "codex", profile["slug"] + ".json").unlink()

        def add_registrations():
            for profile in extra:
                self.S.write_profile(profile)
            return -1

        frames = self.picker([add_registrations, ord("q")], height=14)
        self.assertEqual({token for token, workspace in self.reads}, set(self.percent))
        self.assertIn("5 accounts", frames[-1]["lines"][0])

    def test_five_same_workspace_accounts_keep_their_own_usage(self):
        for profile in self.profiles:
            profile["label"] = "Work"
            self.S.write_profile(profile)
        rows = self.S.usage_rows(["codex"])
        self.assertEqual(len(rows), 5)
        self.assertEqual({workspace for token, workspace in self.reads}, {"shared-team"})
        expected = {p["id"]: self.percent[p["payload"]["auth"]["tokens"]["access_token"]]
                    for p in self.profiles}
        for row in self.S.usage_rows(["codex"], refresh=False):
            self.assertEqual(row["windows"][0]["percent"], expected[row["id"]])
        self.assertEqual(len(self.reads), 5)

    def test_opening_refreshes_recent_cache_that_has_no_reset_counts(self):
        clock = [self.S.time.time()]
        with patch.object(self.S.time, "time", side_effect=lambda: clock[0]):
            self.S.usage_rows(["codex"])
            # Simulate a fresh cache written before reset support existed.
            cache = self.S._usage_cache()
            for entry in cache.values():
                entry.pop("reset_credits", None)
                entry.pop("reset_credits_at", None)
            self.S._write_json_secret(self.S.USAGE_CACHE, cache)
            self.reads.clear()
            self.reset_provider()
            self.percent["Alpha"] = 86

            def idle_tick():
                clock[0] += 5
                return -1

            frames = self.picker([idle_tick, 27], height=18)
        column = self.S.USAGE_COL + self.S.USAGE_W + self.S.RESETS_GAP
        for profile in self.profiles:
            line = next(line for line in frames[0]["lines"].values()
                        if profile["label"] in line)
            self.assertIn("%d%%" % self.percent[profile["label"]], line)
            self.assertEqual(line[column:column + self.S.RESETS_W].strip(), "2 resets")
        self.assertCountEqual([token for token, _ in self.reads], self.percent)

    def test_reopening_refreshes_usage_and_counts_even_with_a_recent_full_cache(self):
        _, banks = self.reset_provider()
        self.picker([27], height=18)
        self.reads.clear()
        self.percent["Alpha"] = 73
        banks["Alpha"] = banks["Alpha"][:1]
        frames = self.picker([27], height=18)
        line = next(line for line in frames[0]["lines"].values() if "Alpha" in line)
        column = self.S.USAGE_COL + self.S.USAGE_W + self.S.RESETS_GAP
        self.assertIn("73%", line)
        self.assertEqual(line[column:column + self.S.RESETS_W].strip(), "1 reset")
        self.assertCountEqual([token for token, _ in self.reads], self.percent)

    def test_opening_refreshes_other_accounts_but_respects_provider_cooldown(self):
        self.S.usage_rows(["codex"])
        self.S._rest_usage("codex:" + self.profiles[0]["id"], 900)
        self.reads.clear()
        self.reset_provider()
        frames = self.picker([27], height=18)
        column = self.S.USAGE_COL + self.S.USAGE_W + self.S.RESETS_GAP
        first = next(line for line in frames[0]["lines"].values() if "Alpha" in line)
        self.assertEqual(first[column:column + self.S.RESETS_W].strip(), "-")
        self.assertEqual(first[1], "!")
        self.assertCountEqual([token for token, _ in self.reads],
                              [name for name in self.percent if name != "Alpha"])
        for name in ("Bravo", "Charlie", "Delta", "Echo"):
            line = next(line for line in frames[0]["lines"].values() if name in line)
            self.assertEqual(line[column:column + self.S.RESETS_W].strip(), "2 resets")

    def test_u_chooses_provider_and_starts_automatic_registration_without_save(self):
        self.S.usage_rows(["codex"])
        for provider in ("codex", "claude"):
            self.reads.clear()
            with self.subTest(provider=provider), \
                    patch.object(self.S, "_choose", return_value=provider), \
                    patch.object(self.S, "_ask", return_value="New account"), \
                    patch.object(self.S, "_confirm", return_value=True), \
                    patch.object(self.S, "_ui_login", return_value="saved automatically") as signin, \
                    patch.object(self.S, "save_live") as save:
                frames = self.picker([ord("u"), 27], height=14)
                signin.assert_called_once_with(unittest.mock.ANY, provider, label="New account")
                save.assert_not_called()
                self.assertIn("'u' add new", " ".join(frames[-1]["lines"].values()))
                self.assertCountEqual([token for token, _ in self.reads], self.percent,
                                      "only opening, not u, should force a usage refresh")

    def test_provider_modal_supports_arrows_enter_and_escape(self):
        for keys, expected in (([258, 10], "claude"), ([258, 259, 10], "codex")):
            with self.subTest(expected=expected), \
                    patch.object(self.S, "_ask", return_value="New"), \
                    patch.object(self.S, "_confirm", return_value=True), \
                    patch.object(self.S, "_ui_login", return_value="saved") as signin:
                frames = self.picker([ord("u")] + keys + [27], height=14)
                self.assertEqual(signin.call_args.args[1], expected)
                modal = [frame for frame in frames if frame["modal"]]
                self.assertTrue(any("Claude (beta)" in text
                                    for frame in modal for text in frame["lines"].values()))
        with patch.object(self.S, "_ui_login") as signin:
            self.picker([ord("u"), 27, 27], height=14)
            signin.assert_not_called()

    def test_canceling_label_or_confirmation_does_not_start_login(self):
        for label, confirm in ((None, True), ("New account", False)):
            with self.subTest(label=label, confirm=confirm), \
                    patch.object(self.S, "_choose", return_value="codex"), \
                    patch.object(self.S, "_ask", return_value=label), \
                    patch.object(self.S, "_confirm", return_value=confirm), \
                    patch.object(self.S, "_ui_login") as signin:
                self.picker([ord("u"), 27], height=14)
                signin.assert_not_called()

    def test_blank_label_uses_automatic_name_instead_of_canceling(self):
        with patch.object(self.S, "_choose", return_value="codex"), \
                patch.object(self.S, "_ask", return_value=""), \
                patch.object(self.S, "_confirm", return_value=True), \
                patch.object(self.S, "_ui_login", return_value="saved") as signin:
            self.picker([ord("u"), 27], height=14)
            signin.assert_called_once_with(unittest.mock.ANY, "codex", label=None)

    def test_login_failure_returns_to_picker_without_changing_existing_accounts(self):
        before = [p["id"] for p in self.S.list_profiles("codex")]
        with patch.object(self.S, "_choose", return_value="codex"), \
                patch.object(self.S, "_ask", return_value="New"), \
                patch.object(self.S, "_confirm", return_value=True), \
                patch.object(self.S, "_ui_login", side_effect=self.S.SwitchError("sign-in timed out")):
            frames = self.picker([ord("u"), 27], height=14)
        self.assertEqual([p["id"] for p in self.S.list_profiles("codex")], before)
        self.assertIn("sign-in timed out", frames[-1]["lines"][13])

    def test_failed_accounts_show_red_exclamation_even_when_selected_or_expanded(self):
        self.S.usage_rows(["codex"])
        native = self.S._http_json
        errors = {
            "Alpha": urllib.error.HTTPError("https://example.invalid", 401, "refused", {},
                                             io.BytesIO(b"{}")),
            "Bravo": urllib.error.HTTPError("https://example.invalid", 429, "throttled", {},
                                             io.BytesIO(b"{}")),
            "Charlie": urllib.error.URLError("offline"),
        }
        def failed_reads(url, headers):
            token = headers["Authorization"].removeprefix("Bearer ")
            if token in errors:
                raise errors[token]
            return native(url, headers)
        self.S._http_json = failed_reads
        self.S.USAGE_RENEW = False
        with patch.object(self.S, "_usage_pairs", return_value={"error": 256}):
            frames = self.picker([ord("d"), 27], height=30)
        for frame in (frames[0], frames[-1]):
            for name in ("Alpha", "Bravo", "Charlie"):
                y = next(y for y, text in frame["lines"].items() if name in text)
                self.assertEqual(frame["lines"][y][1], "!", frame)
                self.assertIn((y, 1, "!", 257), frame["paints"])
            healthy = next(text for text in frame["lines"].values() if "Delta" in text)
            self.assertNotEqual(healthy[1], "!")

    def test_successful_refresh_clears_transient_failure_marker(self):
        self.S.usage_rows(["codex"])
        native = self.S._http_json
        def offline(*args):
            raise urllib.error.URLError("offline")
        self.S._http_json = offline
        def reconnect_and_refresh():
            self.S._http_json = native
            return ord("r")
        frames = self.picker([reconnect_and_refresh, 27], height=14)
        first = next(text for text in frames[0]["lines"].values() if "Alpha" in text)
        last = next(text for text in frames[-1]["lines"].values() if "Alpha" in text)
        self.assertEqual(first[1], "!")
        self.assertNotEqual(last[1], "!")

    def test_successful_read_of_saved_credentials_clears_previous_login_refusal(self):
        profile = self.profiles[0]
        key = "codex:" + profile["id"]
        self.S._record_login_problem(key, "saved login not accepted", profile["payload"])
        row = self.S.usage_rows(["codex"], force=True)[0]
        self.assertIsNone(row["problem"])
        self.assertNotIn("login_problem", self.S._usage_cache()[key])

    def test_refused_saved_grant_stays_flagged_when_fresh_live_grant_reads_usage(self):
        self.S.usage_rows(["codex"])
        profile = self.profiles[0]
        def refused_usage(*args):
            raise urllib.error.HTTPError("https://example.invalid", 401, "refused", {},
                                         io.BytesIO(b'{}'))
        def refused_refresh(*args):
            raise urllib.error.HTTPError("https://example.invalid", 401, "refused", {},
                io.BytesIO(b'{"error":{"code":"refresh_token_invalidated"}}'))
        with patch.object(self.S, "fetch_usage", side_effect=refused_usage), \
                patch.object(self.S, "_post_json", side_effect=refused_refresh):
            with self.assertRaises(self.S.SwitchError):
                self.S.proven_payload("codex", profile)
        self.S.BACKENDS["codex"].write_live(login("Alpha", "fresh-live", "new-grant"))
        self.percent["fresh-live"] = 87
        row = self.S.usage_rows(["codex"], force=True)[0]
        self.assertEqual(row["windows"][0]["percent"], 87)
        self.assertEqual(row["problem"], "saved login revoked")

    def test_r_refreshes_the_entire_page_without_opening_rename(self):
        self.S.usage_rows(["codex"])
        self.reads.clear()
        self.percent["Alpha"] = 92
        self.percent["Echo"] = 99
        renamed = dict(self.profiles[1], label="New label")

        def refresh_after_external_change():
            self.S.write_profile(renamed)
            return ord("r")

        with patch.object(self.S, "_ask", side_effect=AssertionError("refresh opened rename")):
            frames = self.picker([258, refresh_after_external_change, 27], height=14)
        self.assertEqual({token for token, workspace in self.reads}, set(self.percent))
        self.assertTrue(any("92%" in text for text in frames[-1]["lines"].values()))
        self.assertTrue(any("99%" in text for text in frames[-1]["lines"].values()))
        self.assertTrue(any("New label" in text for text in frames[-1]["selected"]))

    def test_g_renames_selected_registration_without_rereading_usage(self):
        profile = self.profiles[1]
        before = self.S.get_profile("codex", profile["id"])["payload"]
        with patch.object(self.S, "_ask", return_value="Renamed account") as prompt:
            frames = self.picker([258, ord("g"), 27], height=14)
        prompt.assert_called_once()
        renamed = self.S.get_profile("codex", profile["id"])
        self.assertEqual(renamed["label"], "Renamed account")
        self.assertEqual(renamed["payload"], before)
        self.assertEqual(len(self.reads), 5, "rename should reuse the existing usage readings")
        self.assertTrue(any("Renamed account" in text for text in frames[-1]["selected"]))

    def test_failed_manual_read_keeps_warning_on_later_ui_ticks(self):
        self.S.usage_rows(["codex"])
        error = urllib.error.HTTPError("https://example.invalid", 401, "refused", {}, io.BytesIO(b"{}"))
        self.S._http_json = lambda *args: (_ for _ in ()).throw(error)
        self.S.USAGE_RENEW = False
        self.S.usage_rows(["codex"], force=True)
        rows = self.S.usage_rows(["codex"], refresh=False)
        for row in rows:
            self.assertEqual(row["problem"], "needs re-login")
            self.assertIn("~", self.S.usage_summary(row)[0])

    def test_forced_read_still_respects_provider_cooldown(self):
        self.S.usage_rows(["codex"])
        key = "codex:" + self.profiles[0]["id"]
        self.S._rest_usage(key, 900)
        self.reads.clear()
        rows = self.S.usage_rows(["codex"], force=True)
        self.assertEqual(len(self.reads), 4)
        self.assertEqual(rows[0]["problem"], "rate limited")

    def test_saved_account_with_fresh_login_does_not_show_save_prompt(self):
        profile = self.profiles[0]
        fresh = login("Alpha", "new-login", "new-grant")
        self.S.BACKENDS["codex"].write_live(fresh)
        self.S._select_profile("codex", profile["id"])
        self.assertIsNone(self.S.active_profile("codex"), "a new grant is not a token rotation")
        before = Path(self.S.PROFILES_DIR, "codex", profile["slug"] + ".json").read_bytes()
        head = next(body for row_type, kind, body in self.S._rows() if row_type == "head")
        self.assertNotIn("unsaved", head)
        self.assertNotIn("press", head)
        self.assertEqual(Path(self.S.PROFILES_DIR, "codex", profile["slug"] + ".json").read_bytes(), before)

    def test_saved_identity_without_selection_does_not_show_save_prompt(self):
        self.S.BACKENDS["codex"].write_live(self.profiles[0]["payload"])
        head = next(body for row_type, kind, body in self.S._rows() if row_type == "head")
        self.assertNotIn("press", head)
        self.assertEqual(self.S._selection(), {})

    def test_s_skips_already_saved_account_without_prompting_or_selecting(self):
        self.S.BACKENDS["codex"].write_live(self.profiles[0]["payload"])
        before = {p["id"]: p for p in self.S.list_profiles("codex")}
        with patch.object(self.S, "_ask", return_value="Duplicate") as prompt:
            frames = self.picker([ord("s"), ord("s"), 27], read_usage=False, height=14)
        prompt.assert_not_called()
        self.assertEqual({p["id"]: p for p in self.S.list_profiles("codex")}, before)
        self.assertEqual(self.S._selection(), {})
        self.assertIn("Account already saved", frames[-1]["lines"][13])

    def test_s_does_not_repair_revocation_using_another_login_for_the_same_person(self):
        profile = self.profiles[0]
        self.S.BACKENDS["codex"].write_live(login("Alpha", "fresh", "new-grant"))
        key = "codex:" + profile["id"]
        self.S._record_login_problem(key, "saved login revoked", profile["payload"])
        cache = self.S._usage_cache()
        with patch.object(self.S, "_ask", return_value="Duplicate") as prompt:
            frames = self.picker([ord("s"), 27], read_usage=False, height=14)
        prompt.assert_not_called()
        self.assertEqual(self.S._usage_cache(), cache)
        self.assertEqual(self.S.get_profile("codex", profile["id"]), profile)
        self.assertEqual(self.S._selection(), {})
        self.assertIn("Account already saved", frames[-1]["lines"][13])

    def test_s_saves_another_person_in_the_same_workspace(self):
        self.S.BACKENDS["codex"].write_live(login("New person", "new-token", "new-grant"))
        with patch.object(self.S, "_ask", return_value="New person") as prompt:
            self.picker([ord("s"), 27], read_usage=False, height=14)
        prompt.assert_called_once()
        profiles = self.S.list_profiles("codex")
        self.assertEqual(len(profiles), 6)
        new = next(p for p in profiles if p["label"] == "New person")
        self.assertEqual(new["identity"]["user_id"], "New person")
        self.assertEqual(self.S._selection()["codex"], new["id"])

    def test_s_skips_saved_claude_login_too(self):
        payload = {"credentials": {"claudeAiOauth": {"accessToken": "fake"}},
                   "oauth_account": {"accountUuid": "claude-user", "emailAddress": "claude@example.invalid"}}
        self.S.BACKENDS["claude"].present = lambda: True
        self.S.BACKENDS["claude"].read_live = lambda: payload
        profile = self.S.make_profile("claude", "Claude account", payload)
        self.S.write_profile(profile)
        with patch.object(self.S, "_ask", return_value="Duplicate") as prompt:
            frames = self.picker([258] * 5 + [ord("s"), 27], read_usage=False, height=14)
        prompt.assert_not_called()
        self.assertEqual(self.S.list_profiles("claude"), [profile])
        self.assertEqual(self.S._selection(), {})
        self.assertIn("Account already saved", frames[-1]["lines"][13])

    def test_fresh_live_login_supplies_its_accounts_usage_without_overwriting_saved_grant(self):
        profile = self.profiles[0]
        fresh = login("Alpha", "fresh-live", "new-grant")
        self.percent["fresh-live"] = 87
        self.S.BACKENDS["codex"].write_live(fresh)
        self.S._select_profile("codex", profile["id"])
        rows = self.S.usage_rows(["codex"])
        account = next(row for row in rows if row["id"] == profile["id"])
        self.assertEqual(account["windows"][0]["percent"], 87)
        self.assertEqual(self.S.get_profile("codex", profile["id"])["payload"], profile["payload"])
        self.assertNotIn(("Alpha", "shared-team"), self.reads)
        self.assertFalse(account["active"], "usage does not prove a running Codex applied the login")

    def test_live_login_never_supplies_another_persons_usage_in_same_workspace(self):
        self.S.BACKENDS["codex"].write_live(login("Unregistered", "unregistered", "new-grant"))
        rows = self.S.usage_rows(["codex"])
        for row in rows:
            self.assertEqual(row["windows"][0]["percent"], self.percent[row["label"]])
        self.assertNotIn(("unregistered", "shared-team"), self.reads)

    def test_other_person_in_same_workspace_gets_english_save_prompt(self):
        self.S.BACKENDS["codex"].write_live(login("Unregistered", "new", "new"))
        head = next(body for row_type, kind, body in self.S._rows() if row_type == "head")
        self.assertIn("(press 's' to save this account)", head)

    def reset_provider(self, outcome="reset"):
        from test_codex_resets import credit
        native = self.S._http_json
        banks = {p["label"]: [credit("later-" + p["label"], "2099-11-01T00:00:00Z"),
                              credit("first-" + p["label"], "2099-10-22T19:09:47Z")]
                 for p in self.profiles}
        posts = []
        def provider(url, headers, body=None):
            name = headers["Authorization"].removeprefix("Bearer ")
            if url.endswith("/usage"):
                reading = native(url, headers)
                reading["rate_limit_reset_credits"] = {"available_count": len(banks[name])}
                return reading
            if body is not None:
                posts.append((name, copy.deepcopy(body)))
                if outcome == "reset":
                    banks[name] = [c for c in banks[name] if c["id"] != body["credit_id"]]
                    self.percent[name] = 0
                return {"code": outcome}
            return {"available_count": len(banks[name]), "credits": copy.deepcopy(banks[name])}
        self.S._http_json = provider
        return posts, banks

    def test_reset_count_is_right_of_usage_and_add_new_is_followed_by_t(self):
        posts, _ = self.reset_provider()
        frames = self.picker([27], height=14)
        frame = frames[-1]
        alpha = next(line for line in frame["lines"].values() if "Alpha" in line)
        self.assertGreater(alpha.index("2 resets"), alpha.index("10%"))
        help_text = " ".join(frame["lines"][y].strip() for y in sorted(frame["lines"]) if y < 4)
        self.assertIn("'u' add new | 't' use reset", help_text)
        self.assertEqual(posts, [])

    def test_t_modal_shows_selected_account_expiry_and_enter_escape(self):
        posts, banks = self.reset_provider()
        frames = self.picker([258, ord("t"), 10, 27], height=14)
        modal = " ".join(text for f in frames if f["modal"] for text in f["lines"].values())
        self.assertIn("Use a reset for Bravo ?", modal)
        self.assertIn("Expires: 2099-10-22 19:09 UTC", modal)
        self.assertIn("enter | esc", modal)
        self.assertEqual(posts[0][0], "Bravo")
        self.assertEqual(posts[0][1]["credit_id"], "first-Bravo")
        self.assertEqual(len(posts), 1)
        self.assertEqual(len(banks["Bravo"]), 1)
        self.assertEqual(len(banks["Alpha"]), 2)
        last = next(line for line in frames[-1]["lines"].values() if "Bravo" in line)
        column = self.S.USAGE_COL + self.S.USAGE_W + self.S.RESETS_GAP
        self.assertEqual(last[column:column + self.S.RESETS_W].strip(), "1 reset")
        self.assertIn("0%", last)
        self.assertIn("Reset applied", frames[-1]["lines"][13])

    def test_reset_with_delayed_usage_updates_automatically_without_r(self):
        posts, _ = self.reset_provider()
        native = self.S._http_json
        reads_after_reset = []
        before = self.percent["Bravo"]
        def delayed_usage(url, headers, body=None):
            reply = native(url, headers, body)
            if posts and url.endswith("/usage"):
                who = headers["Authorization"].removeprefix("Bearer ")
                reads_after_reset.append(who)
                if len(reads_after_reset) == 1:
                    reply["rate_limit"]["primary_window"]["used_percent"] = before
            return reply
        self.S._http_json = delayed_usage
        clock = [self.S.time.time()]
        def idle_tick():
            clock[0] += 5
            return -1
        with patch.object(self.S.time, "time", side_effect=lambda: clock[0]):
            frames = self.picker([258, ord("t"), 10, idle_tick, 27], height=14)
        main = [f for f in frames if not f["modal"]]
        stale = next(line for line in main[-2]["lines"].values() if "Bravo" in line)
        final = next(line for line in main[-1]["lines"].values() if "Bravo" in line)
        self.assertIn("25%", stale)
        self.assertIn("1 reset", stale)
        self.assertIn("0%", final)
        self.assertNotIn("25%", final)
        self.assertEqual(reads_after_reset, ["Bravo", "Bravo"])
        self.assertEqual(len(posts), 1, "follow-up reads must never consume another credit")

    def test_reset_keeps_rechecking_when_only_one_window_has_updated(self):
        posts, _ = self.reset_provider()
        native = self.S._http_json
        count = [0]
        def partially_updated(url, headers, body=None):
            reply = native(url, headers, body)
            if url.endswith("/usage"):
                if posts:
                    count[0] += 1
                weekly = 100 if not posts or count[0] == 1 else 0
                reply["rate_limit"]["secondary_window"] = {
                    "used_percent": weekly, "limit_window_seconds": 604800,
                    "reset_after_seconds": 86400}
            return reply
        self.S._http_json = partially_updated
        clock = [self.S.time.time()]
        def idle_tick():
            clock[0] += 5
            return -1
        with patch.object(self.S.time, "time", side_effect=lambda: clock[0]):
            frames = self.picker([258, ord("t"), 10, idle_tick, 27], height=14)
        final = next(line for line in frames[-1]["lines"].values() if "Bravo" in line)
        self.assertNotIn("100%", final)
        self.assertIn("0%", final)
        self.assertEqual(len(posts), 1)

    def test_escape_cancels_reset_without_spending(self):
        posts, banks = self.reset_provider()
        frames = self.picker([ord("t"), 27, 27], height=14)
        self.assertTrue(any(f["modal"] for f in frames))
        self.assertEqual(posts, [])
        self.assertEqual(len(banks["Alpha"]), 2)
        self.assertIn("cancelled", frames[-1]["lines"][13])

    def test_no_resets_shows_message_without_confirmation(self):
        posts, banks = self.reset_provider()
        banks["Alpha"] = []
        frames = self.picker([ord("t"), 27], height=14)
        self.assertFalse(any(f["modal"] for f in frames))
        self.assertEqual(frames[-1]["lines"][13].strip(), "No resets available for this account.")
        self.assertEqual(posts, [])

    def test_unknown_count_does_not_display_zero(self):
        frames = self.picker([27], height=14)
        alpha = next(line for line in frames[-1]["lines"].values() if "Alpha" in line)
        column = self.S.USAGE_COL + self.S.USAGE_W + self.S.RESETS_GAP
        self.assertEqual(alpha[column:column + self.S.RESETS_W].strip(), "-")

    def test_reset_counts_use_singular_for_zero_and_one_plural_for_more(self):
        _, banks = self.reset_provider()
        banks["Alpha"] = []
        banks["Bravo"] = banks["Bravo"][:1]
        frames = self.picker([27], height=18)
        column = self.S.USAGE_COL + self.S.USAGE_W + self.S.RESETS_GAP
        for name, expected in (("Alpha", "0 reset"), ("Bravo", "1 reset"),
                               ("Charlie", "2 resets")):
            line = next(line for line in frames[-1]["lines"].values() if name in line)
            self.assertEqual(line[column:column + self.S.RESETS_W].strip(), expected)

    def test_failed_api_read_displays_dash_instead_of_cached_reset_count(self):
        self.reset_provider()
        self.S.usage_rows(["codex"])
        def offline(*args):
            raise urllib.error.URLError("offline")
        def disconnect_and_refresh():
            self.S._http_json = offline
            return ord("r")
        frames = self.picker([disconnect_and_refresh, 27], height=18)
        column = self.S.USAGE_COL + self.S.USAGE_W + self.S.RESETS_GAP
        first = next(line for line in frames[0]["lines"].values() if "Alpha" in line)
        last = next(line for line in frames[-1]["lines"].values() if "Alpha" in line)
        self.assertEqual(first[column:column + self.S.RESETS_W].strip(), "2 resets")
        self.assertEqual(last[column:column + self.S.RESETS_W].strip(), "-")

    def test_reset_shortcut_is_visible_at_eighty_columns(self):
        frames = self.picker([27], height=16, width=80)
        text = " ".join(frames[-1]["lines"].values())
        self.assertIn("'t' use reset", text)
        self.assertIn("'u' add new", text)

    def test_duplicate_saved_identity_does_not_choose_or_replace_a_uuid(self):
        first = self.profiles[0]
        second = self.S.make_profile("codex", "Alpha again", first["payload"])
        self.S.write_profile(second)
        self.S.BACKENDS["codex"].write_live(login("Alpha", "fresh", "fresh-grant"))
        before = {p["id"]: p["payload"] for p in self.S.list_profiles("codex")}
        head = next(body for row_type, kind, body in self.S._rows() if row_type == "head")
        self.assertNotIn("press", head)
        self.assertEqual(self.S._selection(), {})
        self.assertEqual({p["id"]: p["payload"] for p in self.S.list_profiles("codex")}, before)


if __name__ == "__main__":
    unittest.main()
