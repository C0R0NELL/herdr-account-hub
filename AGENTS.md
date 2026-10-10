# Working in this repo

`switcher.py` is the whole plugin: standard library only, no compilation. Its
manifest's install preparation hook migrates legacy private storage before
Herdr removes the old checkout; it never signs in or calls provider APIs. Run the
checks with `python3 test_switcher.py` — this root entrypoint runs the checks in
`tests/`. They use a temporary state directory and a fake credential backend,
and never touch a real login.

Solved problems that cost real investigation live in `docs/solutions/`, stamped
with how each was established — relevant when changing anything about renewal,
switching, or the credential stores.

## Versioning and releases

Account Hub is intended for public distribution through Herdr. Treat published
versions as user-facing releases.

### Mandatory before every commit to `main`

1. Read the current `version` from `herdr-plugin.toml`. Review `git diff`,
   `git diff --cached`, and any untracked files intended for the commit. Base
   the decision on the actual changes being committed, not the commit title or
   a summary from an earlier turn.
2. Suggest a specific version to the user in `MAJOR.MINOR.PATCH` (`0.0.0`)
   format before committing. State the current version, the proposed version,
   and the reason, citing the relevant behavior or compatibility changes in
   the diff. Apply the conventions below and use the highest required bump
   when the commit contains multiple kinds of changes.
3. Wait for agreement on the proposed version before committing, unless the
   user has already explicitly selected or approved that version for these
   changes. General permission to commit is not approval of an unstated version.
4. Apply the agreed version to the official manifest and keep its changelog
   entry consistent. Review the final staged diff and run the required checks
   before committing. Never silently choose a version or skip this review.

For documentation-only or test-organization changes with no plugin behavior
or compatibility change, explicitly recommend keeping the current version and
explain why. Reviewing versioning is mandatory even when no bump is needed.

### Release conventions

- Bump `version` in the official `herdr-plugin.toml` for each release that changes
  plugin behavior. Use semantic versioning: patch for fixes, minor for compatible
  features, and major for breaking changes to a stable public contract. Before
  1.0, breaking changes bump the minor version and must be documented explicitly.
  Group related development edits into a release; documentation-only changes do
  not require a version bump.
- Record user-facing changes in `CHANGELOG.md` when preparing a release. Its
  version, the official manifest, and any `vX.Y.Z` release tag must agree.
- Keep the official ID/name stable. Generate the development ID/name through
  `scripts/link-dev.py`; never commit the generated development manifest.
- Run `python3 test_switcher.py` before releasing. Keep credentials, `.local/`,
  and other private state excluded from Git.
- Verify managed-install upgrades with temporary, fictitious accounts before
  public release. Preserve registration UUIDs, credentials, selection, settings,
  and pending reset operations. State and configuration must remain outside all
  source checkouts, scoped to the stable official/development plugin ID. Preserve
  the install preparation hook that imports an older checkout's `.local/`
  before cleanup. Git ignore rules do not preserve files during installation.
  Run `python3 tests/check_managed_upgrade.py` with an installed Herdr for the
  isolated, fictitious install/upgrade/uninstall/reinstall integration check.

Update availability is based on the upstream commit for the installed Git ref,
not only the manifest version. The Plugin Manager reads the remote manifest to
display the target version when available. Users explicitly apply updates;
locally linked development checkouts are excluded from update checks. Publishing
a commit or tag does not automatically update an installed plugin.

Recheck distribution behavior against the [Herdr plugin documentation](https://herdr.dev/docs/plugins/),
[marketplace documentation](https://herdr.dev/docs/marketplace/), and
[Plugin Manager documentation](https://github.com/speardragon/herdr-plugin-manager)
when changing installation, releases, or state persistence. Repository names and
checkout paths may change without changing the stable manifest ID. Review the
distribution notes in [docs/publishing.md](docs/publishing.md) before publication.
