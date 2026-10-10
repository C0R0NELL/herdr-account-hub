# Account Hub

Account Hub is an independent fork of
[herdr-account-switch](https://github.com/rcosteira79/herdr-account-switch).
Its plugin ID is `c0r0nell.account-hub`; the original remains
`rcosteira.account-switch`. Both can be registered at the same time, with
separate profile, selection, cache and backup directories. Account Hub can
use the same Codex credential store when an account is applied; their saved
registrations remain independent.

Account Hub keeps its private files in `.local/` inside its own checkout.
It uses `ACCOUNT_HUB_` settings and does not use the original plugin's settings
or Herdr's shared plugin state/config directories.

Select saved Claude Code / Codex accounts inside [Herdr](https://herdr.dev).
For Codex, select an account, press **Enter**, and confirm with **Enter**. The
account applies to this machine's Codex home and open chats reload automatically.

## What it does

- **Independent registrations** use a local UUID. Adding an account creates a
  new record, even when email, provider identity or Team workspace are the same.
  Saving the current login does not duplicate an already registered account.
- **One Codex account for this machine.** The picker writes the selected login
  to `auth.json` and runs the official `codex app-server daemon restart` command.
  The fresh daemon loads authentication without the old in-memory cache; native
  CLI clients reconnect to their existing chats. No conversation ID input,
  special launch command, separate pane or per-chat account is required.
- **Shared history** stays in `CODEX_HOME`, or `~/.codex` when unset. The existing
  `CODEX_SQLITE_HOME` and configured `sqlite_home` stay in effect. Switching does
  not copy rollouts, rebuild databases, create account-specific homes, fork
  conversations or rename threads.
- **Verified application.** Account Hub checks the restarted server's account
  with `account/read`, waits for previously loaded chats to return and checks
  their IDs, custom titles and working directories. It reports success only
  after confirmation. On failure it attempts to restore the previous login and
  reload that account, reporting explicitly when recovery cannot be confirmed.
- **Natural token renewal.** Codex continues managing its ordinary saved OAuth
  login and renewal. Account Hub does not replace it with an external token
  provider or keep another authentication server running.

### Scope and requirements

Requires Python 3.11 or newer and Herdr 0.8.0 or newer. Claude Code support is
beta; its registration tests use fictitious credentials, not a real Claude account.

Automatic reload requires **Codex CLI and daemon 0.162.0 or newer**, using the
managed local daemon and the default OpenAI provider. This covers ordinary
`codex` and `codex resume` terminals sharing the selected `CODEX_HOME`.
Different users, other Codex homes, remote servers and embedded `--no-daemon`
executions are outside that shared daemon's scope.

The credential backend must be `cli_auth_credentials_store = "file"` (the
Codex default). Keyring/ephemeral credentials and custom model providers are
refused before overwriting the file. If the daemon is not running, the plugin
installs the selected login for future chats and does not start a daemon.
A failed passive probe is never treated as success.

The official restart may wait for running work during Codex's configured
shutdown grace period. In Codex 0.162.0, `shutdownGraceSeconds` in
`$CODEX_HOME/app-server-daemon/settings.json` defaults to 60 seconds and accepts
0–300 seconds. If the daemon is still busy at the deadline, Codex forces it to
stop; an unfinished task or compaction can be interrupted. The picker explains
that the daemon waits for active work. Reload and verification can take
additional time. Account Hub reads this setting without changing it. The switch
uses the installed CLI's lifecycle commands and the
[official app-server protocol](https://learn.chatgpt.com/docs/app-server).
The Unix WebSocket transport is documented as experimental; Account Hub checks
compatibility and never silently falls back to changing disk credentials only.

## Install

```sh
herdr plugin install C0R0NELL/account-hub
```

Herdr downloads the repository into its managed GitHub plugin directory and
registers the root `herdr-plugin.toml`. The plugin uses that checkout's location;
it does not create or require a `GITHUB_REPOSITORIOS` directory. This repository
is currently private, so Git must be authenticated with an account that has
access. The same install command applies if the repository is made public.

To see the actual install directory on your machine:

```sh
herdr plugin list --plugin c0r0nell.account-hub --json
```

The response's `plugin_root` is the directory containing the code and `.local/`
private data. Herdr replaces managed checkouts on reinstall and removes them on
uninstall. Back up `.local/` before either operation, then restore it into the
new checkout after reinstalling. Never commit that backup.

### Local development

Developers can keep their clone anywhere and register it directly:

```sh
git clone https://github.com/C0R0NELL/account-hub.git /path/to/account-hub
herdr plugin link /path/to/account-hub
cd /path/to/account-hub
python3 -B test_switcher.py
```

Edit the linked checkout and close/reopen an existing picker to test Python
changes before committing. No push, pull, or copy to another checkout is needed.
Re-run `herdr plugin link /path/to/account-hub` when the manifest changes.
Linking is a local developer choice; other users continue to use `plugin install`.

To give development its own entry in the plugin manager, run this from the
checkout with Python 3.11 or newer:

```sh
python3 scripts/link-dev.py
```

This generates and links `herdr-plugin.dev.toml`, ignored by Git, with the name
`dev-account-hub` and ID `c0r0nell.dev-account-hub`. It executes this checkout's
code and keeps its private data in this checkout's `.local/`. The official
`herdr-plugin.toml` keeps the name `Account Hub` and ID `c0r0nell.account-hub`
for commits and GitHub installations. Re-run the helper when the official
manifest changes; Python code changes are used when you reopen the picker.
Use `c0r0nell.dev-account-hub.open` for a development picker keybinding.

If this checkout was previously linked with the official ID, first link the
development entry, update any keybindings to its ID, and then run
`herdr plugin unlink c0r0nell.account-hub` to unregister the old local entry.
Keep any separately installed GitHub version registered: the two IDs can coexist.

Herdr refuses a GitHub install over a locally linked plugin. To return to a
managed installation, use `herdr plugin unlink c0r0nell.account-hub`, then the
install command above. Unlink leaves the development checkout and its data alone;
the managed installation starts with its own `.local/` directory.

### Save accounts

Then, with the account you use most logged in:

```sh
herdr plugin action invoke save --plugin c0r0nell.account-hub
```

That saves the current login if its person/workspace is not already registered.
Pressing **s** for an existing account shows `Account already saved`, without
prompting for a name, creating a duplicate or replacing saved credentials.
To recover a revoked registration, select it and use **a** to sign in again;
saving the current login does not repair revocation.

To add an account, press **u** in the picker,
choose **Codex** or **Claude (beta)** with the arrow keys, and press Enter. Supply
an optional label in **Name** and confirm with Enter to start the official sign-in.
The plugin saves the completed login as a new UUID automatically; no separate
**s** is needed.
The currently active account and chat history stay in place until you apply a
registration with Enter. **c** remains a compatibility shortcut for adding Codex.

Codex uses its official device-code sign-in in an empty temporary credential
folder. Claude uses `claude auth login --claudeai` with a temporary
`CLAUDE_CONFIG_DIR`, following its [documented configuration-directory
isolation](https://code.claude.com/docs/en/authentication#log-in-with-multiple-accounts).
Claude automatic registration is beta, tested with fictitious file credentials,
and currently unavailable on macOS because its directory-scoped Keychain
capture is not implemented. Existing Claude logins can still be saved with **s**.

Both sign-in flows return to the picker when the official CLI exits. Follow the
CLI's displayed URL/code instructions; Ctrl+C cancels, and the plugin stops the
sign-in after 15 minutes. Canceling any dialog with Esc starts no login. Failed,
canceled or timed-out sign-ins create no registration and discard the temporary
credential folder. The native flow may impose a shorter expiry of its own.

Do not run successive `codex login` commands in the shared home to collect
accounts. In Codex 0.162.0, starting that command revokes the existing OAuth
login before the new sign-in, so a previously saved copy may stop working.
Saving an auth file does not protect its grant from server-side revocation.

To reauthenticate an existing Codex registration, select it and press **a**,
then confirm. Sign in to the same person and workspace; a mismatched login is
refused without replacing that registration. After sign-in, **Enter** applies
the account through the ordinary machine-wide switch. If device login is not
available for your workspace, set `codex_login_method = "browser"` in the
plugin config to use the official browser flow instead.

The corresponding terminal commands are:

```sh
python3 /path/to/account-hub/switcher.py codex-login --label Work
python3 /path/to/account-hub/switcher.py codex-login PROFILE_UUID
# Use the official browser flow when device auth is unavailable:
python3 /path/to/account-hub/switcher.py codex-login PROFILE_UUID --browser
```

The temporary folder is discarded after sign-in. The plugin retains credentials
only in `.local/profiles/codex/` or `.local/profiles/claude/`. It never opens chats
in those temporary homes.

The selected profile is stored by UUID, separately from its credentials. Email,
user ID and workspace ID are metadata, never keys for replacing a registration.
Duplicate names are allowed; the picker shows the UTC save time to distinguish
them. Scripted commands can use the UUID printed by `save`, `status` or `list`
when a name is ambiguous. Renaming keeps the UUID and credential file unchanged.

### Switch the Codex account

Open Account Hub from Herdr's plugin manager. Select the saved Codex account and
press **Enter**. A centered dialog asks:

```text
Switch Codex account
Switch to Work ?
Reload open chats; keep history and titles.
enter | esc
```

Press **Enter** to apply, or **Esc** to cancel without changing anything.
After the success message, return to your existing chat. Its native Codex
interface reconnects to the restarted daemon using the same conversation.
All chats on that shared daemon use the selected account for subsequent work.

The scriptable command performs the same machine-wide transaction without an
interactive confirmation:

```sh
python3 /path/to/account-hub/switcher.py switch codex PROFILE_UUID
```

The UUID identifies a saved account, not a conversation. A unique profile label
also works. The picker supplies it automatically; you do not need to copy IDs.
The `next` action cycles the machine's account through the same verified flow.

### Keybindings (`~/.config/herdr/config.toml`)

Run `herdr server reload-config` after editing these:

```toml
[[keys.command]]
key = "prefix+a"              # cycle the focused agent's kind to its next account
type = "plugin_action"
command = "c0r0nell.account-hub.next"
description = "next account"

[[keys.command]]
key = "prefix+shift+a"        # open the picker
type = "plugin_action"
command = "c0r0nell.account-hub.open"
description = "account picker"
```

To jump straight to one named account, call the script instead — actions take
no arguments. `herdr plugin list --plugin c0r0nell.account-hub --json`
reports where the plugin is installed:

```toml
[[keys.command]]
key = "prefix+1"
type = "shell"
command = "python3 /path/to/account-hub/switcher.py switch claude work"
```

## Account details

Open the account picker and press **d** to expand the usage windows for every
saved account. Codex appears first, followed by Claude Code (beta). This is the
single usage view in Herdr's plugin menu; there is no separate Usage action.

```
Codex
  ● main          Session    42%  ████░░░░░░  limit in 1h30 (resets in 2h)
                  Weekly     18%  ██░░░░░░░░  ~34% left (resets in 4d01h)
Claude Code (beta)
  ● main          Session    16%  ██░░░░░░░░  limit in 2h10 (resets in 10m)
                  Weekly     65%  ███████░░░  limit in 1d12h (resets in 4d02h)
                  Fable       6%  █░░░░░░░░░  ~88% left (resets in 4d02h)
    spare         Session     0%  ░░░░░░░░░░  —
                  Weekly      2%  ░░░░░░░░░░  ~92% left (resets in 5d07h)
```

In the picker, `●` identifies the credential store's selected login.
`list.active` has the same scope (`active_source = "credential_store"`).
The terminal diagnostic `python3 switcher.py status` also shows the reload
confirmation. The point of
listing the others is the case above: `main` runs out before its session renews,
and `spare` has room now.

**r** refreshes the accounts and usage, **d** returns to the compact view, and
**Esc** closes the picker.

Opening the picker requests fresh usage and reset counts, even when its cache
is recent. While it stays open, it checks the account list and redraws every
five seconds, and refreshes usage every two minutes by default (`usage_ttl_s`).
Press **r** for an immediate usage refresh. Opening, automatic and manual reads
all respect provider cooldowns.

### Earned Codex resets

Codex rows have a second column to the right of usage: `3 resets`. The count
comes from the same account-specific usage response and shares its refresh
interval. `-` means the service has not supplied a valid count or the account's
API read failed. Counts use `0 reset`, `1 reset`, and `2 resets` or more.
`~3 resets` marks an aged reading without a reported API error. Claude rows
leave this column empty.

Select an account with the arrow keys and press **t** (next to **u** in the
help line). The plugin fetches that account's reset details and chooses the
available reset with the nearest expiry, randomly selecting among ties.
Expired, redeeming and unsupported reset types are excluded. Dated credits
are considered before credits whose expiry is not provided. If the service
caps the details so the earliest expiry cannot be determined, no reset is used.

```text
Use reset
Use a reset for Work ?
Expires: 2026-10-22 19:09 UTC
enter | esc
```

Enter consumes the selected reset; Esc cancels. This uses the selected
registration's credentials without switching the machine's active account or
restarting chats. The plugin then reads the provider's usage and reset count
again, rather than assuming that usage became zero or decrementing a count.
After a successful reset it also schedules up to three additional usage reads
for that registration, bypassing the normal two-minute cache interval. The
picker runs these on its existing idle ticks: a response that still contains
the old quota, or only one updated window, does not require pressing **r**.
The follow-ups respect provider cooldowns and never repeat the consumption.
The provider can return `nothing_to_reset` or `no_credit`; these are shown as
no reset used. An `applicable_available_count` hint does not block the attempt.

An operation UUID is saved in `.local/reset-attempts.json` **before** sending a
consumption request. If its response is lost, pressing **t** again offers the
same credit with the same operation UUID, including after closing/reopening
the picker. This follows the official idempotency rule and prevents a retry
from spending a second credit. Concurrent confirmations for the same
registration are serialized; a stale confirmation must check again.

The capability and outcomes are documented in the official
[Codex app-server documentation](https://learn.chatgpt.com/docs/app-server).
Account Hub uses the authenticated HTTP routes implemented by the official
[Codex 0.162.0 backend client](https://github.com/openai/codex/blob/rust-v0.162.0/codex-rs/backend-client/src/client/rate_limit_resets.rs),
alongside its existing usage reader. These HTTP routes are an internal Codex
contract and may change between versions.

### Where the numbers come from

The switcher fetches usage independently with each saved account's own
credentials. It does not read ccstatusline's cache or require a status-line
plugin. Concurrent switcher panes share a cache and serialize requests so
they do not fetch the same expired reading at once. Open panes pick up newer
readings from other switcher actions on their UI ticks.

Each provider publishes the account's own allowance:

```
GET https://api.anthropic.com/api/oauth/usage        # claude
GET https://chatgpt.com/backend-api/wham/usage       # codex
```

Both are unofficial and can change or vanish. A read that fails leaves the row
saying so rather than showing a stale number as if it were current.

### Reading an account you are not on

Only the live account keeps a fresh token; a parked profile's has usually
expired. Reading one therefore renews it first, exactly as the CLI does on any
expiry, and as [openusage](https://github.com/robinebers/openusage) does to keep
its own panel current.

**Renewing spends the stored refresh token.** The reply carries a new pair and
the old one dies, so losing that reply would cost you a browser login. The order
guards against it: back up the profile, call the endpoint, write the reply to a
sidecar the instant it lands, rewrite the profile, clear the sidecar. A run that
dies mid-way is adopted by the next one. An access token lasts hours, so this is
roughly one renewal per account per token lifetime, not one per refresh.

`ACCOUNT_HUB_USAGE_RENEW=0` turns it off; parked accounts then show their
last-seen numbers with an age, and the live ones stay accurate.

A last-seen reading still projects. Each window records when it was read, and
the spend rate is worked out against that moment rather than against the clock
now — so an old reading brings its limit *closer* as it ages, instead of the
frozen percentage making a busy account look calmer the longer nobody looked.
The age beside the account is what says how much to trust it.

### Colours

| var | default | meaning |
|-----|---------|---------|
| `ACCOUNT_HUB_USAGE_THRESHOLDS` | `60,85` | percent at which a window turns warn, then crit |
| `ACCOUNT_HUB_USAGE_COLORS` | `ok=green,warn=yellow,crit=red,stale=blue` | the palette |
| `ACCOUNT_HUB_USAGE_BAR` | `█░` | filled and empty bar characters |
| `ACCOUNT_HUB_USAGE_BAR_WIDTH` | `10` | bar width in cells |

The overlay and the `--color` text form read the same palette, so they agree.

## Picker keys

```
j / k or ↓ / ↑   move selection
enter            switch account (Codex: centered Enter/Esc confirmation, then global reload)
c                compatibility shortcut: add a Codex account
s                save current acc (already saved: message only; new: asks for a name)
a                sign in again to selected Codex account / capture current Claude login (confirms)
r                refresh the whole account list and every account's usage
g                rename the selected profile
x                delete the selected profile, including the one in use
d                show / hide every usage window, not just the short one
u                add new: choose Codex / Claude (beta), then sign in and save automatically
t                use selected Codex account's earliest-expiring earned reset (Enter/Esc confirms)
q / esc          close
```

Each account carries how much of it is left, so the picker answers "which one
should I switch to" on its own:

```
 ● main             █████░░░  66% 2h00↺  you@example.com | max
   spare            ░░░░░░░░   2% 4h47↺  you@work.example | team
```

That column is the **short** window — the session one, five hours on both
providers. It is the window you meet again and again in a working day, so it is
the one worth the single figure. A weekly window at 9% is not what stops you,
whatever it projects about six days out.

Ranking the windows against each other was tried and does not work: a window
that will not fill projects nothing, drops out of the comparison, and hands the
column to a nearly empty weekly window. One account read 11% with almost seven
days left while its session sat at 69%.

One exception. A **spent** window takes the column, because once one is full the
account is blocked and that outranks how the session is doing.

The compact countdown always shows the selected window's automatic reset and
carries `↺`: `2h00↺` means that window renews in two hours. If the provider has
not supplied a reset time, it shows `—`. Estimated exhaustion times (`limit in`)
appear only in the detailed view opened with `d`.

Every window is still there — `d` swaps the column for all of them, each with its
own projection, one account per block:

```
Claude Code
 ● main                                  you@example.com | max
                    Session    11%  █░░░░░░░░░  ~77% left (resets in 2h37)
                    Weekly     66%  ███████░░░  limit in 1d13h (resets in 3d23h)
                    Fable       7%  █░░░░░░░░░  ~84% left (resets in 3d23h)

   spare                                 you@work.example | team
                    Session     0%  ░░░░░░░░░░  —
                    Weekly      2%  ░░░░░░░░░░  ~92% left (resets in 5d04h)
                    Fable       0%  ░░░░░░░░░░  —

Codex
 ● main                                  you@example.com | plus
                    Weekly     66%  ███████░░░  limit in 1d13h (resets in 3d23h)
```

The account line drops its own figure while expanded, because the windows below
already carry it. The picker omits column titles and uses `|` between hints and
identity fields.

**Fable** is a per-model weekly limit. The API calls it `weekly_scoped` and puts
the model in a `scope` field, so the model's name is shown instead of the key.

**Codex shows every window the account reports**, including its five-hour and
weekly limits when available. The compact row summarizes the shortest window,
or a spent window if one is blocked; `d` shows the individual readings.

The detailed view's projections show where the window lands if you keep
spending it as you have been. `~70% left` finishes with room
to spare; `limit in 1d13h (resets in 4d00h)` runs out three days early — 66%
used is comfortable with a day to go and a problem with five. The reset comes
along in brackets, because a projection means nothing without knowing what it is
racing.

It is worked out from the window's length and how much of it has already passed,
so it says nothing until the window has run long enough to mean something: a
window minutes old would otherwise claim the limit is imminent. A window already
at 100% reads `spent`.

The picker requests a fresh read when opened, then refreshes at the cache
interval (two minutes by default), because the endpoints rate-limit. `r`
refreshes the entire picker and requests a fresh read even during that cache
interval, while still respecting the provider's cooldown. Newly added
registrations are read on the next picker tick. A successful switch verification
also saves its usage reading for other cache-aware actions and UI ticks.

If a read fails or ages past the cache lifetime, the picker keeps the bar and
countdown and marks the last known percentage with `~` (for example, `~100%`).
Press `d` to see the reading's age and refresh error. A failed read keeps its
warning on subsequent UI ticks until a successful read clears it. Automatic
refresh while the picker stays open respects the cache lifetime, except for
short follow-up checks after a reset; every refresh respects the provider's
`Retry-After` delay, falling back to five minutes when no usable delay is supplied.

An account with a known error has a red **!** to the left of its name, including
when selected or expanded. This covers refused/revoked credentials, reporting
failures and rate limits; **d** explains the reason. A successful read clears a
temporary reporting error. A known refused saved login remains marked even if
a different live login for that person can read usage, until those saved
credentials change or verification of that saved login succeeds. A high usage
percentage alone is not a login failure and does not show **!**.

The picker shows the saved account count and scrolls with `j/k` or the arrow
keys when the list or expanded readings exceed the pane height. Selection
follows the registration UUID across list updates. Renewing credentials does
not change the order of registrations.

`s`, `a`, `g` and `x` each open a dialog in the middle of the screen. Deleting removes
this plugin's saved copy of a login and nothing else: the credential store the
CLI reads is never touched, so you stay logged in. Delete the profile for the
account you are on and the next switch away re-saves it under an `autosaved-…`
name.

`●` marks the selected UUID whose saved login matches the live credential
store. The header shows *"live: … (press 's' to save this account)"* only when
the current person/workspace is absent from the saved accounts. A fresh login
for an already registered account does not show that prompt. Usage reads can
use that account's current live token without overwriting its saved registration
or choosing among duplicate UUIDs.

The section titles are `Codex` and `Claude Code (beta)`, in that order. Enter and confirmation
apply the selected Codex account, reload the daemon and verify its account;
the action reports the result.

## What it reads and writes

| kind | credential store | identity |
|------|------------------|----------|
| claude | Keychain item `Claude Code-credentials` (macOS), else `~/.claude/.credentials.json` | the `oauthAccount` block in `~/.claude.json` |
| codex | `~/.codex/auth.json` | `account_id` + the `id_token` claims inside it |

`CLAUDE_CONFIG_DIR` and `CODEX_HOME` are honoured if you set them. On claude,
**only** the `oauthAccount` key of `~/.claude.json` is touched — projects,
history and onboarding state are rewritten verbatim.

New profiles live in `<checkout>/.local/profiles/<kind>/<uuid>.json`, mode
`0600` inside a `0700` directory. They contain email/provider metadata and
password-equivalent credentials. `selection.json` stores the selected UUID per
kind; usage caches and rotating-token sidecars are keyed by UUID too.

Existing profiles can be read in place without moving or copying their files.
Their local filenames supply stable compatibility UUIDs, independently of
provider identities. No record is selected by matching an email or workspace;
select an existing profile explicitly. The UUID is persisted when that record
is next written. Sessions, conversation history and terminal titles are outside
the registration and reauthentication workflow.

### Repository privacy

Private data lives in this checkout's `.local/` directory. Its `profiles/`,
`backups/` and `rotated/` directories all contain credentials. Its `config/`
directory contains only Account Hub's settings. The live Codex
login remains in `~/.codex/auth.json` (or `$CODEX_HOME/auth.json`). These files
are not source code and must not be published.

The repository's `.gitignore` excludes all of `/.local/`, plus local credential
files, `.env` files and account-state directories copied into the checkout. Ignore rules do
not remove files already committed or protect secrets pasted into tracked code.
Tests use temporary directories and fictitious logins only.

## Not losing a login

Swapping a credential store is the one operation here that can actually cost
you something, so every switch:

1. takes an `flock`, so two switches (or a switch racing your own keybinding)
   can't interleave writes while a CLI is refreshing its token;
2. **asks the provider whether the saved login still works**, while the live
   one is still in place. Reading the store back proves the write landed. It
   says nothing about whether that account can still sign in, and the two come
   apart exactly when it matters: a login the provider has retired installs
   cleanly, reports success, and signs you out. Only a definitive refusal stops
   a switch — the provider answering 401 to a token it has just issued, or
   turning the refresh token down in words. Being offline, timing out and
   getting rate limited prove nothing either way, and none of them block a
   switch. Set `verify_switch = false` to skip the check;
3. **auto-saves the outgoing login** if it isn't associated with a selected UUID — you can
   never overwrite an account the plugin doesn't already have a copy of — and
   **refreshes its snapshot** if it is. The CLI keeps renewing the live tokens,
   so a profile saved hours ago holds older ones; parking the account without
   updating it can leave a copy that is no longer able to renew, and that costs
   a browser login to repair;
4. writes a timestamped backup of it under `<checkout>/.local/backups/`
   (last 10 kept);
5. reads the store back and compares the account identity, and **puts the
   previous login back** if the write didn't take.

An account already logged in but never saved shows up in the picker as
*unsaved*; press `s` before switching away if you want to name it yourself
rather than take the `autosaved-…` name.

## Config

Settings live inside the plugin's own checkout:

```text
<plugin-root>/.local/config/config.toml
```

```toml
# Percent at which a usage window turns amber, then red.
usage_thresholds = "50,75"

# Usage refresh interval in seconds for the account picker.
usage_ttl_s = 120

# Override one colour of the four: ok, warn, crit, stale.
usage_colors = "warn=magenta"

# Renew a parked account's token so its usage can be read. Turning this off
# leaves parked accounts showing their last-seen numbers with an age.
usage_renew = false

# Ask the provider whether a saved login still works before installing it.
# Turning this off restores the old behaviour: a retired login installs
# cleanly and signs you out.
verify_switch = false
```

The key is the variable below without its `ACCOUNT_HUB_` prefix, lowercased.
`config.json` works too if you prefer it. A file that fails to parse is ignored
rather than fatal, and the defaults stand.

`ACCOUNT_HUB_STATE_DIR` and `ACCOUNT_HUB_CONFIG_DIR` can explicitly override
these paths. Herdr's injected `HERDR_PLUGIN_STATE_DIR` and
`HERDR_PLUGIN_CONFIG_DIR` are ignored; they cannot redirect the Hub into another
plugin's storage.

An environment variable still wins over the file, but is rarely the practical
choice: plugin actions inherit the **herdr server's** environment, so setting
one means exporting it before herdr starts. For a direct script invocation,
variables can be set inline on the command.

| var | default | meaning |
|-----|---------|---------|
| `ACCOUNT_HUB_NOTIFY` | `1` | herdr notification on each switch, and on a refused one |
| `ACCOUNT_HUB_VERIFY_SWITCH` | `1` | ask the provider whether a saved login still works before installing it |
| `ACCOUNT_HUB_USAGE_RENEW` | `1` | renew a parked account's token so its usage can be read |
| `ACCOUNT_HUB_USAGE_TTL_S` | `120` | how long a usage answer is reused |
| `ACCOUNT_HUB_SETTLED_PERCENT` | `5` | spend this much of a window and its rate counts, however new it is |
| `ACCOUNT_HUB_RENEW_MARGIN_S` | `300` | renew this far ahead of a token's expiry |
| `ACCOUNT_HUB_COMMAND_TIMEOUT_S` | `30` | how long any one command may take before it is abandoned |
| `ACCOUNT_HUB_LOCK_TIMEOUT_S` | `5` | how long to wait for another switch before refusing |
| `ACCOUNT_HUB_CODEX_LOGIN_METHOD` | `device` | official Codex sign-in flow: `device` or `browser` |
| `HERDR_BIN_PATH` | `herdr` | herdr binary (set by herdr when it invokes an action) |


## What the providers do to your logins

### Rotation updates the selected UUID; reauthentication is explicit

A switch is not the only moment the live tokens change. The CLI renews them as
it works, and `claude login` or `codex login` replaces them outright. Waiting
for a switch to copy that back left a profile holding a credential the provider
had already retired, while the live one beside it worked — and the next switch
would install the dead copy.

Reads of the accounts sync token rotation only into the explicitly selected
UUID, after checking provider identity. For Codex, session claims or a token
still in common must also associate the live login with that record. If the
association cannot be established, no registration is updated automatically.
Other registrations remain untouched, including duplicates of the same identity.

To sign in again to an existing Codex registration, select it and press `a`.
This uses official sign-in in an empty temporary credential folder, preserving
the currently active account until you press Enter to apply the result.

If you have already completed a fresh external CLI login and want to capture
that existing auth file into one specific registration, run:

```sh
python3 switcher.py reauth codex <profile-uuid>
```

This captures the current login into only that UUID, preserves its name and
creation time, clears its old usage/staged tokens, and selects it. It does not
launch login or change the live credential store. `reauth` rejects labels and
requires an existing UUID. `save` creates a UUID only for an account not already
registered; an existing account produces `Account already saved`.

A renewal counts as a change too: renewing a parked profile's tokens moves its
`saved_at`, so anything watching that field can tell the credential was
repaired. `autocontinue` remembers a refused login exactly that way.

A saved login can stop working while it sits on disk, and the snapshot gives no
sign of it: it still parses, still installs, and only fails when an agent uses
it. Four provider rules cause that, and they are why everything under "Not
losing a login" above works the way it does — refresh tokens are single-use and
rotate, a refusal counts only when the provider words it, an upgraded account
looks exactly like a revoked one, and starting a new Codex CLI login revokes the
OAuth grant already stored in that home. Collect registrations using the
plugin's empty temporary login store instead.

Written up once, with the refusal codes and how each was established:
[`docs/solutions/runtime-errors/saved-login-revoked-outside-the-plugin.md`](docs/solutions/runtime-errors/saved-login-revoked-outside-the-plugin.md).

### A refused switch says so

The picker prints a refusal on its own status line. Other account-changing
actions also send the reason as a Herdr notification so failures remain visible
when the command runs through a keybinding.

`next`, `switch`, `save` and `reauth` send the reason as a herdr notification. A
completed switch already sent one, so the two match. `ACCOUNT_HUB_NOTIFY=0`
turns both off. Polling commands such as `list` send no notifications.

Where that notification appears is herdr's setting, not this plugin's.
`[ui.toast] delivery = "system"` hands it to macOS, which drops it when the
alert style is off. `delivery = "herdr"` draws it inside herdr instead, and
needs no permission from macOS.

## Fragility

Credential layouts are the agents' private business and can change without
notice. If a switch stops taking, `python3 /path/to/account-hub/switcher.py status`
prints what each store currently reports; the store
paths and the shape read out of them are the two constants at the top of
`ClaudeBackend` / `CodexBackend` in `switcher.py`.

A Codex install that uses the OS keyring rather than `auth.json` is not
supported. Account Hub refuses that switch before writing the file.

## Requirements

- herdr ≥ 0.8.0
- Python 3.11+ (stdlib only; uses `curses` for the picker)
- Codex CLI and managed local daemon ≥ 0.162.0 for automatic Codex reload
- macOS or Linux

## The other herdr plugins

Each installs on its own; they share nothing but an author.

- [**herdr-idle-shell-badge**](https://github.com/rcosteira79/herdr-idle-shell-badge) — Badges idle agents that still have a background shell running, so one that *looks* done but left a process alive isn't mistaken for finished.
- [**herdr-readpending**](https://github.com/rcosteira79/herdr-readpending) — Mark agents you started reading but haven't finished — a numbered badge plus a reorderable overlay queue that clears when you focus the agent.
- [**herdr-autocontinue**](https://github.com/rcosteira79/herdr-autocontinue) — Watch agents for usage-limit walls, badge the countdown to the reset, and re-prompt the agents you armed once the window reopens.
