---
module: account-hub
date: 2026-10-09
problem_type: runtime_error
component: datastore
tags: [codex, registration, uuid, workspace, credential-store]
---

# Evidence

Source inspection showed that `save_live` deduplicated by `_fingerprint`, which
preferred Codex's `tokens.account_id`. That field can identify the workspace
shared by several people. A regression with three fictitious users, distinct
emails and credentials, and one workspace produced one saved profile instead
of three before the change. No real login was read or changed by the regression.

Initially Add new also permitted duplicate identities. The email-deduplication
policy below supersedes that behavior. The catalog storage key remains a local
UUID, independent of the provider's workspace and person metadata.

# Solution

Each new account gets a UUID4 record with its own credential file. Provider
identity remains metadata. A separate selection file points to a record UUID;
switching, token synchronization, usage caches, staged tokens, renaming and
deletion address that record, without finding a replacement by provider identity.

Reauthentication captures the live CLI login only into an explicitly supplied,
existing UUID. It preserves the UUID and label, invalidates that record's usage
and staged tokens, and leaves other registrations untouched. A different Codex
session is not silently synchronized into a record with matching email or
workspace. Missing session claims require a token in common to permit syncing;
otherwise the user must explicitly capture the login.

Existing profile files are read in place using stable UUIDs derived from local
filenames, without rewriting credentials on read or migrating conversation
history. Their filename remains unchanged when normal operations persist the ID.

## Manual save behavior (2026-10-10)

The user approved making **s** and the Save action ignore an account that is
already registered. Initially the person/workspace comparison decided whether to
skip a duplicate manual snapshot; it never chooses or replaces an existing
UUID. The picker displays `Account already saved` before asking for a name,
and `save_live` rechecks under the shared lock to serialize concurrent saves.

For Codex, a workspace without person metadata cannot establish a match;
only an identical snapshot permits skipping in that case. Different people
sharing a workspace remain separate. A new grant for an already saved person
also produces a message only, preserving credentials and refusal warnings;
recovery uses **a** against the explicit registration UUID.

This was established with fictitious credentials: repeated manual saves,
fresh login versus revoked snapshot, two concurrent save processes, missing
person metadata, and Codex/Claude picker actions. The staged Codex login test
originally verified that explicit Add new could register the same person independently.

## Current email policy and automatic-copy bug (2026-10-10)

Read-only inspection found six Codex registrations, including an original and
an `autosaved-` record with identical credentials and email. The live auth file
matched both, while the persisted selection named another email. The previous
active lookup required that selection to match the live grant, so it returned
no active registration. Switching then treated the outgoing login as unsaved
and appended an automatic copy regardless of existing account identity.

The user specified provider plus normalized email for duplicate detection and
current-account display. UUIDs continue to address credentials and actions.
Manual save, automatic preservation during switch, and Add new now skip an
email already registered, regardless of workspace/provider-user metadata.
Without an email, only identical credential snapshots match; a shared workspace
alone never groups accounts. This rule is applied under the existing lock for
state-changing operations.

`active_profile` is now a read-only account display lookup. For legacy duplicates,
it prefers a matching selected UUID, otherwise an original registration over an
automatic copy. It never writes selection or credentials. The separate
`_selected_login_profile` retains the strict grant check used by switching and
token synchronization. Detecting an email is not evidence of a daemon reload
and cannot authorize silent credential replacement.

Fictitious regressions cover stale/missing selection, normalized email across
changed workspace metadata, provider separation, Add new duplicate skips,
no automatic copy during switching, and unchanged credentials during display.
The redundant development `autosaved-` file was moved to the ignored, private
`.local/backups/duplicate-profiles/` after checking identical credentials and no
pending reset. The original UUID was retained, the live auth file remained
byte-for-byte unchanged, and five Codex registrations with one current marker
were verified locally. No provider request or authentication was performed for
this inspection or cleanup.

# Verification and limits

`python3 tests/check_switcher.py` runs the existing renewal/usage checks plus
`tests/test_account_records.py`. The latter uses temporary files, memory-only
credential backends and fake JWT claims. It covers distinct people sharing a
workspace, duplicate registrations, explicit selection and reauthentication,
ambiguous labels, unchanged credential paths on rename, and a store refusing
the requested credentials even when both logins name the same person.

These checks establish local record isolation. They do not establish provider
grant validity or prevent a provider from revoking or rotating authentication
copied into multiple registrations. No session/history copy or title change
is part of this implementation.
