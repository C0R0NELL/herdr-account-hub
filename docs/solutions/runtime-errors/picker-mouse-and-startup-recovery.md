---
module: account-hub
date: 2026-10-10
problem_type: runtime_error
component: picker
tags: [usage, startup, mouse, confirmation, cache]
---

# Evidence and scope

The user reported error markers on picker entry that cleared after manual
refresh. Read-only inspection of the development usage cache after refresh
found no retained reporting or saved-login errors, so it did not establish
which actual provider error caused the initial markers.

A deterministic fake HTTP/curses fixture reproduced one contributing defect:
entry forces a read, but a temporary failure with a recent successful cache
remained flagged until manual refresh or the normal TTL. Restoring connectivity
alone did not clear it on subsequent UI ticks. The regression failed before
the fix and passed afterward. This establishes recovery behavior, not a
provider-side initialization bottleneck.

# Recovery

Usage GET failures caused by connection errors, timeouts, HTTP 408, or HTTP 5xx
schedule account-scoped retries after 5, 10, and 20 seconds. After those retries,
the next attempt waits the normal TTL. No sleeps block the initial screen.
Cached readings and warnings remain visible until a successful read. Successful
collection clears the reporting error and pending retries.

The schedule is persisted by provider and registration UUID under the existing
process lock. Cache-only ticks do not fetch or advance it. Other readers observe
the same deadlines, including when no successful reading exists yet. HTTP 429
clears short retries and retains its provider cooldown. Authentication errors,
saved-grant refusals, and failures inside token renewal are excluded from this
short usage-retry path. The existing normal renewal path remains unchanged.

# Mouse actions

The picker requests native curses left-click events and restores the previous
mouse mask on exit. Every visible account-row cell opens switch confirmation,
except the complete Codex reset column, including its padding. Its reset action
has precedence over the rest of the row. Hit testing accounts for scrolling and
expanded usage rows. Headers, expanded detail-only rows, the footer, release
events, and modified clicks have no account action.

A hit stores the provider and UUID, redraws the selected row, then dispatches
the existing Enter or reset action. Both Codex and Claude switching require
confirmation; reset consumption retains its expiry selection and idempotency
flow. Removing the clicked registration before dispatch cancels the action
rather than targeting another row. No mouse support falls back to the keyboard.

All picker modals share underlined, clickable Enter/Esc labels. Global mouse
coordinates are translated to each centered window before hit testing. Clicking
an option in the provider chooser selects it; Enter still confirms that selection.
Clicks outside action labels, on separators, releases, or modified clicks do not
confirm or dismiss dialogs. Confirmation clicks go through the existing keyboard
action path, preserving the same switch/delete/reset safeguards.

# Verification

`tests/test_usage_picker.py` covers automatic temporary-error recovery, bounded
retries, cold-start deadlines, cooldowns, exclusion of renewal/auth failures,
correct clicked UUID, cancellation, scrolling, expanded rows, Claude
confirmation, deleted registrations, zero resets, complete account/reset columns,
all three modal types, and irrelevant modal clicks. Its credentials and
provider responses are fictitious.

An isolated PTY check also ran the real ncurses implementation at 18 by 140
cells with the same fictitious backend. SGR press/release packets on Bravo's
name and reset count each selected Bravo and opened the expected dialog. Enter
produced exactly one fixture switch or reset consumption; both processes exited
cleanly. This checks terminal decoding, not a physical click in the user's
Herdr client. No real credentials, provider requests, or resets were used.

`tests/test_picker_terminal.py` also exercises native SGR terminal mouse packets
for centered Enter/Esc controls (including a click outside the modal), and a
usage-column click followed by a modal Enter click. The parent sends each packet
after the corresponding screen is drawn, matching separate user gestures instead
of allowing ncurses to coalesce queued clicks before the first dialog appears.
