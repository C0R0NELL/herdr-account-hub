---
module: account-hub
date: 2026-10-10
problem_type: runtime_error
component: picker
tags: [curses, resize, footer, redraw]
---

# Evidence

The supplied screenshots show a duplicated/truncated support link and account
rows with remnants of another layout. Reopening restores the display. These
images show rendering corruption; they do not establish an incorrect provider
usage response or damaged registrations.

Two native ncurses regressions reproduced a concrete cause with fictitious
accounts in an isolated PTY. A resize from 18x140 to 14x80 during a blocking
operation left the footer layout using the earlier dimensions. Its `refresh()`
processed SIGWINCH and updated ncurses, but the following raw OSC 8 paint still
sent a cursor position of row 18, column 132. That position exceeded the new
PTY boundaries and could clamp to the last cell, wrap, and scroll the display
without updating ncurses' physical-screen cache. The rebuilt picker's logical
last row was also blank instead of containing the footer. Both tests failed
before the fix and passed afterward.

# Changes

- Synchronize ncurses with the PTY size before list, footer, dialog, and switch
  progress layout. A detected resize invalidates the physical-screen cache.
- Independently check the current PTY bounds immediately before raw hyperlink
  painting. An outdated target is skipped rather than wrapping at the edge.
- Clear and repaint after modal restoration and official sign-in. Invalidate
  the screen on switch completion/failure and explicit refresh, so unchanged
  logical rows are also retransmitted when the physical display has changed.

The existing OSC 8 URL, pink color, account UUID association, credential stores,
and provider usage cache are unchanged by this rendering fix.

# Verification and limits

`tests/test_picker_terminal.py` runs the real ncurses implementation with fake
credential and HTTP backends. In addition to the two regressions above, it
checks that outdated hyperlink coordinates produce no text, and that returning
from a switch retransmits all five account rows after an out-of-band screen
clear. The root `python3 test_switcher.py` includes these native terminal checks.
No actual login, switch, provider request, or reset is performed.

The [Python curses documentation](https://docs.python.org/3/library/curses.html)
documents the physical/virtual screen distinction, `resizeterm()`, and
`clearok()`. Validation here covers the PTY/ncurses boundary, not a physical
mouse or resize gesture in the user's attached Herdr client.
