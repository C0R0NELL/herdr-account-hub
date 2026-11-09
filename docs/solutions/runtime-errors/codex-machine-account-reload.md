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

- `python3 -B tests/check_switcher.py`: core checks, UUID isolation, global transaction
  failures, confirmation/cancellation and transport tests; all credentials fake.
- `ACCOUNT_HUB_TEST_REAL_CODEX=1 python3 -B -m unittest tests.test_codex_runtime.RealCodexMachine`:
  optional native integration with two chats and three dummy users, no inference.
- The default suite does not restart a real daemon or read saved user accounts.
  The optional native test uses temporary homes and fictitious credentials.

# Identifying a temporary chat (2026-10-23)

A read-only inspection of a running 0.164.0 daemon returned an idle thread with
`ephemeral: true`, no `name`, no preview and no storage `path`. Its originator was
`codex-tui`. The official [app-server thread documentation](https://learn.chatgpt.com/docs/app-server#threads)
distinguishes in-memory ephemeral threads from persisted history and establishes
that `thread/loaded/list` includes loaded threads, not just resumable chats.
These fields do not prove that an idle thread can safely be discarded or why its
client created it. Do not infer a cache failure from the refusal.

The previous refusal named no thread and stopped at the first blocker. It now
collects every temporary thread in stable ID order and numbers them `[1]`, `[2]`,
etc. Each entry identifies the directory basename, title (or `Untitled`) and a
short thread ID. Labels are bounded and stripped of terminal control characters;
conversation previews are never used. The picker displays a scrollable list;
CLI errors include the numbered list and recovery guidance. Without explicit
discard consent, ephemeral threads prevent credential writes and daemon restart.

## Explicit Force confirmation

The list offers **Force** (`f` or click) and **Esc**, with a persistent warning
that unsaved content will be lost. Enter does not confirm destructive discard.
The UI retries the same account/UUID with only the full temporary thread IDs
shown in that dialog. Inspection after credential verification and immediately
before restart checks that every currently temporary thread is in the confirmed
set; otherwise it refuses and requests a new confirmation. A thread that became
persistent joins the saved-chat snapshot instead of being discarded.

Official docs and the installed 0.163.0 generated protocol schema establish no
immediate close method for ephemeral roots. `thread/delete` cannot delete them;
`thread/unsubscribe` only affects the calling connection and has an inactivity
grace. Force therefore uses the existing official shared-daemon restart, whose
process exit discards in-memory temporary chats. It does not send delete/archive
requests, close Herdr panes, or kill unrelated processes. The restarted daemon
must no longer list the confirmed temporary IDs, and saved chats still have
their IDs, names, directories and adopted account verified. Failed switches
attempt normal account rollback; already discarded temporary content cannot be
reconstructed, and failures after a discard attempt say so.

There is no API transaction that freezes other clients while restarting. The
rechecks reduce stale consent; they cannot make concurrent thread creation
atomic with the daemon command. Do not promise isolated per-thread shutdown or
claim that normal rollback can restore ephemeral history.

Fictitious regressions cover named/unnamed/idle ephemeral threads, unchanged
authentication on cancel, multiple blockers, new-thread reconfirmation,
preserved saved chats, sanitized labels, short terminals, scrolling, mouse/`f`
confirmation and ignored Enter. The optional native runtime test also creates
an ephemeral thread with fictitious credentials before a forced account switch
and verifies its disappearance while the two saved native chats reconnect.

# Applying a current-account re-login (2026-11-09)

A fictitious re-login reproduced the reported recovery problem: `login_codex`
saved the new grant under the existing UUID, but left both the shared auth file
and the daemon's authentication unchanged. Applying that same UUID through
`switch()` installed the new grant and reloaded both saved chats. Switching to
another account first was unnecessary. This explains recovery after re-login;
it does not establish what originally invalidated a provider's credentials.

The picker now captures the live login before starting an existing account's
sign-in. Automatic application requires that the displayed current UUID, person,
and workspace match that registration. After successful sign-in and terminal
restoration, it uses the same UI switch path as Enter, including daemon progress,
temporary-chat Force confirmation, saved-chat verification, and rollback.
Reauthenticating an inactive account and adding an account remain save-only.
The standalone `codex-login` command retains its explicit save/apply steps.

The switch transaction checks the original live grant under its store lock and
again after provider verification, before credential writes. A different account,
logout, or new sign-in cancels automatic application; normal token rotation of
the original grant is accepted. The guard is retained across Force retries.
An explicit re-login's new saved grant is not replaced with the old live tokens
when the person and session metadata happen to match. Failed or cancelled
application keeps the completed saved login and reports that reload did not
complete. These checks do not create an atomic transaction with external CLI
writers; the scope and limitations of the existing switch still apply.

`tests/test_codex_machine.py` exercises the picker with fictitious sign-ins and
a simulated daemon, covering current/inactive accounts, interrupted sign-in,
changed live accounts/grants, workspace mismatches, normal token rotation,
no daemon, restart failure, and explicit temporary-chat discard/cancellation.
The optional native runtime test additionally replaces a current account's
grant through the guarded switch, verifying the new auth file and unchanged
saved-chat IDs, titles, directories, and native clients after daemon restart.
