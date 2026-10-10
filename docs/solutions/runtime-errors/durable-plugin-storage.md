---
module: account-hub
date: 2026-10-10
problem_type: integration
component: private-storage
tags: [herdr, installation, upgrades, uuid, credentials, persistence]
---

# Evidence

The old default placed accounts inside the source checkout's `.local/`. Git
exclusions prevent committing those files, but do not preserve them when an
installer removes a checkout. Local links and managed installs both need a
durable store keyed to the stable official plugin ID.

The [Herdr plugin documentation](https://herdr.dev/docs/plugins/#commands-and-environment)
provides separate state and configuration directories for each plugin.
The official [installer](https://github.com/herdrdev/herdr/blob/master/src/cli/plugin.rs)
runs the incoming manifest's preparation commands before activating the new
checkout and cleaning up the old installation. The old registry entry remains
available during that hook. The [directory implementation](https://github.com/herdrdev/herdr/blob/master/src/plugin_paths.rs)
places state/config outside managed checkouts, and the
[configuration implementation](https://github.com/herdrdev/herdr/blob/master/src/config/io.rs)
respects XDG directories on Linux and macOS.

# Changes

- Use injected state/config paths only when the host plugin ID matches Account
  Hub's official ID. Otherwise derive portable XDG/Home defaults.
  Explicit Account Hub overrides remain supported outside source checkouts.
- Keep storage independent of source directory names. Direct CLI execution,
  local links, and managed installs use the official manifest identity.
- Run `storage-migrate` as the manifest's install preparation hook and initialize
  storage before runtime actions. The hook finds only the same plugin ID's older
  registered checkout. It never reads the live credential backend or contacts a
  provider. Installation does not automatically register a live login.
- Move all legacy private files together while holding migration and original
  switch locks. A same-filesystem rename preserves the lock inode and existing
  credentials. A compatibility link lets older pickers continue using that store.
- Copy configuration atomically to the durable config directory, preserving its
  original private backup. Conflicts and unexpected links stop without replacing
  another copy. The known legacy `.local/current -> checkout` pointer
  is retained without traversing the source tree.
- Secure private directories/files as `0700`/`0600`. Keep the compatibility link
  and additional local manifests excluded from Git.

# Verification and limits

`tests/test_storage.py` covers temporary XDG and injected host paths, a shared
official identity across checkouts, all private record types, original UUIDs,
checkout removal, idempotent migration, original lock inode, permissions,
conflicts, interrupted config writes,
bounded lock waits, explicit test isolation, and unsafe paths/links.

`python3 tests/check_managed_upgrade.py` passed against the installed Herdr 0.9.3
using a local Git URL redirect, isolated configuration, and fictitious accounts.
It exercises a legacy managed install, migration through reinstall, another
upstream commit, uninstall, reinstall, and switching between a managed install
and an editable local link. A conflicting durable store also
rejected the update and retained the old managed checkout and both data copies.
Every credential, registration UUID,
selection, setting, cache, rotated-token sidecar, backup, and pending reset record
remained unchanged. Only a disposable named server is started for the local
unlink API; it is stopped before cleanup. No real agent login or provider API
is used.

The real local-storage migration preserved five Codex registration UUIDs and all
32 existing private files byte for byte, including the reset journal and backups.
The live Codex authentication file and the original shared lock inode were
unchanged. No authentication, renewal, provider read, or reset was performed.

On 2026-10-10, local editing was consolidated on the official manifest and ID.
The selected five registrations, their credentials, selection, cache, and reset
records were verified byte for byte after the transition. Duplicate registrations
from the replaced installation were excluded from the active store. Private
recovery copies remain outside all source checkouts. The live Codex login was
unchanged, and Herdr configuration validation and reload both passed.

Cross-filesystem legacy moves fail safely instead of splitting locks still used
by older pickers. Close those pickers and migrate outside the checkout before
retrying. Concurrent operations can also make preparation time out; retry after
they finish. These failures leave account data available and abort installation.
Deleting durable state explicitly is still destructive; persistence protects
checkout replacement, not user deletion of private directories.
