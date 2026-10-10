---
module: account-hub
date: 2026-10-10
problem_type: runtime_error
component: usage
tags: [claude, usage, rate-limit, cache]
---

# Evidence

A fake HTTP fixture reproduces a two-hour-old 0% usage reading followed by
HTTP 429. Account Hub retains the last known percentage, marks it stale, and
records the reporting error and retry deadline. Separate fixtures establish
numeric and HTTP-date `Retry-After` handling. These are local regressions, not
measurements of a real Claude account or proof of a provider's rate-limit scope.

# Local defects and changes

- The switch verifier fetched usage and discarded it. Successful verification
  now saves the reading, and a 429 records its cooldown for usage readers.
- The picker refreshes while open, respecting each account's backoff. Manual
  refresh covers the whole list rather than only the selected account.
- Every 429 previously imposed a fixed five-minute wait. The switcher now
  honors numeric and HTTP-date `Retry-After` values, with five minutes as the
  fallback for missing or unusable values.
- Old percentages were displayed without age or error in the picker. The
  collapsed view keeps the bar and countdown with a `~` before the last known
  percentage; expanded usage shows the age and refresh error.

Regression checks in `tests/check_switcher.py` use fake HTTP and credential stores.
They cover saving and reusing verification reads, sharing verification backoff,
retry delays, stale-zero presentation, and aging a reading while the picker
remains open. These changes improve recovery and reduce duplicate reads; they
cannot guarantee that Anthropic's reporting endpoint will accept a request.

# Independent collection and concurrent readers

Account Hub uses the authenticated provider endpoint and its own UUID-keyed
cache. It does not import readings from other plugins or status-line caches.

Code inspection found that concurrent picker/usage processes could both read
an expired cache before either recorded its HTTP result, causing duplicate
requests. Usage collection now takes the existing re-entrant process lock
before reading the cache, fetching, and recording a result or cooldown. A
waiting reader therefore sees the preceding reader's update. The picker reloads
the plugin's own cache on UI ticks to observe reads from other actions.

Regression tests start two processes simultaneously with fake HTTP and verify
exactly one request per account, both for successful reads and HTTP 429s.
This proves request deduplication within the switcher, not why Anthropic
throttled an individual account. Other clients can still query that endpoint,
and provider throttling remains possible.

# Picker visibility and account association (2026-10-09)

Read-only inspection found five Codex registrations on disk, while the user
sometimes saw three in the picker. A small fake curses screen reproduced the
cause: rendering stopped at the pane bottom without scrolling. Expanded usage
made the problem worse. The picker now counts registrations and scrolls to keep
the selected account visible. Selection follows `(kind, UUID)`, rather than its
position in a changing list. Legacy records no longer move when renewal updates
`saved_at`.

A fake endpoint reproduced stale reads after explicit refresh: recent cache
entries prevented any request. Explicit picker refresh now bypasses the normal
TTL, preserving provider cooldowns. The picker's refresh action is **r**.
New registrations are detected on UI ticks and their readings are collected
without waiting for the old list's timer.
Errors persist in each UUID's cache entry until successful collection, so a
later cache-only tick does not remove a failed-read warning.

The saved-registration identity and the selected authentication grant serve
different purposes. A fresh login for a person/workspace already registered
was incorrectly labeled unsaved and usage was read with the older saved token.
The picker now tests registration presence independently. Usage may read the
matching live person/workspace's token without selecting or overwriting any
UUID. Another person in the same workspace cannot supply that account's usage.
The save prompt appears only for an absent identity and reads
`(press 's' to save this account)`.

`tests/test_usage_picker.py`, included by `python3 -B test_switcher.py`, reproduces
clipped accounts, expanded-row scrolling, selection drift, stale explicit
refresh, new registrations, fresh live authentication, and persistent errors.
It also checks five distinct users with identical labels in one workspace,
duplicate registrations, and cooldown preservation. All data, credentials,
and HTTP responses are fictitious. No real authentication, token renewal,
provider usage request, or daemon restart was performed in this investigation.

# Fresh readings on picker entry (2026-10-10)

A fake-curses regression reproduced opening the picker with a recent usage
cache written before reset counts existed: it showed the old percentage and
`-` for resets without querying the provider. Reopening with a recent complete
cache also kept the old usage and reset count. Both cases were caused by the
initial `usage_rows()` honoring the normal TTL.

The initial read now bypasses the TTL, like `r`, while preserving each UUID's
provider cooldown. Subsequent idle ticks still reuse cached readings until
the normal interval or a scheduled post-reset check. Fake HTTP regressions in
`tests/test_usage_picker.py` verify fresh percentages/counts on the first frame,
reopening, no repeated request on the next idle tick, and refreshing the other
four accounts while one is in cooldown. No real credentials or resets were used.
