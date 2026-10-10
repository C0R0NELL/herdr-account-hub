# Publishing Account Hub

The source repository is [C0R0NELL/herdr-account-hub](https://github.com/C0R0NELL/herdr-account-hub).
Install it with `herdr plugin install C0R0NELL/herdr-account-hub`. The repository
name does not change the official ID `c0r0nell.account-hub`, the display name
**Account Hub**, or the development ID `c0r0nell.dev-account-hub`.

## Marketplace requirements

Herdr automatically discovers public GitHub repositories with the topic
`herdr-plugin` and a parseable `herdr-plugin.toml` on the default branch. Forks
and archived repositories are excluded. The index refreshes every 30 minutes;
it is discovery, not approval or a security review. A `herdr-` name prefix is
not a discovery requirement.

The manifest requires `id`, `name`, `version`, and `min_herdr_version`. Declare
supported platforms and document external dependencies. This plugin uses Python
standard-library code without compilation; its install preparation hook protects
legacy storage before checkout cleanup. The relevant agent CLI is required
for its account features. Preserve the license and attribution.

Sources: [Marketplace](https://herdr.dev/docs/marketplace/) and
[Plugins](https://herdr.dev/docs/plugins/).

## Release preparation

- Retest durable storage for each release that changes installation or storage.
  Official and development IDs use separate state/config directories outside
  checkouts. The install hook imports legacy `.local/` before old code is removed.
  `python3 tests/check_managed_upgrade.py` validates fictitious accounts through
  the real Herdr installer; conflicts fail without replacing credentials.
  See the [migration evidence](solutions/runtime-errors/durable-plugin-storage.md)
  and [official storage guidance](https://herdr.dev/docs/plugins/#commands-and-environment).
- Prepare the actual release diff under [AGENTS.md](../AGENTS.md): propose and
  agree on a version, update the manifest and `CHANGELOG.md`, run the full
  checks, review Git exclusions, and then commit and push. The development
  manifest and private data must remain excluded.
- Keep the repository public and its `herdr-plugin` topic present for marketplace
  discovery. Renaming the repository by itself does not publish a release.

## Scripted releases

Write the notes once in the first `CHANGELOG.md` version section, matching
the official manifest. The helper takes only that section, rejects malformed,
empty, or duplicate entries, and reuses the exact notes in the commit body and
GitHub Release. Subjects follow Conventional Commits and are written in English.
The agent reviews the actual diff and obtains agreement on the version first,
as required by [AGENTS.md](../AGENTS.md). The script never chooses a bump.
Review all changes since the preceding published tag when writing notes,
including already committed changes. The helper does not generate summaries
from commit messages.

The workflow runs locally with Python 3.11 or newer. There is no GitHub Actions
release workflow: committing or opening a PR does not publish anything. Only
the separate `publish` command pushes the release commit/tag and creates the
GitHub Release, using an account with repository write permission.

From the checkout root, preview without writes or network requests:

```sh
python3 scripts/release.py --subject 'fix(picker): keep accounts visible during lock contention'
```

After staging the intended files, create the release commit with the approved
version and a subject appropriate to its diff. Then publish when authorized:

```sh
python3 scripts/release.py commit --version X.Y.Z --subject 'fix(picker): describe the actual fix'
python3 scripts/release.py publish --version X.Y.Z
```

Replace `X.Y.Z` with the agreed version. Commit runs the unit/terminal/release
checks and the managed-install integration check. Publish repeats those checks
on a clean committed tree, pushes that exact commit and its `vX.Y.Z` tag
atomically without force, then creates the GitHub Release. It uses the configured
GitHub `origin` and authenticated `gh`; ordinary plugin installation needs neither
this script nor a GitHub login. Existing conflicting tags/releases are rejected.
After a GitHub API failure, retry the same publish command: the commit and tag
are preserved, and a matching already published release is not duplicated.

The GitHub Release title is generated from the official name and version. GitHub
receives committed notes through a temporary file and `gh release create
--verify-tag --notes-file`; no separate text needs to be authored. See the
[GitHub CLI](https://cli.github.com/manual/gh_release_create) and
[GitHub Release API](https://docs.github.com/en/rest/releases/releases#create-a-release).
Documentation/test-only maintenance uses an ordinary Conventional Commit and
does not require another plugin version or GitHub Release.

The current [Herdr plugin documentation](https://herdr.dev/docs/plugins/)
describes `herdr plugin update` for managed installs. Verify that the installed
Herdr binary supports it; older binaries can reinstall from the same GitHub
source instead. The [Plugin Manager](https://github.com/speardragon/herdr-plugin-manager)
compares the installed commit to the requested upstream ref and displays the
remote manifest version when available. Users apply updates explicitly;
locally linked development checkouts are excluded from those update checks.
