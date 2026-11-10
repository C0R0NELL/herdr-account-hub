---
module: account-hub
date: 2026-11-10
problem_type: runtime_error
component: codex-sign-in
tags: [codex, executable, path, herdr, sign-in]
---

# Evidence

The affected machine could run Codex CLI 0.167.0 from both its interactive shell
and a Python subprocess. Inspecting only the running picker's executable setting
and executable lookup showed `codex` with no match in that process's `PATH`.
The shell's working installation was in its user's `.npm-global/bin` directory.
The picker therefore failed before starting sign-in. Account cache state and
the Codex daemon's authentication were not involved at this boundary.

`_run_official_login` and `login_codex` were unchanged between 0.5.0 and 0.5.1.
The old catch-all `OSError` message hid the distinction between missing paths,
permissions, and executable format failures. The documented settings reader
also supported per-plugin configuration while `CODEX` bypassed that reader and
consulted only the environment.

# Changes

- Prefer the explicit `ACCOUNT_HUB_CODEX_BIN` / `codex_bin` setting, then the
  inherited `PATH`. If that lookup is missing, search a bounded set of common
  current-user and system install directories. HOME, XDG data locations, and
  inherited `NVM_BIN` / `PNPM_HOME` are portable inputs; no username is embedded.
- Pin the found executable path, append launcher and fallback directories only
  to Codex subprocess environments, and preserve existing runtime preference.
  This also supports npm/version-manager launchers whose sibling runtime is
  absent from the original picker `PATH`.
- Resolve `codex_bin` through the existing settings reader and expand `~`.
  Treat it as one executable path, including paths containing spaces; never
  interpret shell commands, run startup files, scan projects, or select arbitrary
  installed versions. Invalid or empty settings use normal automatic discovery.
- Include the bounded OS error name in sign-in startup failures. Do not print
  raw exception messages, environment contents, provider responses, or tokens.
- Document automatic discovery, the custom-install escape hatch, and Herdr's
  per-pane `--env` option as a one-launch workaround for older plugin versions.
  Restarting the Codex daemon does not repair the picker's inherited environment.

# Verification and limits

Regressions launch the actual plugin command with an empty executable search
directory and either a configured fake CLI or an unconfigured user npm install.
The latter uses an env-based launcher plus a sibling fake runtime, proving that
both executable discovery and subprocess runtime lookup work from a fictitious
home containing spaces. Each CLI writes only a fictitious login to the empty
temporary authentication home. The account is saved and the shared fixture login
remains unchanged. Both cases fail before their fixes and pass afterward.
Other regressions cover inherited-PATH priority, custom XDG data locations,
home expansion, environment precedence, safe OS error codes, failed-login cleanup,
and preservation of the existing registration.

No real authentication or account switch is needed for these checks. The
process-environment comparison establishes the affected picker's missing lookup;
successful remote reauthentication still requires the user to complete sign-in.
