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

## Work remaining before public distribution

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
- Once the release is ready and publication is authorized, make the repository
  public and add the `herdr-plugin` topic. Renaming the repository by itself
  does not publish it or put it in the marketplace.

The current [Herdr plugin documentation](https://herdr.dev/docs/plugins/)
describes `herdr plugin update` for managed installs. Verify that the installed
Herdr binary supports it; older binaries can reinstall from the same GitHub
source instead. The [Plugin Manager](https://github.com/speardragon/herdr-plugin-manager)
compares the installed commit to the requested upstream ref and displays the
remote manifest version when available. Users apply updates explicitly;
locally linked development checkouts are excluded from those update checks.
