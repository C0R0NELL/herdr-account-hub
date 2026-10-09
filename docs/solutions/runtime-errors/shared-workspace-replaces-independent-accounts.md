---
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

The requested registration contract also permits registering the same identity
twice. Consequently, using the provider user ID, email, or user/workspace pair
as the catalog key would still violate that contract.

# Solution

Each registration appends a UUID4 record with its own credential file. Provider
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

# Verification and limits

`python3 test_switcher.py` runs the existing renewal/usage checks plus
`test_account_records.py`. The latter uses temporary files, memory-only
credential backends and fake JWT claims. It covers distinct people sharing a
workspace, duplicate registrations, explicit selection and reauthentication,
ambiguous labels, unchanged credential paths on rename, and a store refusing
the requested credentials even when both logins name the same person.

These checks establish local record isolation. They do not establish provider
grant validity or prevent a provider from revoking or rotating authentication
copied into multiple registrations. No session/history copy or title change
is part of this implementation.
