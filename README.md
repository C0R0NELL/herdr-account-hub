# Account Hub

Account Hub is an independent fork of
[herdr-account-switch](https://github.com/rcosteira79/herdr-account-switch).
Its plugin ID is `c0r0nell.account-hub`; the original remains
`rcosteira.account-switch`. Both can be registered at the same time, with
separate profile, selection, cache and backup directories. They still operate
on the same machine-wide Claude Code and Codex credential stores.

Hot-swap Claude Code / Codex logins from inside [herdr](https://herdr.dev),
without logging out and back in. For the case: your main account hits its
window at 11am, you have a second one, and you don't want to re-auth in a
browser to use it — or to find out three panes later which account the fleet
has been billing.

> **Tested with Claude Code and the Codex CLI only.** Those are the two
> credential stores it knows how to read, back up and write, and it has a
> backend for each. No other harness is supported: adding one means teaching it
> that harness's store, not configuring it.

## What it does

- **Named profiles** per agent kind. A profile is a snapshot of the credential
  store the CLI actually reads, identified by its own local UUID. Every save
  creates a new record, even for the same email, provider identity or workspace.
  Three people in the same Team workspace remain three independent records.
- **Switch** from an overlay picker, or cycle to the next account with one
  keybinding — no UI, no prompt.
- Both CLIs re-read their credential store on every request, so **agents that
  are already running switch too**, from their next turn. Nothing to restart.
- A **`$acct` badge** on every claude/codex pane naming the account in use, so
  the sidebar answers "which account is this burning" at a glance. It appears as
  soon as one profile is saved: `👤 work` for a saved login, `👤 work*` for a
  login no profile has a copy of, `👤 logged out` for none.

### No daemon

Unlike the other plugins here, this one is pure action — nothing runs in the
background. Credentials only change when you press a key, so there is nothing
to react to. The cost is that the `$acct` badge is refreshed at the moments the
plugin runs (a switch, a save, herdr startup, while the picker is open) rather
than continuously; a pane opened after the last switch shows no badge until the
next one. That is the whole trade, and it beats a daemon polling forever to
restate a value that changes twice a day.

## Install

```sh
gh repo clone C0R0NELL/account-hub ~/.local/share/herdr/plugins/account-hub
herdr plugin link ~/.local/share/herdr/plugins/account-hub
```

This repository is private, so cloning requires GitHub access. A development
checkout can also be linked with `herdr plugin link /path/to/account-hub`.
Pull code updates into the linked checkout and close/reopen an existing picker.
Re-run `herdr plugin link` when the manifest changes.

Then, with the account you use most logged in:

```sh
herdr plugin action invoke save --plugin c0r0nell.account-hub
```

That snapshots whatever is logged in right now. Log into the other account the
normal way (`claude` → `/login`, or `codex login`), run `save` again, and you
have two profiles to switch between.

The selected profile is stored by UUID, separately from its credentials. Email,
user ID and workspace ID are metadata, never keys for replacing a registration.
Duplicate names are allowed; use the UUID printed by `save`, `status` or `list`
when a name is ambiguous. Renaming keeps the UUID and credential file unchanged.

### Config (`~/.config/herdr/config.toml`)

**1. Show the badge.** `$acct` is a pane token, and a token renders only if a
sidebar row names it. Installing a plugin does not edit your config, so until
some row names `$acct`, the badge is invisible and herdr reports no error. The
`enable-badge` action does that one edit for you:

```sh
herdr plugin action invoke enable-badge --plugin c0r0nell.account-hub
```

It backs `config.toml` up first, merges `$acct` into the rows you already have
(and into any `rows_by_agent` override for claude/codex, since an override
replaces `rows` rather than extending it), then reloads the server. Run it
twice and the second run does nothing. To do it by hand instead:

```toml
[ui.sidebar.agents]
rows = [["state_icon", "workspace", "tab", "$acct"], ["agent"]]
```

The `stamp` action checks this at startup and posts one notification if the
token is unreferenced. It never edits the config on its own — that only happens
when you invoke `enable-badge`. It touches the sidebar row only — it does not
add, remove or switch accounts. That is the picker, under `open`.

#### Where to show it

The account is the same on every pane of a kind, so there are three places to
put it and they are not exclusive. All three read the same value.

| where | how | scope |
|-------|-----|-------|
| sidebar, per pane | `$acct` token, via `enable-badge` | one badge per agent row |
| herdr tab bar | `tab_bar_right` command entry | one badge for the whole session |
| Claude Code status line | ccstatusline `custom-command` widget | one badge per Claude pane |

#### One badge instead of one per pane

Credentials are machine-wide per kind, so every claude pane shows the same
account and the sidebar repeats itself down the column. To state it once, put it
in the tab bar instead and turn the pane token off:

```toml
[ui]
tab_bar_right = [
  { type = "command", command = "python3 ~/.local/state/herdr/plugins/c0r0nell.account-hub/current/switcher.py badge claude", interval_seconds = 30, timeout_seconds = 5 },
  { type = "text", text = "  " },
]
tab_bar_right_separator = ""
```

The last item on the tab bar sits flush against the right edge, which reads as
cramped. A whitespace-only `text` entry after the badge is the inset — herdr
keeps it. Blank `tab_bar_right_separator` when you add one, or the default ` · `
lands between the badge and the inset. Drop both lines if you want the badge
hard against the edge.

That path is a symlink the plugin repoints at itself on every run. Naming the
install directory instead would also work — herdr names it
`<plugin-id>-<hash>`, and the hash is `sha256(plugin_id)` truncated to twelve
characters, so it is stable across updates and moves only if the plugin id
changes. Checked on every plugin installed here, this one included.

The symlink is still the better path to write down. Where a plugin lives depends
on how it was installed — `herdr plugin install` puts it under
`~/.config/herdr/plugins/github/`, `herdr plugin link` leaves it wherever you
linked it — and that layout is herdr's business, not a documented interface. The
symlink is keyed by plugin id in the state directory, which herdr already treats
as ours. `herdr plugin list --plugin c0r0nell.account-hub --json` reports the
real directory if you want it.

The `badge` command prints one line and exits — herdr re-runs it on the
interval, so this needs no daemon and no `[[startup]]` hook. It reads the
credential store and the plugin's usage cache, then starts a background usage
refresh if one is due. Naming a kind
(`badge claude`) prints that kind alone; `badge` with no argument prints every
kind it can name, each prefixed with the agent's name:

```
Claude 👤 work 42% · Codex 👤 spare 18%
```

The percentage is usage consumed in the current five-hour session, even when
weekly or model-specific limits such as Fable are exhausted. It appears after
the account name when a cached session reading exists;
`~42%` marks an old reading or a reporting cooldown. While herdr polls the badge,
it refreshes the active account's usage every two minutes by default, respecting
provider cooldowns. Set `usage_ttl_s = 300` for five minutes, or use
`ACCOUNT_SWITCH_USAGE_TTL_S=300` in the command. New readings appear on the next
tab-bar poll (30 seconds in the example above). Background requests keep slow
responses from delaying the badge; failed reads retain the last percentage.
Without a reading, the badge shows only the account name until a refresh succeeds.
The picker, usage panel, and switch checks share the same cache.

The badge cannot vary per pane or per tab, and that is not a limitation of the
badge. A credential store is machine-wide per agent, so every claude pane bills
to the same account; a per-pane badge would print the same value on each one.
Printing both accounts once, on the tab bar, says everything there is to say.

Set `ACCOUNT_SWITCH_BADGE=0` in herdr's environment to stop stamping panes once
the tab bar shows it, or keep both — they read the same value.

#### In ccstatusline instead

[ccstatusline](https://github.com/sirmalloc/ccstatusline) drives the Claude Code
status line, and its **Custom Command** widget runs a shell command and shows
the output — the same one-line `badge` command, no herdr involved:

```json
{
  "type": "custom-command",
  "commandPath": "python3 ~/.local/state/herdr/plugins/c0r0nell.account-hub/current/switcher.py badge claude",
  "timeout": 2000
}
```

Add it through `ccstatusline`'s editor (Custom → Custom Command) rather than by
hand, so the widget gets an `id`. The widget also takes `maxWidth` to truncate
and `preserveColors` to keep ANSI colour from the command.

This works because `badge` needs nothing from herdr: it reads the credential
store, prints one line and exits. It runs the same from a herdr action, a bare
shell, or a Claude Code status line. Only the pane and workspace badges talk to
the herdr socket.

The trade is scope. The status line lives inside one Claude pane, so it names
the account for that pane's CLI — which is the same account everywhere, stated
once per pane rather than once per session. Pick the tab bar if you want it
stated exactly once.

#### Choosing the badge

`ACCOUNT_SWITCH_GLYPH` picks the character, and `ACCOUNT_SWITCH_BADGE_FORMAT`
decides what goes around it. Both fields are optional, so the format doubles as
a way to show less:

| format | shows |
|--------|-------|
| `{glyph} {name}` (default) | `👤 work` |
| `{glyph}{name}` | `👤work` — no space |
| `{glyph}` | `👤` — badge only, no account name |
| `{name}` | `work` — no glyph |
| `{agent} - {name}` | `Claude - work` — the agent's short name |
| `{title}: {name}` | `Claude Code: work` — its full name |

`{agent}` and `{title}` earn their place when the badge names two accounts at
once. A format that uses either one already says which agent it is, so the
automatic prefix steps aside. With `separator` set to three spaces:

```toml
badge_format = "{agent} - {name}"
separator = "   "
```

```
Claude - work   Codex - spare
```

A format naming a field that does not exist falls back to the default rather
than blanking the badge.

For a `tab_bar_right` entry, set them inline on the command — that env is yours
to write, unlike the one herdr hands to plugin actions:

```toml
command = "ACCOUNT_SWITCH_GLYPH=🔑 python3 ~/.local/state/herdr/plugins/c0r0nell.account-hub/current/switcher.py badge claude"
```

**2. Keybindings** — `herdr server reload-config` after editing these:

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
command = "python3 ~/.local/state/herdr/plugins/c0r0nell.account-hub/current/switcher.py switch claude work"
```

## Usage

What is left on every account you have saved, both agent kinds, in one panel:

```sh
herdr plugin action invoke usage --plugin c0r0nell.account-hub
```

```
Claude Code
  ● main          Session    16%  ██░░░░░░░░  limit in 2h10 (resets in 10m)
                  Weekly     65%  ███████░░░  limit in 1d12h (resets in 4d02h)
                  Fable       6%  █░░░░░░░░░  ~88% left (resets in 4d02h)
    spare         Session     0%  ░░░░░░░░░░  —
                  Weekly      2%  ░░░░░░░░░░  ~92% left (resets in 5d07h)
Codex
  ● main          Weekly     42%  ████░░░░░░  ~34% left (resets in 4d01h)
```

`●` is the account that is live. The point of listing the others is the case
above: `main` runs out before its session renews, and `spare` has room now.

`r` re-reads, `q` closes. `switcher.py usage` prints the same thing as text
(`--color` for ANSI, `--json` for a machine to read); neither prints a
credential.

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

`ACCOUNT_SWITCH_USAGE_RENEW=0` turns it off; parked accounts then show their
last-seen numbers with an age, and the live ones stay accurate.

A last-seen reading still projects. Each window records when it was read, and
the spend rate is worked out against that moment rather than against the clock
now — so an old reading brings its limit *closer* as it ages, instead of the
frozen percentage making a busy account look calmer the longer nobody looked.
The age beside the account is what says how much to trust it.

### Colours

| var | default | meaning |
|-----|---------|---------|
| `ACCOUNT_SWITCH_USAGE_THRESHOLDS` | `60,85` | percent at which a window turns warn, then crit |
| `ACCOUNT_SWITCH_USAGE_COLORS` | `ok=green,warn=yellow,crit=red,stale=blue` | the palette |
| `ACCOUNT_SWITCH_USAGE_BAR` | `█░` | filled and empty bar characters |
| `ACCOUNT_SWITCH_USAGE_BAR_WIDTH` | `10` | bar width in cells |

The overlay and the `--color` text form read the same palette, so they agree.

## Picker keys

```
j / k or ↓ / ↑   move selection
enter            switch to the selected account
s                save the current login as a new profile (prompts for a name)
a                capture the current CLI login into the selected profile (confirms)
r                rename the selected profile
x                delete the selected profile, including the one in use
d                show / hide every usage window, not just the short one
u                re-read usage
q / esc          close
```

Each account carries how much of it is left, so the picker answers "which one
should I switch to" on its own:

```
       account               used limit in
 ● main             █████░░░  66% 1h32   you@example.com · max
   spare            ░░░░░░░░   2% 4h47↺  you@work.example · team
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

While the shown window is filling, the figure counts down to when it fills —
`1h32` above is when work stops on `main`, not when its session refills two
hours from now. That is what the column is titled for.

Not every window has a limit still ahead of it, though. A **spent** one has
arrived at it, one too new has no rate to project from, one being spent slowly
enough never reaches it before it resets, and a cached reading can hold a limit
the clock has already gone past. `spare` above is the second of those: 2% spent,
and not yet running long enough to say what that means.

Those rows show the reset instead, and carry a `↺`. Without it a reset reads as
a limit that is not coming, which is what the column said for every row before.

Every window is still there — `d` swaps the column for all of them, each with its
own projection, one account per block:

```
       account       window   used              at this rate
Claude Code
 ● main                                  you@example.com · max
                    Session    11%  █░░░░░░░░░  ~77% left (resets in 2h37)
                    Weekly     66%  ███████░░░  limit in 1d13h (resets in 3d23h)
                    Fable       7%  █░░░░░░░░░  ~84% left (resets in 3d23h)

   spare                                 you@work.example · team
                    Session     0%  ░░░░░░░░░░  —
                    Weekly      2%  ░░░░░░░░░░  ~92% left (resets in 5d04h)
                    Fable       0%  ░░░░░░░░░░  —

Codex
 ● main                                  you@example.com · plus
                    Weekly     66%  ███████░░░  limit in 1d13h (resets in 3d23h)
```

The account line drops its own figure while expanded, because the windows below
already carry it. Both views carry column titles.

**Fable** is a per-model weekly limit. The API calls it `weekly_scoped` and puts
the model in a `scope` field, so the model's name is shown instead of the key.

**Codex shows only its weekly window.** It reports one worth watching; the rest
is noise next to claude's three.

**at this rate** is the column that decides whether to switch: where the window
lands if you keep spending it as you have been. `~70% left` finishes with room
to spare; `limit in 1d13h (resets in 4d00h)` runs out three days early — 66%
used is comfortable with a day to go and a problem with five. The reset comes
along in brackets, because a projection means nothing without knowing what it is
racing.

It is worked out from the window's length and how much of it has already passed,
so it says nothing until the window has run long enough to mean something: a
window minutes old would otherwise claim the limit is imminent. A window already
at 100% reads `spent`.

The picker and usage panel refresh at the cache interval (two minutes by
default), because the endpoints rate-limit. `u` in the picker or `r` in the
usage panel requests a refresh. A successful switch verification also saves
its usage reading, so opening the picker does not repeat that request.

If a read fails or ages past the cache lifetime, the picker keeps the bar and
countdown and marks the last known percentage with `~` (for example, `~100%`).
Press `d` to see the reading's age and refresh error. Refresh respects the
cache lifetime and the provider's `Retry-After` delay, falling back to five
minutes when no usable delay is supplied.

`s`, `a`, `r` and `x` each open a dialog in the middle of the screen. Deleting removes
this plugin's saved copy of a login and nothing else: the credential store the
CLI reads is never touched, so you stay logged in. Delete the profile for the
account you are on and the next switch away re-saves it under an `autosaved-…`
name.

`●` marks the selected UUID whose login is live. A section header reading
*"live: … (unsaved, press s)"* means the live login is not associated with a
selected record. Press `s` to register it, or `a` on an existing record to
explicitly replace that record's authentication.

## What it reads and writes

| kind | credential store | identity |
|------|------------------|----------|
| claude | Keychain item `Claude Code-credentials` (macOS), else `~/.claude/.credentials.json` | the `oauthAccount` block in `~/.claude.json` |
| codex | `~/.codex/auth.json` | `account_id` + the `id_token` claims inside it |

`CLAUDE_CONFIG_DIR` and `CODEX_HOME` are honoured if you set them. On claude,
**only** the `oauthAccount` key of `~/.claude.json` is touched — projects,
history and onboarding state are rewritten verbatim.

New profiles live in `HERDR_PLUGIN_STATE_DIR/profiles/<kind>/<uuid>.json`, mode
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

With no state-directory override, private data lives outside this checkout at
`~/.local/state/herdr/plugins/c0r0nell.account-hub/`. Its `profiles/`,
`backups/` and `rotated/` directories all contain credentials. The live Codex
login remains in `~/.codex/auth.json` (or `$CODEX_HOME/auth.json`). These files
are not source code and must not be published.

The repository's `.gitignore` also excludes local credential files, `.env`
files, and account-state directories copied into the checkout. Ignore rules do
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
4. writes a timestamped backup of it under `HERDR_PLUGIN_STATE_DIR/backups/`
   (last 10 kept);
5. reads the store back and compares the account identity, and **puts the
   previous login back** if the write didn't take.

An account already logged in but never saved shows up in the picker as
*unsaved*; press `s` before switching away if you want to name it yourself
rather than take the `autosaved-…` name.

## Config

Settings live in a file in the plugin's own config directory:

```sh
$(herdr plugin config-dir c0r0nell.account-hub)/config.toml
```

```toml
# The badge character on each agent pane and in the tab bar.
glyph = "🔑"

# What goes around it, from {glyph}, {name}, {agent} and {title}. Drop {name}
# for a badge with no account name.
badge_format = "{glyph}{name}"

# Percent at which a usage window turns amber, then red.
usage_thresholds = "50,75"

# Usage refresh interval in seconds, including the top-right badge.
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

The key is the variable below without its `ACCOUNT_SWITCH_` prefix, lowercased.
`config.json` works too if you prefer it. A file that fails to parse is ignored
rather than fatal, and the defaults stand.

An environment variable still wins over the file, but is rarely the practical
choice: plugin actions inherit the **herdr server's** environment, so setting
one means exporting it before herdr starts. The `tab_bar_right` and
ccstatusline entries are the exception — those take variables inline on the
command, which is why they are the easy place to restyle the badge.

| var | default | meaning |
|-----|---------|---------|
| `ACCOUNT_SWITCH_NOTIFY` | `1` | herdr notification on each switch, and on a refused one |
| `ACCOUNT_SWITCH_BADGE` | `1` | write the `$acct` pane token |
| `ACCOUNT_SWITCH_BADGE_ALWAYS` | `0` | badge a kind with no saved profile too |
| `ACCOUNT_SWITCH_GLYPH` | `👤` | the badge character |
| `ACCOUNT_SWITCH_BADGE_FORMAT` | `{glyph} {name}` | badge layout, from `{glyph}`, `{name}`, `{agent}`, `{title}`; any may be dropped |
| `ACCOUNT_SWITCH_SEPARATOR` | ` · ` | between kinds, when `badge` prints more than one |
| `ACCOUNT_SWITCH_VERIFY_SWITCH` | `1` | ask the provider whether a saved login still works before installing it |
| `ACCOUNT_SWITCH_USAGE_RENEW` | `1` | renew a parked account's token so its usage can be read |
| `ACCOUNT_SWITCH_USAGE_TTL_S` | `120` | how long a usage answer is reused |
| `ACCOUNT_SWITCH_SETTLED_PERCENT` | `5` | spend this much of a window and its rate counts, however new it is |
| `ACCOUNT_SWITCH_RENEW_MARGIN_S` | `300` | renew this far ahead of a token's expiry |
| `ACCOUNT_SWITCH_COMMAND_TIMEOUT_S` | `30` | how long any one command may take before it is abandoned |
| `ACCOUNT_SWITCH_LOCK_TIMEOUT_S` | `5` | how long to wait for another switch before refusing |
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

After an official CLI login, choose which record to update: select it in the
picker and press `a`, or run:

```sh
python3 switcher.py reauth codex <profile-uuid>
```

This captures the current login into only that UUID, preserves its name and
creation time, clears its old usage/staged tokens, and selects it. It does not
launch login or change the live credential store. `reauth` rejects labels and
requires an existing UUID. `save` always registers a new UUID instead.

A renewal counts as a change too: renewing a parked profile's tokens moves its
`saved_at`, so anything watching that field can tell the credential was
repaired. `autocontinue` remembers a refused login exactly that way.

A saved login can stop working while it sits on disk, and the snapshot gives no
sign of it: it still parses, still installs, and only fails when an agent uses
it. Four provider rules cause that, and they are why everything under "Not
losing a login" above works the way it does — refresh tokens are single-use and
rotate, a refusal counts only when the provider words it, an upgraded account
looks exactly like a revoked one, and Codex revocation is scoped to the
`installation_id` inside `CODEX_HOME` rather than to the account.

Written up once, with the refusal codes and how each was established:
[`docs/solutions/runtime-errors/saved-login-revoked-outside-the-plugin.md`](docs/solutions/runtime-errors/saved-login-revoked-outside-the-plugin.md).

### A refused switch says so

The picker prints a refusal on its own status line. Every other path only wrote
it to stderr, and herdr drops that. The failure then read as "the badge did not
change", with nothing saying why.

`next`, `switch`, `save` and `reauth` send the reason as a herdr notification. A
completed switch already sent one, so the two match. `ACCOUNT_SWITCH_NOTIFY=0`
turns both off. Nothing polled sends one: `badge` runs once a second from the
tab bar, and a notification per tick would be worse than the silence.

Where that notification appears is herdr's setting, not this plugin's.
`[ui.toast] delivery = "system"` hands it to macOS, which drops it when the
alert style is off. `delivery = "herdr"` draws it inside herdr instead, and
needs no permission from macOS.

## Fragility

Credential layouts are the agents' private business and can change without
notice. If a switch stops taking, `herdr plugin action invoke status --plugin
c0r0nell.account-hub` prints what each store currently reports; the store
paths and the shape read out of them are the two constants at the top of
`ClaudeBackend` / `CodexBackend` in `switcher.py`.

A Codex install that keeps its credentials in the macOS Keychain rather than
`~/.codex/auth.json` is not supported — the plugin reports codex as not logged
in rather than guessing.

## Requirements

- herdr ≥ 0.8.0 (for the `[[startup]]` badge refresh)
- Python 3 (stdlib only; uses `curses` for the picker)
- macOS or Linux

## The other herdr plugins

Each installs on its own; they share nothing but an author.

- [**herdr-idle-shell-badge**](https://github.com/rcosteira79/herdr-idle-shell-badge) — Badges idle agents that still have a background shell running, so one that *looks* done but left a process alive isn't mistaken for finished.
- [**herdr-readpending**](https://github.com/rcosteira79/herdr-readpending) — Mark agents you started reading but haven't finished — a numbered badge plus a reorderable overlay queue that clears when you focus the agent.
- [**herdr-autocontinue**](https://github.com/rcosteira79/herdr-autocontinue) — Watch agents for usage-limit walls, badge the countdown to the reset, and re-prompt the agents you armed once the window reopens.
