---
module: account-hub
date: 2026-10-10
problem_type: integration
component: earned-resets
tags: [codex, reset-credits, uuid, idempotency, account-isolation]
---

# Evidence and contract

The official [app-server documentation](https://learn.chatgpt.com/docs/app-server)
defines `account/rateLimits/read` reset-credit summaries/details and
`account/rateLimitResetCredit/consume`. Its rules require a UUID per logical
redemption and the **same UUID on retries**, followed by a fresh usage read.
`alreadyRedeemed` is idempotent success; `nothingToReset` and `noCredit` are
distinct from success. The docs do not specify the criteria for an eligible
window and do not establish a requirement to reach 100% usage first.

Official source at `rust-v0.162.0` establishes the HTTP wire contract used
behind those RPC methods:

- `codex-rs/backend-client/src/client/rate_limit_resets.rs`: GET
  `/backend-api/wham/rate-limit-reset-credits`, POST the same path plus
  `/consume` with `credit_id` and `redeem_request_id`.
- `codex-rs/backend-client/src/types.rs`: count `available_count`, credit
  `reset_type`, `status`, RFC3339 `expires_at` (nullable), and response `code`
  values `reset`, `already_redeemed`, `nothing_to_reset`, `no_credit`.
- `codex-rs/app-server/src/request_processors/account_processor/rate_limit_resets.rs`:
  maps these fields/outcomes to the documented RPC surface.

A user-authorized read-only query of the current account returned a usage
summary with `available_count: 3`, `applicable_available_count: 0`, and three
available `codex_rate_limits` details, each with an RFC3339 expiry. No tokens
were renewed and no reset was consumed. This establishes the read contract in
the installed account; it does **not** prove the outcome of a redemption or
the interpretation of the applicability hint. Do not gate confirmation on
that hint or claim it proves rejection.

# Implementation rules

- Poll counts and nearest expiry with existing usage reads/cache. Positive counts
  need at most one additional detail GET per account refresh unless the usage
  response already supplies complete details. **t** rereads details before
  confirmation. Count is authoritative, not the length of a potentially capped
  detail list. Detail read failures preserve usage/count and show `-` for expiry;
  detail HTTP 429 cooldowns persist per UUID and also apply to forced refresh.
- Scope credentials, cache and operation journal to the selected registration
  UUID. Never change global auth, selection, history or the running daemon.
- Choose available Codex credits with the closest future expiry; randomize
  ties. Exclude expired/redeeming/unknown types. Treat undated credits after
  dated credits and show `Expires: not provided` when necessary.
- The picker shows the nearest dated expiry as `YYYY/MM/DD HH:MM UTC` in a fixed
  column beside the count. Zero/unknown resets and incomplete, malformed or
  undated detail lists show `-` there without shifting the account's email.
  Display and confirmation use the same availability/expiry filtering rules.
- Fail without consuming if complete expiry details cannot be determined.
- Preparing/canceling performs no redemption. Persist the operation UUID to
  `reset-attempts.json` in the durable plugin state directory (0600, inside
  0700 state) only after Enter and
  before POST. If that write fails, send no POST.
- A lost response leaves the journal pending. A retry uses the same opaque
  credit ID and operation UUID even if the credit is now absent from GET.
  Never select a fresh credit to guess whether the previous one succeeded.
- Validate account identity again after the modal; serialize and reject stale
  confirmations from concurrent pickers. Credential rotation for the same
  person/workspace does not invalidate the logical operation.
- Persist a known outcome, invalidate old reset counts, then reread usage.
  Do not infer new percentages/counts. A failed refresh remains explicit.

# Verification limits

`python3 -B tests/check_switcher.py` includes `tests/test_codex_resets.py` and
`tests/test_usage_picker.py`. They use fictitious credentials/provider responses:
earliest expiry, tied choice, unavailable/malformed/capped details, cancellation,
account isolation in a shared workspace, confirmation expiry, two simultaneous
processes, lost-response recovery from a fresh module, file permissions and the
actual urllib request adapter. No development test redeems a real credit.

Development tests do not exercise real provider consumption; the user's later
redemption and its limits are described below. The HTTP contract is from
official code, not a separately supported public REST API; keep it aligned
with Codex changes.

# Delayed usage after a reset (2026-10-10)

The user subsequently confirmed a reset in the picker and reported that the
credit count changed while usage stayed unchanged until pressing **r**.
Read-only inspection of the local receipt found `outcome: reset`; the latest
usage cache was written roughly ten seconds after that receipt and contained
the updated quota. The first usage response was not retained, so these local
files do not prove the provider's timing or internal consistency mechanism.

Code inspection found one immediate post-reset GET whose result was cached for
the normal 120-second interval. A deterministic fake provider reproduced the
reported behavior: it returned the new credit count with the old usage on the
first GET, then updated usage on the next GET. An idle picker tick failed to
fetch that next response. The regression failed before the fix and passed
afterwards, without **r**, real credentials or another redemption.

Successful `reset` and `already_redeemed` outcomes now enqueue three bounded
usage-only follow-ups in the existing per-UUID cache, with delays of 2, 5 and
10 seconds between checks. The picker services them on its five-second idle
ticks; they bypass the ordinary cache lifetime and respect `Retry-After`.
Other fresh registrations are not fetched. The existing lock serializes
cache/queue updates across pickers, and cache-only reads remain network-free.
Closing/reopening the picker retains the queued checks.

Do not stop after the first changed window: a separate fake response sequence
reproduced a primary window updating while the weekly window still showed
100%. Keeping the bounded follow-ups fixed that case too. Read the actual
provider values, including nonzero post-reset usage; never guess a zero or
repeat the consume POST while refreshing. After the bounded checks, ordinary
polling resumes. Regression coverage also checks request bounds, cooldowns,
account isolation and the exact single redemption count.
