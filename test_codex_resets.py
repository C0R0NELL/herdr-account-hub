#!/usr/bin/env python3
"""Earned resets: fictitious accounts and provider responses, no real redemption."""
import copy
import importlib.util
import io
import json
import os
import stat
from pathlib import Path
import tempfile
import time
import unittest
import urllib.error
from unittest.mock import patch

from test_account_records import login


def credit(key, expires, status="available", reset_type="codex_rate_limits"):
    return {"id": key, "expires_at": expires, "status": status,
            "reset_type": reset_type, "granted_at": "2026-10-01T00:00:00Z"}


class CodexResets(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hub-resets-")
        self.env = patch.dict(os.environ, {
            "CODEX_HOME": str(Path(self.temp.name, "codex")),
            "ACCOUNT_HUB_STATE_DIR": str(Path(self.temp.name, "state")),
            "ACCOUNT_HUB_CONFIG_DIR": self.temp.name,
        })
        self.env.start()
        spec = importlib.util.spec_from_file_location(
            "resets_hub", Path(__file__).with_name("switcher.py"))
        self.S = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.S)
        self.native_http = self.S._http_json
        self.S.notify = lambda *a: None
        self.S.BACKENDS["claude"].read_live = lambda: None
        self.profiles = []
        for name in ("One", "Two"):
            p = self.S.make_profile("codex", name, login(name, name, name))
            self.S.write_profile(p)
            self.profiles.append(p)
        self.S.BACKENDS["codex"].write_live(self.profiles[1]["payload"])
        self.S._select_profile("codex", self.profiles[1]["id"])
        self.live_before = self.S.BACKENDS["codex"].read_live()
        self.selected_before = self.S._selection()
        self.banks = {
            "One": [credit("later", "2099-12-01T00:00:00Z"),
                    credit("first", "2099-10-22T19:09:47.796656Z"),
                    credit("middle", "2099-11-01T00:00:00Z")],
            "Two": [credit("other-account", "2099-01-01T00:00:00Z")],
        }
        self.used = {"One": 100, "Two": 27}
        self.requests, self.redeemed = [], {}
        self.outcome = "reset"
        self.S._http_json = self.http
        self.S._post_json = lambda *a: self.fail("unexpected token renewal")

    def tearDown(self):
        self.assertEqual(self.S.BACKENDS["codex"].read_live(), self.live_before)
        self.assertEqual(self.S._selection(), self.selected_before)
        self.env.stop()
        self.temp.cleanup()

    def http(self, url, headers, body=None):
        who = headers["Authorization"].removeprefix("Bearer ")
        self.assertEqual(headers["chatgpt-account-id"], "shared-team")
        self.requests.append((url, who, copy.deepcopy(body)))
        if url.endswith("/usage"):
            return {"rate_limit": {"primary_window": {
                "used_percent": self.used[who], "limit_window_seconds": 18000,
                "reset_after_seconds": 3600}},
                "rate_limit_reset_credits": {"available_count": len(self.banks[who])}}
        if url.endswith("/consume"):
            self.assertIsNotNone(body)
            key = (who, body["redeem_request_id"])
            if key in self.redeemed:
                return {"code": "already_redeemed"}
            if self.outcome == "reset":
                chosen = next(c for c in self.banks[who] if c["id"] == body["credit_id"])
                self.banks[who].remove(chosen)
                self.used[who] = 0
                self.redeemed[key] = body["credit_id"]
            return {"code": self.outcome}
        return {"available_count": len(self.banks[who]), "credits": copy.deepcopy(self.banks[who])}

    def prepare(self):
        return self.S.prepare_reset("codex", self.profiles[0]["id"])

    def test_usage_and_cache_keep_reset_counts_separate_by_registration(self):
        rows = self.S.usage_rows(["codex"])
        self.assertEqual([r["reset_credits"]["available_count"] for r in rows], [3, 1])
        self.requests.clear()
        cached = self.S.usage_rows(["codex"], refresh=False)
        self.assertEqual([r["reset_credits"]["available_count"] for r in cached], [3, 1])
        self.assertEqual(self.requests, [])

    def test_prepare_chooses_earliest_and_only_reads_selected_account(self):
        attempt = self.prepare()
        self.assertEqual(attempt["credit"]["id"], "first")
        self.assertEqual([who for _, who, _ in self.requests], ["One"])
        self.assertTrue(all(body is None for _, _, body in self.requests))
        self.assertEqual(self.redeemed, {})

    def test_ties_are_random_only_within_earliest_expiration(self):
        self.banks["One"] += [credit("tie", "2099-10-22T19:09:47.796656Z")]
        with patch.object(self.S.random, "choice", side_effect=lambda choices: choices[-1]) as choose:
            self.assertEqual(self.prepare()["credit"]["id"], "tie")
            self.assertEqual({c["id"] for c in choose.call_args.args[0]}, {"first", "tie"})

    def test_same_day_considers_hour_fractional_seconds_and_timezone(self):
        self.banks["One"] = [
            credit("later-hour", "2099-10-22T10:00:00Z"),
            credit("later-fraction", "2099-10-22T09:00:00.200000Z"),
            credit("earliest", "2099-10-22T09:00:00.100000Z"),
            credit("earlier-clock-later-utc", "2099-10-22T08:30:00-03:00"),
        ]
        self.assertEqual(self.prepare()["credit"]["id"], "earliest")

    def test_expired_redeeming_and_unsupported_credits_are_never_selected(self):
        self.banks["One"] += [credit("expired", "2000-01-01T00:00:00Z"),
                              credit("busy", "2090-01-01T00:00:00Z", "redeeming"),
                              credit("unknown", "2090-01-01T00:00:00Z", reset_type="other")]
        self.assertEqual(self.prepare()["credit"]["id"], "first")

    def test_dated_credit_is_used_before_a_credit_without_expiry(self):
        self.banks["One"].append(credit("forever", None))
        self.assertEqual(self.prepare()["credit"]["id"], "first")
        self.banks["One"] = [credit("forever", None)]
        self.assertIsNone(self.prepare()["credit"]["expires_at"])

    def test_empty_unknown_malformed_or_incomplete_details_cannot_redeem(self):
        replies = [{"available_count": 0, "credits": []}, {},
                   {"available_count": 3, "credits": None},
                   {"available_count": 4, "credits": self.banks["One"]},
                   {"available_count": 1, "credits": [credit("bad", "not-a-date")]},
                   {"available_count": True, "credits": []}]
        for reply in replies:
            with self.subTest(reply=reply), patch.object(self.S, "_http_json", return_value=reply):
                with self.assertRaises(self.S.SwitchError):
                    self.prepare()
        self.assertEqual(self.redeemed, {})

    def test_consume_uses_confirmed_id_and_refreshes_real_provider_values(self):
        attempt = self.prepare()
        result = self.S.consume_reset(attempt)
        self.assertIn("Reset applied", result)
        posted = [(who, body) for _, who, body in self.requests if body is not None]
        self.assertEqual(posted, [("One", {"credit_id": "first", "redeem_request_id": attempt["request_id"]})])
        rows = self.S.usage_rows(["codex"], refresh=False)
        self.assertEqual(rows[0]["windows"][0]["percent"], 0)
        self.assertEqual(rows[0]["reset_credits"]["available_count"], 2)
        self.assertEqual(len(self.banks["Two"]), 1)

    def test_delayed_usage_is_rechecked_before_normal_ttl(self):
        self.S.usage_rows(["codex"])
        native = self.S._http_json
        post_reads = []
        def delayed_usage(url, headers, body=None):
            reply = native(url, headers, body)
            if self.redeemed and url.endswith("/usage"):
                post_reads.append(headers["Authorization"])
                if len(post_reads) == 1:
                    reply["rate_limit"]["primary_window"]["used_percent"] = 100
            return reply
        clock = [time.time()]
        with patch.object(self.S.time, "time", side_effect=lambda: clock[0]), \
                patch.object(self.S, "_http_json", side_effect=delayed_usage):
            self.S.consume_reset(self.prepare())
            immediate = self.S.usage_rows(["codex"], refresh=False)[0]
            self.assertEqual(immediate["reset_credits"]["available_count"], 2)
            self.assertEqual(immediate["windows"][0]["percent"], 100)
            clock[0] += 5
            updated = self.S.usage_rows(["codex"])[0]
            self.assertEqual(updated["windows"][0]["percent"], 0)
            self.assertIsNotNone(updated.get("refresh_after"))
            clock[0] += 10
            self.S.usage_rows(["codex"])
        self.assertEqual(post_reads, ["Bearer One"] * 3)
        self.assertEqual(len(self.redeemed), 1)

    def test_unchanged_usage_follow_up_reads_are_bounded(self):
        self.S.usage_rows(["codex"])
        native = self.S._http_json
        post_reads = []
        def unchanged_usage(url, headers, body=None):
            reply = native(url, headers, body)
            if self.redeemed and url.endswith("/usage"):
                post_reads.append(headers["Authorization"])
                reply["rate_limit"]["primary_window"]["used_percent"] = 100
            return reply
        clock = [time.time()]
        with patch.object(self.S.time, "time", side_effect=lambda: clock[0]), \
                patch.object(self.S, "_http_json", side_effect=unchanged_usage):
            self.S.consume_reset(self.prepare())
            rows = self.S.usage_rows(["codex"], refresh=False)
            checks = 0
            while rows[0].get("refresh_after") is not None:
                checks += 1
                self.assertLessEqual(checks, 3, "must not poll forever")
                clock[0] = rows[0]["refresh_after"]
                rows = self.S.usage_rows(["codex"])
            clock[0] += 5
            self.S.usage_rows(["codex"])
        self.assertEqual(len(post_reads), 4, "one immediate read plus three follow-ups")
        self.assertEqual(len(self.redeemed), 1)

    def test_follow_up_respects_retry_after_and_cache_only_calls_make_no_requests(self):
        self.S.usage_rows(["codex"])
        native = self.S._http_json
        post_reads = []
        def delayed_with_cooldown(url, headers, body=None):
            if (self.redeemed and url.endswith("/usage")
                    and headers["Authorization"] == "Bearer One"):
                post_reads.append(headers["Authorization"])
                if len(post_reads) == 2:
                    raise urllib.error.HTTPError(url, 429, "slow down", {"Retry-After": "20"},
                                                 io.BytesIO(b'{}'))
                reply = native(url, headers, body)
                if len(post_reads) == 1:
                    reply["rate_limit"]["primary_window"]["used_percent"] = 100
                return reply
            return native(url, headers, body)
        clock = [time.time()]
        with patch.object(self.S.time, "time", side_effect=lambda: clock[0]), \
                patch.object(self.S, "_http_json", side_effect=delayed_with_cooldown):
            self.S.consume_reset(self.prepare())
            clock[0] += 5
            cooling = self.S.usage_rows(["codex"])[0]
            self.assertEqual(cooling["refresh_after"], clock[0] + 20)
            clock[0] += 10
            self.S.usage_rows(["codex"], force=True)
            self.S.usage_rows(["codex"], refresh=False)
            self.assertEqual(len(post_reads), 2)
            clock[0] = cooling["refresh_after"]
            self.S.usage_rows(["codex"], refresh=False)
            self.assertEqual(len(post_reads), 2, "refresh=False must remain read-only")
            ready = self.S.usage_rows(["codex"])[0]
            self.assertEqual(ready["windows"][0]["percent"], 0)
            self.assertIsNotNone(ready["refresh_after"])
        self.assertEqual(len(post_reads), 3)
        self.assertEqual(len(self.redeemed), 1)

    def test_follow_up_stores_nonzero_provider_usage_without_guessing_zero(self):
        self.S.usage_rows(["codex"])
        native = self.S._http_json
        count = [0]
        def used_while_resetting(url, headers, body=None):
            reply = native(url, headers, body)
            if self.redeemed and url.endswith("/usage"):
                count[0] += 1
                reply["rate_limit"]["primary_window"]["used_percent"] = 100 if count[0] == 1 else 7
            return reply
        clock = [time.time()]
        with patch.object(self.S.time, "time", side_effect=lambda: clock[0]), \
                patch.object(self.S, "_http_json", side_effect=used_while_resetting):
            self.S.consume_reset(self.prepare())
            clock[0] += 5
            updated = self.S.usage_rows(["codex"])[0]
        self.assertEqual(updated["windows"][0]["percent"], 7)
        self.assertIsNotNone(updated["refresh_after"])
        self.assertEqual(len(self.redeemed), 1)

    def test_unknown_response_retries_same_operation_even_after_restart(self):
        attempt = self.prepare()
        native = self.S._http_json
        def lose_reply(url, headers, body=None):
            result = native(url, headers, body)
            if body is not None:
                raise TimeoutError("response lost after committing")
            return result
        with patch.object(self.S, "_http_json", side_effect=lose_reply):
            with self.assertRaisesRegex(self.S.SwitchError, "not confirmed"):
                self.S.consume_reset(attempt)
        row = self.S.usage_rows(["codex"], refresh=False)[0]
        self.assertIsNone(row["reset_credits"], "unknown redemption must invalidate the old count")
        self.assertIn("unconfirmed", row["problem"])
        # Fresh module/process state: the operation survives only on disk.
        spec = importlib.util.spec_from_file_location(
            "resets_hub_after_restart", Path(__file__).with_name("switcher.py"))
        self.S = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.S)
        self.S._http_json = self.http
        self.S._post_json = lambda *a: self.fail("unexpected token renewal")
        self.S.BACKENDS["claude"].read_live = lambda: None
        pending = self.prepare()
        self.assertEqual(pending["request_id"], attempt["request_id"])
        self.assertEqual(pending["credit"]["id"], "first")
        self.S.consume_reset(pending)
        self.assertEqual(len(self.redeemed), 1)
        self.assertEqual(len(self.banks["One"]), 2)

    def test_two_open_confirmations_cannot_spend_two_credits(self):
        first, second = self.prepare(), self.prepare()
        self.S.consume_reset(first)
        with self.assertRaises(self.S.SwitchError):
            self.S.consume_reset(second)
        self.assertEqual(len(self.redeemed), 1)

    def test_repeat_same_confirmed_attempt_does_not_spend_again(self):
        attempt = self.prepare()
        self.S.consume_reset(attempt)
        self.S.consume_reset(attempt)
        self.assertEqual(len([r for r in self.requests if r[2] is not None]), 1)

    def test_expiring_while_dialog_is_open_does_not_pick_another_credit(self):
        attempt = self.prepare()
        with patch.object(self.S.time, "time", return_value=attempt["credit"]["expires_at"] + 1):
            with self.assertRaisesRegex(self.S.SwitchError, "expired"):
                self.S.consume_reset(attempt)
        self.assertEqual(self.redeemed, {})

    def test_reauthentication_to_different_person_invalidates_confirmation(self):
        attempt = self.prepare()
        profile = self.profiles[0]
        profile["payload"] = login("Else", "Else", "Else")
        profile["identity"] = self.S.BACKENDS["codex"].identity(profile["payload"])
        self.S.write_profile(profile)
        with self.assertRaises(self.S.SwitchError):
            self.S.consume_reset(attempt)
        self.assertEqual(self.redeemed, {})

    def test_no_eligible_window_or_no_credit_is_not_reported_as_success(self):
        for outcome, message in (("nothing_to_reset", "No eligible usage window"),
                                 ("no_credit", "No resets available")):
            with self.subTest(outcome=outcome):
                self.outcome = outcome
                result = self.S.consume_reset(self.prepare())
                self.assertIn(message, result)
                self.assertNotIn("Reset applied", result)
        self.assertEqual(self.redeemed, {})

    def test_unauthorized_read_does_not_use_another_account_or_reveal_body(self):
        with patch.object(self.S, "USAGE_RENEW", False), patch.object(self.S, "_http_json", side_effect=urllib.error.HTTPError(
                "https://example.invalid", 401, "refused", {},
                io.BytesIO(b'{"error":{"code":"token_revoked","message":"SECRET"}}'))):
            with self.assertRaises(self.S.SwitchError) as error:
                self.prepare()
        self.assertNotIn("SECRET", str(error.exception))
        self.assertEqual(self.redeemed, {})

    def test_applied_reset_with_failed_refresh_does_not_infer_new_count_or_usage(self):
        attempt = self.prepare()
        native = self.S._http_json
        def fail_read(url, headers, body=None):
            if body is None:
                raise TimeoutError("reading failed")
            return native(url, headers, body)
        with patch.object(self.S, "_http_json", side_effect=fail_read):
            result = self.S.consume_reset(attempt)
        self.assertIn("Reset applied", result)
        self.assertIn("refresh", result)
        row = self.S.usage_rows(["codex"], refresh=False)[0]
        self.assertIsNone(row["reset_credits"])

    def test_claude_and_api_key_cannot_use_codex_resets(self):
        p = self.S.make_profile("claude", "Claude", {"credentials": {}})
        self.S.write_profile(p)
        with self.assertRaises(self.S.SwitchError):
            self.S.prepare_reset("claude", p["id"])
        self.profiles[0]["payload"] = {"auth": {"OPENAI_API_KEY": "fake-key"}}
        self.S.write_profile(self.profiles[0])
        with self.assertRaises(self.S.SwitchError):
            self.prepare()
        self.assertEqual(self.requests, [])

    def test_zero_applicable_count_does_not_block_confirmed_attempt(self):
        native = self.S._http_json
        def add_applicability(url, headers, body=None):
            reply = native(url, headers, body)
            if url.endswith("/usage"):
                reply["rate_limit_reset_credits"]["applicable_available_count"] = 0
            return reply
        with patch.object(self.S, "_http_json", side_effect=add_applicability):
            self.S.usage_rows(["codex"])
            self.S.consume_reset(self.prepare())
        self.assertEqual(len(self.redeemed), 1)

    def test_new_unknown_summary_replaces_old_count_without_guessing_zero(self):
        self.S.usage_rows(["codex"])
        native = self.S._http_json
        def no_summary(url, headers, body=None):
            reply = native(url, headers, body)
            reply.pop("rate_limit_reset_credits", None)
            return reply
        with patch.object(self.S, "_http_json", side_effect=no_summary):
            rows = self.S.usage_rows(["codex"], force=True)
        self.assertTrue(all(r["reset_credits"] is None for r in rows))

    def test_journal_failure_prevents_consumption(self):
        attempt = self.prepare()
        with patch.object(self.S, "_write_json_secret", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.S.consume_reset(attempt)
        self.assertFalse(any(body is not None for _, _, body in self.requests))

    def test_cache_write_failure_after_confirmed_redemption_still_reports_success(self):
        attempt = self.prepare()
        native = self.S._write_json_secret
        def fail_cache(path, value):
            if path == self.S.USAGE_CACHE:
                raise OSError("disk full")
            return native(path, value)
        with patch.object(self.S, "_write_json_secret", side_effect=fail_cache):
            result = self.S.consume_reset(attempt)
        self.assertIn("Reset applied", result)
        self.assertIn("refresh", result)
        self.assertEqual(len(self.redeemed), 1)

    def test_operation_journal_has_private_permissions(self):
        self.S.consume_reset(self.prepare())
        self.assertEqual(stat.S_IMODE(Path(self.S.RESET_ATTEMPTS).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(Path(self.S.STATE_DIR).stat().st_mode), 0o700)

    def test_real_http_adapter_sends_exact_official_request_contract(self):
        headers = {"Authorization": "Bearer fictitious", "chatgpt-account-id": "shared-team",
                   "Accept": "application/json", "User-Agent": "codex-cli"}
        body = {"credit_id": "opaque-credit", "redeem_request_id": "fictitious-request"}
        response = io.BytesIO(b'{"code":"nothing_to_reset"}')
        with patch.object(self.S.urllib.request, "urlopen", return_value=response) as send:
            result = self.native_http(self.S.CODEX_RESETS_URL + "/consume", headers, body)
        request = send.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url,
                         "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume")
        self.assertEqual(json.loads(request.data), body)
        self.assertEqual(request.get_header("Authorization"), "Bearer fictitious")
        self.assertEqual(request.get_header("Chatgpt-account-id"), "shared-team")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(send.call_args.kwargs["timeout"], self.S.USAGE_TIMEOUT_S)
        self.assertEqual(result, {"code": "nothing_to_reset"})

    def test_two_processes_confirming_together_send_one_redemption(self):
        attempts = [self.prepare(), self.prepare()]
        posted = Path(self.temp.name, "posts")
        ready, start = os.pipe()
        children = []
        for attempt in attempts:
            pid = os.fork()
            if pid == 0:
                os.close(start)
                os.read(ready, 1)
                native = self.S._http_json
                def provider(url, headers, body=None):
                    if body is not None:
                        with posted.open("a") as f:
                            f.write(body["redeem_request_id"] + "\n")
                    return native(url, headers, body)
                self.S._http_json = provider
                try:
                    self.S.consume_reset(attempt)
                except self.S.SwitchError:
                    pass
                except Exception:
                    os._exit(2)
                os._exit(0)
            children.append(pid)
        os.close(ready)
        os.write(start, b"xx")
        os.close(start)
        for child in children:
            self.assertEqual(os.waitpid(child, 0)[1], 0)
        self.assertEqual(len(posted.read_text().splitlines()), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
