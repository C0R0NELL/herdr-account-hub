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
installer removes a checkout. Development and official installs both need a
durable store keyed to their stable plugin ID.

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
  Hub's official or development ID. Otherwise derive portable XDG/Home defaults.
  Explicit Account Hub overrides remain supported outside source checkouts.
- Keep development and production isolated by plugin ID, independent of source
  directory names. A generated development manifest also identifies direct CLI
  execution from that checkout.
- Run `storage-migrate` as the manifest's install preparation hook and initialize
  storage before runtime actions. The hook finds only the same plugin ID's older
  registered checkout. It never reads the live credential backend or contacts a
  provider. Installation does not automatically register a live login.
- Move all legacy private files together while holding migration and original
  switch locks. A same-filesystem rename preserves the lock inode and existing
  credentials. A compatibility link lets older pickers continue using that store.
- Copy configuration atomically to the durable config directory, preserving its
  original private backup. Conflicts and unexpected links stop without replacing
  another copy. The known legacy `.local/current -> checkout` development pointer
  is retained without traversing the source tree.
- Secure private directories/files as `0700`/`0600`. Keep the compatibility link
  and generated manifest excluded from Git.

# Verification and limits

`tests/test_storage.py` covers temporary XDG and injected host paths, dev/prod
isolation, all private record types, original UUIDs, checkout removal, idempotent
migration, original lock inode, permissions, conflicts, interrupted config writes,
bounded lock waits, explicit test isolation, and unsafe paths/links.

`python3 tests/check_managed_upgrade.py` passed against the installed Herdr 0.9.3
using a local Git URL redirect, isolated configuration, and fictitious accounts.
It exercises a legacy managed install, migration through reinstall, another
upstream commit, uninstall, and reinstall. A conflicting durable store also
rejected the update and retained the old managed checkout and both data copies.
Every credential, registration UUID,
selection, setting, cache, rotated-token sidecar, backup, and pending reset record
remained unchanged. No server or real agent login was used.

The real development migration preserved five Codex registration UUIDs and all
32 existing private files byte for byte, including the reset journal and backups.
The live Codex authentication file and the original shared lock inode were
unchanged. No authentication, renewal, provider read, or reset was performed.

Cross-filesystem legacy moves fail safely instead of splitting locks still used
by older pickers. Close those pickers and migrate outside the checkout before
retrying. Concurrent operations can also make preparation time out; retry after
they finish. These failures leave account data available and abort installation.
Deleting durable state explicitly is still destructive; persistence protects
checkout replacement, not user deletion of private directories.
