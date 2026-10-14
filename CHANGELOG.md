# Changelog

## 0.4.0 - 2026-10-14

### Added

- Show the nearest available Codex reset expiry beside the reset count in
  `YYYY/MM/DD HH:MM UTC` format, using the existing per-account usage refresh
  and cache. Refresh the date after reset consumption and switch verification.
- Fetch expiry details only for positive reset counts when complete details are
  absent from the usage response. Respect detail-request cooldowns during manual
  refresh and preserve usage/count readings when expiry details cannot be read.

### Changed

- Keep reset count, expiry, and account email columns aligned with matching
  four-space gaps. Center unavailable `-` placeholders within their columns.
- Show `-` for expiry when resets are unavailable or zero, or when expiry
  details are missing, malformed, incomplete, undated, or already expired.

### Maintenance

- Share reset availability and expiry filtering between the picker and the
  redemption confirmation, preserving the existing earliest-expiry selection.
- Add isolated regressions for account-specific expiry caching, UTC formatting,
  provider failures/cooldowns, post-reset refresh, and picker column alignment.
- Update the usage guide and reset troubleshooting notes.

## 0.3.3 - 2026-10-11

### Fixed

- Show recognized OpenAI error codes in Codex switch refusals and cached login
  problems instead of grouped descriptions, preserving the existing sign-in
  guidance and protection of the active account.
- Fall back to the HTTP status when no recognized provider code is available,
  without displaying raw error messages or response bodies.

### Maintenance

- Cover provider error codes, HTTP fallback, and cached refusal display with
  isolated regression tests, and update the authentication troubleshooting notes.
- Include the previously committed LICENSE copyright attribution to Yuri Gomes.

## 0.3.2 - 2026-10-10

### Maintenance

- Use the official Account Hub manifest and identity for local editing, removing
  the separate generated manifest and linking helper. The official plugin ID,
  display name, durable account storage, and managed update flow remain stable.
- Extend isolated Herdr integration checks to cover local linking, unlinking,
  and returning to a managed installation while preserving account records.
- Exclude additional local manifests from Git and reject them in release checks.
- Align contributor, storage, and publishing documentation with the single
  official manifest workflow and record the verified local storage transition.
- Include the repository's existing Dependabot configuration template.

## 0.3.1 - 2026-10-10

### Fixed

- Keep the picker open when another account operation holds the shared lock.
  Preserve the last usage reading, show a waiting notice, and retry refresh
  after the lock becomes available. Escape remains available while waiting.
- Add a blank line between the Codex and Claude Code sections, including when
  no Codex accounts have been saved.

### Maintenance

- Add a single-command release pipeline that validates the requested version
  against the official manifest and changelog, runs checks, commits with English
  Conventional Commits, pushes the exact commit/tag, and publishes the matching
  changelog notes as the GitHub Release body.
- Preserve the release commit when publication fails so repeating the same
  command resumes without another commit or replacing an existing tag.
- Consolidate the check entrypoint under `tests/` and cover release operations
  with temporary Git repositories and a fake GitHub CLI.

## 0.3.0 - 2026-10-10

### Added

- Click any account-row column to open its switch confirmation. The complete
  Codex reset column opens the reset confirmation instead.
- Click Enter/Esc in confirmation, text-input, and provider-selection modals.
  Provider options can also be selected by mouse. Keyboard navigation remains
  available, and clicks outside action labels do not confirm or cancel dialogs.
- Pink terminal hyperlink to GitHub Sponsors in the picker footer.
- Durable storage keyed to the stable plugin ID.
  Installation and first launch migrate legacy private files before source
  checkout cleanup, preserving registrations, credentials, settings, selection,
  caches, backups, and pending reset operations.

### Fixed

- Recognize the current login and prevent duplicate registrations by normalized
  email within each provider. UUIDs still address stored accounts, and credential
  synchronization requires an explicitly selected, matching authentication grant.
- Recover automatically from temporary usage-read failures with bounded retries,
  while respecting provider cooldowns and excluding authentication/renewal failures.
- Repaint the picker after resizes, confirmations, sign-in, and switching so
  stale terminal coordinates do not corrupt account rows or the footer link.
- Keep the current-account marker visible alongside an account error marker.

### Compatibility

- Default private storage moved from checkout-local `.local/` to the plugin's
  Herdr state/config directories. Legacy paths become compatibility links.
  Overrides must also stay outside source checkouts. Conflicting data or a legacy
  move across filesystems aborts preparation instead of replacing credentials.
- Mouse and keyboard account switching both require confirmation for Claude too.

### Maintenance

- Rename the source repository to `C0R0NELL/herdr-account-hub` while retaining the
  official manifest ID.
- Shorten the README, add a usage guide and publishing notes, and organize checks
  in `tests/`, including native-terminal and managed-upgrade regressions.
