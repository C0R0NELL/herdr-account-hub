# Account Hub guide

Start with the [README](../README.md) for an overview. This guide describes
Account Hub's current implementation in [switcher.py](../switcher.py).

## Installation

```sh
herdr plugin install C0R0NELL/herdr-account-hub
```

Herdr registers the official [manifest](../herdr-plugin.toml). The plugin uses
Python's standard library and needs no compilation. An install preparation hook
migrates older local data before checkout replacement. Python 3.11+ is required; the
manifest declares supported platforms and the minimum Herdr version. A private
repository requires Git authentication with access to that repository.

Open Account Hub from Herdr's plugin manager. Installation does not save a login
or start sign-in. Save your current login in the picker, or add another account
through its provider chooser. The available controls are shown in the interface.

Click anywhere on an account row to select it and open the switch confirmation.
The Codex reset column, including its padding, opens the reset confirmation with
the nearest expiry instead. The underlined Enter/Esc labels in all picker modals
can be clicked to confirm or cancel. Provider options can be selected by clicking
their rows. Keyboard controls remain available when the terminal does not support
mouse reporting.

The bottom-right **Coffe <3** link opens [GitHub Sponsors](https://github.com/sponsors/C0R0NELL?frequency=one-time&sponsor=C0R0NELL).
The link uses a terminal hyperlink; open it with Ctrl+click or Cmd+click,
depending on your terminal.

To locate the installed code, inspect `plugin_root`:

```sh
herdr plugin list --plugin c0r0nell.account-hub --json
```

## Registrations and authentication

Each registration gets a local UUID and a separate credential file. Different
emails in the same Team workspace remain separate. Within each provider, email
is compared without surrounding spaces or letter-case differences to detect an
existing account. Saving or adding an email already registered skips the duplicate
and preserves the original UUID and credentials. Codex and Claude registrations
remain separate even when they share an email.

Saving the current login skips an already registered email. It does
not replace saved credentials or repair a revoked login. To repair an existing
Codex registration, select it and use the picker's sign-in action. That flow
checks the person and workspace before replacing that UUID's credentials.

The current-account dot follows the email in the CLI's local login, even after
signing in outside the plugin. This display lookup does not overwrite credentials
or establish that an existing daemon has reloaded them. Token synchronization
still requires an explicitly selected UUID and matching authentication grant.

Adding an account runs the official agent CLI in an empty temporary credential
folder, then saves a completed login automatically. The live login stays in
place until you explicitly switch. Temporary folders are discarded afterward
and never used for chats. Follow the terminal's sign-in instructions; cancellation,
failure, or timeout creates no new registration.

Codex uses device-code sign-in by default. The plugin configuration can select
browser sign-in instead. For terminal use, run these from the repository root:

```sh
python3 switcher.py codex-login --label Work
python3 switcher.py codex-login PROFILE_UUID --browser
```

`PROFILE_UUID` identifies a saved account. Labels may be duplicated, so use the
UUID when addressing one registration explicitly.

## Codex switching

Switching applies the selected credentials to `CODEX_HOME/auth.json`, using
`~/.codex` when `CODEX_HOME` is unset. The plugin checks compatibility before
writing credentials. Its current reload implementation requires Codex CLI and
daemon 0.162.0+, a managed local daemon, the OpenAI provider, and
`cli_auth_credentials_store = "file"`.

If the daemon is running, Account Hub invokes `codex app-server daemon restart`
and verifies the adopted account and the return of previously loaded chat IDs,
titles, and working directories. Existing history and SQLite configuration remain
in place. Switching does not copy history, rename chats, or create per-account
conversation homes.

The change covers chats sharing that Codex home and managed daemon. Other users,
other homes, remote servers, and `--no-daemon` executions have separate scopes.
If the daemon is absent, the selected login is installed for future chats without
starting one. A failed probe is reported rather than treated as proof of absence.

Restart can wait for active work according to Codex's shutdown settings. Work
that exceeds the daemon's shutdown deadline may be interrupted. Account Hub reads
those settings without changing them. A failed switch attempts rollback and
reports when restoring the previous account cannot be confirmed.

Loaded temporary Codex chats pause switching because they have no persisted
history to resume after restarting the daemon. The picker lists every blocker
as `[1]`, `[2]`, etc., with its directory, title (or `Untitled`) and a short chat
reference. Scroll with the arrow keys. **Force** (click or `f`) discards those
temporary chats during the normal Codex daemon restart and continues the account
change; their unsaved content is lost. **Esc** cancels, and Enter does not force.
The discard confirmation is checked again before changing credentials and
before restarting; new temporary chats require another confirmation. Saved
chats retain the normal identity/history/reconnection checks. This restarts the
shared Codex daemon; it does not close Herdr panes or delete saved conversations.

Implementation evidence and limitations are recorded in the
[reload investigation](solutions/runtime-errors/codex-machine-account-reload.md).

## Claude Code (beta)

Claude registration is tested with fictitious credentials; a real-account workflow
has not been validated. Automatic registration currently uses an isolated
file-backed `CLAUDE_CONFIG_DIR` on Linux. It is unavailable on macOS; existing
Claude logins can still be saved through the picker.

Claude switching writes the credential backend and `oauthAccount` metadata while
preserving other configuration. It does not restart or verify already running
Claude chats. Its reauthentication action captures the current CLI login into the
selected registration.

## Usage and resets

The picker requests fresh usage when opened and refreshes while it remains open.
Manual refresh covers the whole list; every read respects provider cooldowns.
Temporary connection, timeout, and server failures get bounded follow-up reads
before returning to the normal refresh interval. Authentication failures do not
use these short retries, and a persistent failure keeps its error marker.
Reading an expired parked login can renew its tokens unless `usage_renew` is
turned off. Details show each reported window and any refresh error.

A red error marker indicates a known account problem. An aged reading is marked
with `~`; an unavailable earned-reset count is shown as `-`. A refused saved
login requires reauthentication even if a fresh live login can read usage for
that same person.

The column beside the Codex reset count shows the nearest available expiry as
`YYYY/MM/DD HH:MM UTC`. It shows `-` for zero resets, unavailable counts, or
missing/incomplete expiry details. Both columns keep their width so the account
email stays aligned. Positive counts fetch expiry details during usage refresh
unless the usage response already supplies them; detail failures preserve the
usage/count and detail rate limits respect the provider's cooldown.

Using a Codex reset offers the available credit with the nearest expiry, including
its time, and chooses randomly among equal expiries. Confirmation consumes it
without switching the live account. The plugin rereads usage and counts afterward.
Retries of a lost response reuse the persisted operation UUID, preventing another
credit from being spent just because the response was lost.

See the [reset investigation](solutions/runtime-errors/codex-earned-reset-idempotency.md)
for provider outcomes and recovery behavior. Usage and reset routes are internal
provider contracts and may change.

## Private files and updates

Credentials and state belong to the plugin ID and stay outside its source
checkout. With the default Herdr directories, Account Hub uses:

```text
~/.local/state/herdr/plugins/c0r0nell.account-hub/profiles/<kind>/<uuid>.json
~/.local/state/herdr/plugins/c0r0nell.account-hub/selection.json
~/.config/herdr/plugins/config/c0r0nell.account-hub/config.toml
```

Caches, backups, token sidecars, and reset receipts share that state directory.
Local links and GitHub installations use the same official identity and storage
on a machine. Paths respect `XDG_STATE_HOME` and `XDG_CONFIG_HOME`, or the
state/config directories Herdr injects for the matching plugin ID.
`ACCOUNT_HUB_STATE_DIR` and `ACCOUNT_HUB_CONFIG_DIR` explicitly override them;
keep overrides outside source checkouts too.

An older `.local/` store is moved automatically at installation or first launch,
preserving UUIDs, credentials, selection, settings, caches, backups, and pending
reset operations. The original path becomes a compatibility link for open old
pickers. The install hook imports the old registered checkout before Herdr removes
it. Conflicting stores/configuration stop migration without overwriting either
copy. A move across filesystems also stops: close old pickers and migrate the data
outside the checkout before retrying the update. This preserves shared file locks
instead of copying a store while older processes may still use it.

Managed updates, uninstallation, and reinstallation leave the durable data in
place. Deleting these state/config directories explicitly still deletes it.
Private directories use `0700`; private files use `0600`. The tracked `.gitignore`
excludes the compatibility path and additional local manifests, but cannot
protect secrets already committed. Applying an account still changes the shared
live CLI store, independently of where Account Hub keeps its saved copies.

## Configuration

Optional settings are read from `config.toml` or `config.json` in the plugin's
configuration directory described above.
For example, choose browser sign-in or disable renewal during parked usage reads:

```toml
codex_login_method = "browser"
usage_renew = false
```

An `ACCOUNT_HUB_` environment setting takes precedence over its matching lowercase
configuration key. Herdr actions inherit the server's environment. Settings are
loaded when the Python process starts; reopen the picker after editing them.
The supported options and defaults are defined in [switcher.py](../switcher.py).

## Local editing and checks

Keep your editable checkout wherever you prefer. From its root:

```sh
herdr plugin link "$PWD/herdr-plugin.toml" --enabled
python3 tests/check_switcher.py
```

The link uses the official manifest, ID `c0r0nell.account-hub`, and name
**Account Hub**. If this ID already has a managed GitHub installation on the
machine, uninstall that registration first; its durable accounts remain in place.
Reopen the picker to load Python edits; relink after manifest changes. Use a
normal GitHub installation on another machine to verify released updates.
Linked checkouts read edited files directly and are excluded from update checks.

Checks live in [tests/](../tests/) and use temporary state and fictitious logins.
The check script runs the unit, terminal, and isolated release regressions. With Herdr installed,
run `python3 tests/check_managed_upgrade.py` to exercise its real managed installer
against a temporary local Git repository and fictitious accounts. Investigation notes in
[solutions/](solutions/) document established behavior and validation limits.
Release preparation and version proposals must follow [AGENTS.md](../AGENTS.md).
The [publishing notes](publishing.md) cover marketplace requirements and the
remaining distribution work.
