---
module: account-hub
date: 2026-10-09
problem_type: runtime_error
component: codex-runtime
tags: [codex, auth-cache, global-account, daemon-restart, native-reconnect]
---

# Evidence

Changing `auth.json` does not prove that an already running Codex adopted the
account. The official [authentication documentation](https://learn.chatgpt.com/docs/auth)
describes credential caching. The installed Codex CLI 0.162.0 and official source
at tag `rust-v0.162.0` were inspected:

- `codex-rs/login/src/auth/manager.rs` documents the in-memory auth snapshot and
  explicit `AuthManager.reload()`. Ordinary ChatGPT sign-in completion calls
  that reload in `app-server/src/request_processors/account_processor.rs`.
- `codex app-server daemon restart --help` exposes an official lifecycle command.
  `app-server-daemon/src/lib.rs` implements restart of the managed local server.
- `app-server-daemon/src/settings.rs` at the same tag establishes a default
  `shutdownGraceSeconds` of 60 and a maximum of 300. `backend/pid.rs` sends SIGTERM
  on Unix, waits for that grace, then sends SIGKILL if the daemon is still alive.
  `app-server/src/lib.rs` drains running assistant turns and admitted requests;
  the wait is daemon-wide and applies to work beyond compaction. A forced stop
  does not prove that unfinished work completed.
- `tui/src/app/reconnect.rs` reconnects out-of-process clients and resumes their
  exact existing thread IDs, restoring native history and runtime UI state.
- The [app-server protocol](https://learn.chatgpt.com/docs/app-server) exposes
  `account/read`, `config/read`, `thread/loaded/list` and `thread/read`. These
  establish server identity and chat metadata without renaming or copying.

A real-binary test used two ordinary `codex resume` clients, three fictitious
ChatGPT users in the same workspace, a local HTTP fixture and temporary
`CODEX_HOME`/`CODEX_SQLITE_HOME`. No real login or inference was used. Switching
through the public `switch()` transaction preserved both native client processes
and both chat IDs, custom titles and working directories. `account/read` reported
the newly selected user after each switch; both chats returned to the loaded set.

The first real transaction test exposed that `config/read` can return null for
an implicit model provider. Null means the OpenAI default, not a custom provider.
A regression covers this before the credential write.

# Solution

There is one machine-wide account for the current user's Codex home. Enter in
the picker opens an English centered Enter/Esc dialog; Enter applies through the same
`switch()` path as the scriptable command and the cycle action.

Before writing, inspect the existing daemon, the file credential backend and
loaded chats. Install the selected registration's credentials atomically, then
use the official daemon restart. Its fresh AuthManager replaces the old auth
cache, and native clients reconnect themselves. Verify the server's account,
loaded chat set and unchanged IDs/names/directories before committing selection
or reporting success. If no daemon is running, install the login for future
chats without starting one. On failure, restore the previous credentials and,
when a restart was attempted, reload and verify the previous account as well.
Report an unconfirmed rollback explicitly.

Do not delete history databases, copy rollouts, create per-account homes, rename
threads, supply conversation IDs manually or launch special managed Codex panes.
Codex retains its ordinary OAuth token refresh lifecycle. Local account records
remain UUID-addressed and separate even when workspace/email metadata coincide.
The plugin's own usage cache remains UUID-keyed; clearing it is not an auth reset.

# Scope

The installed CLI and daemon must be at least 0.162.0, use the managed local
daemon, the default OpenAI provider and file credentials. Other users, other
homes, remote servers and `--no-daemon` sessions are separate execution scopes.
The daemon command owns the graceful shutdown policy; the plugin does not kill
arbitrary processes. The picker says that the daemon waits for active work,
in English, for Codex only; it does not change daemon settings. The settings
reader follows the official fallback for malformed/out-of-range grace values.
The command timeout
allows that grace plus 90 seconds of restart overhead; chat reconnection has its
own verification deadline. A failed passive probe is not evidence of an absent daemon.
Unix WebSocket transport remains experimental as described in the official docs.

# Verification

- `python3 -B test_switcher.py`: core checks, UUID isolation, global transaction
  failures, confirmation/cancellation and transport tests; all credentials fake.
- `ACCOUNT_HUB_TEST_REAL_CODEX=1 python3 -B -m unittest tests.test_codex_runtime.RealCodexMachine`:
  optional native integration with two chats and three dummy users, no inference.
- The default suite does not restart a real daemon or read saved user accounts.
  The optional native test uses temporary homes and fictitious credentials.
