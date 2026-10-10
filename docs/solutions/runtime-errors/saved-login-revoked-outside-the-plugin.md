---
module: account-hub
date: 2026-10-09
problem_type: runtime_error
component: datastore
severity: high
symptoms:
  - "A saved profile installs cleanly, the switch reports success, and the agent is signed out"
  - "A parked profile's access token is still valid by its own exp claim, but the provider answers 401"
  - "codex: a profile stops working with no switch, no renewal and no action by this plugin"
root_cause: logic_error
resolution_type: code_fix
tags: [oauth, refresh-token, revocation, codex, credential-store]
---
# Problem

A credential snapshot can stop working while sitting on disk, and nothing about
the snapshot shows it. It still parses, still installs, and only fails when an
agent uses it. `switch()` used to read the store back and compare the account
identity, which proves the write landed and says nothing about whether the login
still signs in.

## Evidence: Codex sign-in revocation

Established on Codex 0.162.0, 2026-10-09. Official source
`codex-rs/cli/src/login.rs` calls `clear_existing_auth_before_login`, which calls
`logout_with_revoke`, for both browser and device-code sign-in. A real-binary,
local-only test with fictitious tokens proved that `codex login --device-auth`
sends a revocation request for the auth file in its home before starting login.

Account Hub now starts official sign-in in a fresh temporary credential folder
with no auth file to revoke. Only the result is stored under the explicit local
registration UUID, while all actual chats continue using the user's shared
Codex home. This is an authentication-only staging folder, not per-account chat
history. The same real-binary test confirmed that the staged flow sends no
revocation request and leaves the working shared auth file unchanged.

## Official references and investigation evidence (2026-10-09)

- The official [authentication documentation](https://learn.chatgpt.com/docs/auth)
  establishes that the CLI and IDE share cached credentials, that file storage
  lives under `CODEX_HOME`, and that Codex automatically refreshes active tokens.
  It also identifies `codex-login.log` as the log for direct CLI sign-in. It does
  not itself describe the CLI's pre-login revocation; that was established from
  the official 0.162.0 source and the local-only native test above.
- The official [accounts and sessions guidance](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions)
  says to retain the latest replacement tokens and serialize refreshes for the
  same session. Its [recovery guidance](https://developers.openai.com/siwc/token-sharing-open-source/errors-and-recovery)
  distinguishes unusable refresh tokens such as `refresh_token_invalidated`
  and `refresh_token_reused` from temporary failures. These pages describe the
  separate Sign in with ChatGPT integration flow: do not substitute its dynamic
  client IDs, token endpoint or lifetimes for the Codex CLI's token format.
- A usage-cache refresh is not a new sign-in, and deleting a usage cache cannot
  restore a grant revoked on the server. Recover affected UUIDs with the
  plugin's isolated sign-in action, and apply a saved registration with Enter.
  Add new registrations with the provider chooser rather than sequential
  direct sign-ins in the shared home when collecting accounts for later switching.

## Rejection handling in the implementation

The following table describes `proven_payload` and `_refresh_refused` in
`switcher.py`. It is a local recovery policy, not a guarantee about every
provider's error vocabulary:

| response | handling |
|---|---|
| First usage HTTP 401 | Force token renewal, then retry the usage read once |
| HTTP 401 during renewal or after the retry | Refuse the switch before changing the active account |
| Renewal HTTP 400 with `invalid_grant`, `refresh_token_reused`, `refresh_token_invalidated`, or `refresh_token_expired` | Refuse the saved login |
| HTTP 429 | Record the provider cooldown; do not classify the login as revoked |
| Network failure or other unrecognized response | Do not infer revocation; retain the separate runtime checks |

Known error codes provide bounded explanations; raw provider messages and
credentials are never printed. A JWT expiry and an account's plan string cannot
establish whether its grant remains usable, or why a provider rejected it.

# What Didn't Work

- Trusting the token's `exp`. A future expiry cannot prove that the provider
  still accepts the grant.
- Reading the credential store back after writing it. That catches a store that
  refused the write. It cannot catch a credential the provider has retired.
- Treating all HTTP failures as revocation. Temporary failures do not establish
  an unusable grant, and the first usage 401 still has a renewal recovery path.

# Solution

Ask the provider before overwriting a working login, while it is still in place.
`proven_payload` renews the target profile, reads the usage endpoint, and on a
401 renews past the clock and asks once more. It refuses the switch only on a
refusal listed in the table above. `verify_switch = false` turns off this
provider check, not Codex's compatibility and daemon reload checks.

Renewing rotates the tokens, so the check hands back the payload to install and
`switch()` writes *that* one to the profile. Writing the old one back would park
a spent pair over a live one — the failure this exists to prevent.

# Why This Works

The provider decides whether it accepts a login. Parsing, identity matching and
expiry checks describe the snapshot, not its server-side grant. A transient
failure is not a successful verification. Codex switching additionally verifies
the reloaded daemon's adopted account, as described in the
[reload investigation](codex-machine-account-reload.md).

# Prevention

- Never treat a parseable snapshot as a working login.
- Never treat a network failure as a dead account. Follow the response handling
  in the table and keep it aligned with `proven_payload` and `_refresh_refused`.
- Persist a renewal's reply before using it: from the moment the endpoint
  answers, the old refresh token is dead and the reply is the only usable pair
  that account has. `renew_profile` stages it to disk first for that reason.
- Refresh a profile's snapshot as you park it, so a switch away does not leave a
  copy that can no longer renew.
- For Codex CLI 0.162.0, collect new registrations using the provider chooser,
  and reauthenticate existing UUIDs with the picker sign-in action. Calling
  `codex login` in the shared home revokes its existing OAuth login before sign-in.
- Do not attribute revocation to a plan change without provider evidence.

Covered by `tests/check_switcher.py`: a retired profile is refused with the live store
untouched and nothing written, a spent refresh token is refused, and being
offline or rate limited both switch through.

# Fresh regression evidence (2026-10-09)

- Selecting the already active registration now synchronizes that UUID from the
  live file before verifying it, so a refresh token rotated naturally by Codex
  is not spent again from the older snapshot. A deterministic test reproduced
  the stale-token refusal before the fix and passes afterwards.
- Renewed `last_refresh` values now use UTC RFC 3339 (`Z`), matching Codex's
  timestamp type. The previous naive Python timestamp lacked a timezone.
- Provider rejection codes are shown as bounded explanations, never raw bodies,
  with `a` as the recovery action; users are no longer told to create another
  duplicate registration with `save`.
- Multiple registrations with the same display name now show UTC save times.
  This makes a fresh registration distinguishable without displaying secret data.
- These regressions use fictitious credentials. They do not identify the cause
  of any real account's refusal; that requires provider evidence or the user
  completing an official reauthentication. Real Claude authentication remains
  unvalidated, as documented in the [guide](../../guide.md#claude-code-beta).

`python3 -B test_switcher.py` includes registration/renewal regressions.
`ACCOUNT_HUB_TEST_REAL_CODEX=1 python3 -B -m unittest
 tests.test_codex_login.RealCodexLogin.test_native_login_revokes_existing_store_but_plugin_login_preserves_it`
exercises the official CLI with dummy credentials, a local revocation fixture
and blocked external connections.
