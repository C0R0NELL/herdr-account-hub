---
module: account-hub
date: 2026-10-09
problem_type: runtime_error
component: account-picker
severity: high
tags: [oauth, credentials, registration, timeout, account-status, claude-beta]
---
# Account picker registration and failure visibility

Direct `codex login` in the shared home can revoke a previously saved login.
The old picker made adding a safe registration hard to discover: `c` was hidden,
and `u` duplicated the manual usage refresh already provided by `r`. Refused
accounts also lacked a visible indicator next to their names.

## Current behavior

- `u` opens the centered **Add new account** chooser. Arrows select Codex or
  Claude (beta); Enter continues and Esc cancels. **Name** accepts an optional
  label. All confirmation dialogs use **enter | esc**. After confirmation,
  the official CLI owns the terminal and supplies its own login URL/code
  instructions, automatic-save notice, cancellation key and 15-minute limit.
- Official sign-in runs in an empty, private temporary credential directory.
  The plugin waits for CLI success, validates the resulting credential/account
  shape, and stores a new UUID automatically if the email is not registered.
  An existing email returns `account already saved`, preserving the original
  credentials and UUID. It never calls `save_live` or
  applies that login to the shared history home during registration.
- Ctrl+C cancels. `subprocess.run(timeout=900)` kills and waits for the CLI if
  it times out. Failure, cancellation and timeout never create a registration,
  even if the CLI wrote partial credentials; the temporary directory is removed.
- `r` refreshes the whole picker. `c` remains a compatibility Codex shortcut.
- `s` saves the current account only when absent. An existing normalized email
  shows `Account already saved`, without asking for a name or changing a UUID,
  credentials, selection or refusal cache. Recover an existing registration
  with `a`; manual save is not reauthentication.
- A known account error has a red `!` in the marker column, including on the
  selected row and in expanded mode. Healthy usage percentages do not trigger
  it. Details retain the reason, and monochrome terminals retain the symbol.
  The current-account dot remains visible beside `!` when both apply.
- A refusal during switching is cached by registration UUID and credential
  payload hash, separately from usage reporting failures. A new live grant for
  the same person must not clear a refusal of the saved grant merely by reading
  usage. Verification/read of those actual saved credentials clears the failure;
  credential replacement invalidates it. Explicit reauthentication clears the
  registration's cache. Temporary reporting errors clear after a successful read.

## Provider constraints and evidence

Codex pre-login revocation and the isolated login flow are established in
`saved-login-revoked-outside-the-plugin.md`, including the native CLI test with
dummy credentials and a local revocation fixture. This change adds a bounded
wait to that existing flow; no real credentials were queried or renewed.

Claude's official [authentication documentation](https://code.claude.com/docs/en/authentication)
supports separate `CLAUDE_CONFIG_DIR` directories for claude.ai logins. The
[CLI reference](https://code.claude.com/docs/en/cli-reference) documents
`claude auth login`; the installed 2.1.295 help confirms `--claudeai`.
Registration sets a fresh config directory and omits inherited API/auth tokens
to collect a subscription login. Console/profile authentication is outside this
flow. Automatic macOS capture remains unavailable because the current CLI uses
directory-scoped Keychain entries; the plugin does not guess their names or read
the shared Keychain during an isolated registration. Existing capture with `s`
remains available. Claude remains beta: no actual Claude account was tested.

## Verification

`python3 -B tests/check_switcher.py` includes all registration and picker tests:

- Arrow/Enter/Esc provider choice, optional names, confirmation cancellation,
  automatic signup dispatch and return to the picker after failure.
- Red error markers on selected/expanded rows, reporting recovery, and refusal
  retention when a fresh live grant supplies the same person's usage.
- Codex and Claude fake sign-ins: distinct UUIDs, shared-team accounts, unchanged
  active credentials/selection, partial-output refusal and temporary cleanup.
- A real local sleeping CLI fixture proves timeout stops its process; it uses
  no login and makes no network requests.

`ACCOUNT_HUB_TEST_REAL_CODEX=1 python3 -B -m unittest
tests.test_codex_login.RealCodexLogin.test_native_login_revokes_existing_store_but_plugin_login_preserves_it`
also passed after adding the timeout. Its credentials are fictitious and
external connections are blocked.
