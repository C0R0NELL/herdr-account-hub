# Working in this repo

`switcher.py` is the whole plugin: standard library only, no compilation. Its
manifest's install preparation hook migrates legacy private storage before
Herdr removes the old checkout; it never signs in or calls provider APIs. Run the
checks with `python3 tests/check_switcher.py`. They use a temporary state
directory and a fake credential backend,
and never touch a real login.

Solved problems that cost real investigation live in `docs/solutions/`, stamped
with how each was established — relevant when changing anything about renewal,
switching, or the credential stores.

## Versioning and releases

Account Hub is intended for public distribution through Herdr. Treat published
versions as user-facing releases.

### Commit messages

- Write every commit subject and body in English using
  [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/).
  Use `<type>(<scope>): <summary>`; the scope is optional. Choose the type and
  scope from the actual diff: `feat`, `fix`, `docs`, `style`, `refactor`, `perf`,
  `test`, `build`, `ci`, `chore`, or `revert`. Mark breaking changes with `!`.
- Describe the resulting behavior in the subject, rather than using only a
  version number. Follow it with a blank line and concrete bullet notes. For
  example, `fix(picker): keep accounts visible during lock contention`.
- For a release commit, write the notes once in `CHANGELOG.md`. The release
  helper uses that exact version section as the commit body and GitHub Release
  description. Keep the notes in English; never include private account data.
  Cover the complete release diff since the preceding published tag, including
  changes already committed. The helper extracts notes; it does not summarize
  commit history or verify that every change is documented.

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

### Standard release workflow

Use `scripts/release.py` for release commits and GitHub Releases. The script
does not infer approval, choose a version, edit the changelog, or stage files.
The agent must still review the actual diff, propose the correct version, and
obtain the user's agreement under the rules above. Human review also checks
the accuracy and English wording of the subject and notes; format validation
cannot establish either of those.

This is a manually invoked workflow, not a GitHub Actions workflow. A commit or
pull request does not create a tag or GitHub Release. Publication requires the
separate, explicitly authorized `publish` command. The release helper requires
Python 3.11 or newer; publishing also requires GitHub write permission.

1. Update the official manifest and the first `CHANGELOG.md` entry to the
   approved version. Changelog headings must be `## X.Y.Z - YYYY-MM-DD`, with
   unique versions and nonempty bullet notes. Keep older entries below it.
2. Run `python3 scripts/release.py --subject '<type>(<scope>): <summary>'` to
   preview the selected version, tag, subject, and exact notes. It has no
   network calls or writes. Review all changes and stage only intended files.
3. Run `python3 scripts/release.py commit --version X.Y.Z --subject
   '<type>(<scope>): <summary>'`. Both arguments are required. It checks the
   staged manifest/changelog, Git exclusions, obvious credential patterns,
   version progression, and both test commands before committing on `main`.
   Unstaged/untracked files and changes during checks abort the operation.
4. Once publication is authorized, run
   `python3 scripts/release.py publish --version X.Y.Z` on the clean committed
   tree. It reruns both checks, pushes the exact commit and its tag without
   force, and creates the GitHub Release using those committed notes. Git and
   the authenticated GitHub CLI (`gh`) are required. Conflicting tags or
   existing release metadata are never replaced. Retry the same command after
   a GitHub failure; an already matching published release is a no-op.

Documentation/test-only maintenance can use a normal Conventional Commit
without a new changelog version or GitHub Release, after the mandatory version
review. Never amend published history just to apply the new message convention.

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
- Run `python3 tests/check_switcher.py` before releasing. Keep credentials, `.local/`,
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
