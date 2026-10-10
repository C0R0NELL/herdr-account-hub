#!/usr/bin/env python3
"""Manage Claude Code / Codex accounts from inside Herdr.

Credential selection on disk does not prove a running Codex adopted it.
Codex account changes apply to its shared local daemon. Native terminal clients
reconnect to their existing chats after the official daemon restart.

What a profile holds (per agent kind):
  claude  the OAuth credential blob (macOS Keychain item "Claude Code-credentials",
          or ~/.claude/.credentials.json elsewhere) plus the `oauthAccount`
          identity block out of ~/.claude.json
  codex   ~/.codex/auth.json

Profiles live in this checkout's .local/profiles/<kind>/<uuid>.json, 0600 under
a 0700 directory. They are password-equivalent secrets: same value as the files
they came from, in the same place on the same machine, and never leave it.

Subcommands:
  ui       the overlay picker (plugin pane entrypoint)
  open     open that pane (action)
  next     switch to the next profile of the focused agent's kind (action)
  save     save the current account if it is not already registered (action)
  status   print what is logged in and what is saved (terminal diagnostic)
  switch   switch <kind> <uuid|unique-label> — the scriptable form
  reauth   reauth <kind> <uuid> — replace only this record from the live login
"""
import base64
import fcntl
import getpass
import hashlib
import json
import os
import platform
import queue
import random
import re
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import time
import uuid

PLUGIN_ID = os.environ.get("HERDR_PLUGIN_ID", "c0r0nell.account-hub")
HERDR = os.environ.get("HERDR_BIN_PATH", "herdr")
# Resolve the script rather than the caller's cwd or an inherited plugin path.
ROOT = os.path.dirname(os.path.realpath(__file__))
# Each checkout owns its private data. Herdr's injected state/config directories
# and Account Switch's environment settings must not redirect Account Hub.
STATE_DIR = os.path.expanduser(os.environ.get("ACCOUNT_HUB_STATE_DIR")
                              or os.path.join(ROOT, ".local"))
PROFILES_DIR = os.path.join(STATE_DIR, "profiles")
BACKUP_DIR = os.path.join(STATE_DIR, "backups")
LOCK = os.path.join(STATE_DIR, "switch.lock")
SELECTION = os.path.join(STATE_DIR, "selection.json")
KEEP_BACKUPS = 10
IS_MAC = platform.system() == "Darwin"


CONFIG_DIR = os.path.expanduser(os.environ.get("ACCOUNT_HUB_CONFIG_DIR")
                               or os.path.join(STATE_DIR, "config"))
PREFIX = "ACCOUNT_HUB_"


def _load_settings():
    """Settings from the plugin's own config dir.

    Actions inherit the herdr server's environment, so an environment variable
    can only be set by restarting herdr with it exported. A file in the config
    dir is the one place a person can actually change these.
    """
    for name in ("config.toml", "config.json"):
        path = os.path.join(CONFIG_DIR, name)
        if not os.path.exists(path):
            continue
        try:
            if name.endswith(".toml"):
                import tomllib
                with open(path, "rb") as handle:
                    return tomllib.load(handle)
            with open(path, encoding="utf-8") as handle:
                return json.load(handle)
        except Exception:
            continue  # a broken config must not stop the picker
    return {}


SETTINGS = _load_settings()


def _setting(name):
    """The environment first, then the config file, then nothing.

    The config key is the variable without its prefix, lowercased:
    ACCOUNT_HUB_USAGE_RENEW is `usage_renew`.
    """
    if name in os.environ:
        return os.environ[name]
    key = name[len(PREFIX):].lower() if name.startswith(PREFIX) else name.lower()
    return SETTINGS.get(key)


def _flag(name, default=True):
    value = _setting(name)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("0", "false", "no", "off", "")


def _num_env(name, default):
    try:
        return float(_setting(name))
    except (TypeError, ValueError):
        return default


NOTIFY = _flag("ACCOUNT_HUB_NOTIFY")
# How long any one command we shell out to may take before it is abandoned.
# Generous: it is a backstop against never finishing, not a performance budget.
COMMAND_TIMEOUT_S = _num_env("ACCOUNT_HUB_COMMAND_TIMEOUT_S", 30.0)
# How long another reader/switch waits for the shared lock. A Codex restart can
# hold it during graceful shutdown; competing actions report that wait.
LOCK_TIMEOUT_S = _num_env("ACCOUNT_HUB_LOCK_TIMEOUT_S", 5.0)


class SwitchError(Exception):
    """An operation that could not be confirmed; message describes recovery."""


# ---- running a command ----------------------------------------------------

def _abandon(proc):
    """Kill a command's whole process group, not only the command.

    Killing the command alone is not enough: something it started can inherit
    the command's own descriptors and outlive it, so the group goes rather
    than the one process.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except OSError:
        pass
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass


def _run(argv, stdin_text=None, timeout=None, env=None):
    """Run one command, and wait only on the command itself.

    Output goes to temporary files rather than pipes, because a pipe is read
    until end of file and not until the child exits. Those are the same moment
    only while nothing else holds the write end. A picker was found stopped in
    exactly that read, holding the switch lock: the command had already exited
    and something it had started still held the write end. Every later picker
    and every `next` queued behind that lock, the oldest for eleven hours, and
    each of them drew a blank window and said nothing. A file ends when the
    command does, so a process that outlives it cannot hold us.

    The timeout is the remaining backstop, for a command that never exits at
    all. The command runs in its own session, so the whole group can be cut
    loose then and not only the process that has already left.

    Returns a CompletedProcess, and never raises on a non-zero exit. A command
    that had to be abandoned reports a failure, the same as one that failed.
    """
    if timeout is None:
        timeout = COMMAND_TIMEOUT_S
    with tempfile.TemporaryFile(mode="w+") as out, \
            tempfile.TemporaryFile(mode="w+") as err:
        # Input goes to a file too. A secret handed over this way is as absent
        # from `ps` output as one written down a pipe, and a command that never
        # reads it cannot wedge the write.
        stdin = None
        if stdin_text is not None:
            stdin = tempfile.TemporaryFile(mode="w+")
            stdin.write(stdin_text)
            stdin.seek(0)
        try:
            proc = subprocess.Popen(
                argv, stdout=out, stderr=err,
                stdin=stdin if stdin is not None else subprocess.DEVNULL,
                text=True, start_new_session=True, env=env,
            )
        finally:
            if stdin is not None:
                stdin.close()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _abandon(proc)
            return subprocess.CompletedProcess(
                argv, 1, "", "no answer in %gs, gave up on it" % timeout)
        out.seek(0)
        err.seek(0)
        return subprocess.CompletedProcess(
            argv, proc.returncode, out.read(), err.read())


# ---- herdr plumbing -------------------------------------------------------

def herdr(*args):
    """Run the herdr CLI; return CompletedProcess (never raises on non-zero)."""
    return _run([HERDR, *args])


def live_agents():
    """pane_id -> agent info, or None when the herdr server is unreachable
    (distinct from an empty session)."""
    res = herdr("agent", "list")
    if res.returncode != 0:
        return None
    try:
        agents = json.loads(res.stdout)["result"]["agents"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None
    return {a["pane_id"]: a for a in agents if a.get("pane_id")}


def notify(title, body=""):
    if not NOTIFY:
        return
    args = ["notification", "show", title, "--sound", "none"]
    if body:
        args += ["--body", body]
    try:
        herdr(*args)
    except OSError:
        # No herdr to notify through. This also runs on the failure path, where
        # a traceback would bury the very message it was called to carry.
        pass


class _Lock:
    """Serialise credential swaps: two overlapping writes to the same store
    would race the CLIs' own token refresh.

    Re-entrant, because the nesting is real: switch() holds this lock while
    proven_payload renews, and renew_profile takes it again. flock keys on the
    open file rather than on the process, so a second open() inside the first
    would queue behind a lock this very process already holds, and wait for
    ever.

    The wait is bounded, because flock has no timeout of its own. One picker
    stuck while holding this lock used to stop every later picker and every
    `next`, for as long as it stayed stuck, without a word on screen.
    """

    _depth = 0
    _file = None

    def __enter__(self):
        if _Lock._depth == 0:
            _secure_dir(STATE_DIR)
            handle = open(LOCK, "w")
            began = time.time()
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                # Only "someone else holds it" is waited out. A lock that
                # cannot be taken at all still raises, rather than spending the
                # timeout to report a holder that was never there.
                except BlockingIOError:
                    if time.time() - began >= LOCK_TIMEOUT_S:
                        handle.close()
                        raise SwitchError(
                            "another switch is still running — it has held the "
                            "lock for over %gs and may be stuck; nothing "
                            "changed" % LOCK_TIMEOUT_S
                        )
                    time.sleep(0.05)
            _Lock._file = handle
        _Lock._depth += 1
        return self

    def __exit__(self, *exc):
        _Lock._depth -= 1
        if _Lock._depth == 0 and _Lock._file is not None:
            fcntl.flock(_Lock._file, fcntl.LOCK_UN)
            _Lock._file.close()
            _Lock._file = None


# ---- small file helpers ---------------------------------------------------

def _secure_dir(path):
    """Create one of *our* state directories, owner-only. Never used on
    directories we don't own (~/.claude, $HOME) — their modes aren't ours."""
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _write_json_secret(path, data, mode=0o600):
    """Atomic, mode-preserving write of a credential file."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _fingerprint(identity):
    """Provider identity guard, never a registration key.

    Codex's account_id can name a shared workspace; include the person too.
    """
    account = identity.get("account_id")
    person = identity.get("user_id") or identity.get("email")
    if not account and not person:
        return None
    key = json.dumps([account, person], sort_keys=True)
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _jwt_claims(token):
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


# ---- credential backends --------------------------------------------------

class Backend:
    kind = ""
    title = ""
    # Short provider name used in sign-in dialogs and terminal instructions.
    short = ""

    def present(self):
        """True when this agent looks configured on this machine."""
        raise NotImplementedError

    def read_live(self):
        """Current credential payload, or None when nothing is logged in."""
        raise NotImplementedError

    def write_live(self, payload):
        """Install a payload as the live login. Raises SwitchError on failure."""
        raise NotImplementedError

    def identity(self, payload):
        """Human/stable identity fields for a payload."""
        raise NotImplementedError

    def describe(self, identity):
        bits = [identity.get("email") or identity.get("account_id") or "unknown"]
        if identity.get("plan"):
            bits.append(identity["plan"])
        if identity.get("org"):
            bits.append(identity["org"])
        return " | ".join(str(b) for b in bits if b)


class ClaudeBackend(Backend):
    kind = "claude"
    title = "Claude Code"
    short = "Claude"
    KEYCHAIN_SERVICE = "Claude Code-credentials"

    def __init__(self):
        cfg = os.environ.get("CLAUDE_CONFIG_DIR")
        if cfg:
            self.home = os.path.expanduser(cfg)
            self.settings = os.path.join(self.home, ".claude.json")
        else:
            self.home = os.path.expanduser("~/.claude")
            self.settings = os.path.expanduser("~/.claude.json")
        self.creds_file = os.path.join(self.home, ".credentials.json")

    def present(self):
        return os.path.isdir(self.home) or os.path.exists(self.settings)

    # -- keychain (macOS) --
    def _keychain_read(self):
        for args in (
            ["-a", getpass.getuser(), "-w"],
            ["-w"],  # some installs store it under a different account name
        ):
            r = _run(
                ["security", "find-generic-password", "-s", self.KEYCHAIN_SERVICE, *args]
            )
            if r.returncode == 0 and r.stdout.strip():
                try:
                    return json.loads(r.stdout.strip())
                except json.JSONDecodeError:
                    return None
        return None

    def _keychain_write(self, data):
        blob = json.dumps(data, separators=(",", ":"))
        base = [
            "security", "add-generic-password", "-U",
            "-s", self.KEYCHAIN_SERVICE, "-a", getpass.getuser(),
        ]
        # Prefer feeding the secret on stdin so it never lands in `ps` output;
        # fall back to the argv form if this `security` build won't take it.
        attempts = [
            (base + ["-w"], blob + "\n"),
            (base + ["-w", blob], None),
        ]
        for argv, stdin in attempts:
            _run(argv, stdin_text=stdin)
            if self._keychain_read() == data:
                return True
        return False

    # -- payload --
    def _read_creds(self):
        """(payload, store) for whichever store actually holds the login."""
        if IS_MAC:
            kc = self._keychain_read()
            if kc is not None:
                return kc, "keychain"
        f = _read_json(self.creds_file)
        if f is not None:
            return f, "file"
        return None, None

    def _oauth_account(self):
        settings = _read_json(self.settings) or {}
        acct = settings.get("oauthAccount")
        return acct if isinstance(acct, dict) else None

    def read_live(self):
        creds, store = self._read_creds()
        if creds is None:
            return None
        return {
            "store": store,
            "credentials": creds,
            "oauth_account": self._oauth_account(),
        }

    def write_live(self, payload):
        creds = payload.get("credentials")
        if not creds:
            raise SwitchError("profile holds no claude credentials")

        # Write to whatever store is live now; if nothing is logged in, fall
        # back to the store the profile was captured from.
        _, live_store = self._read_creds()
        store = live_store or payload.get("store") or ("keychain" if IS_MAC else "file")

        if store == "keychain":
            if not self._keychain_write(creds):
                raise SwitchError(
                    "could not write the Keychain item 'Claude Code-credentials' "
                    "(unlock the login keychain and retry)"
                )
        else:
            _write_json_secret(self.creds_file, creds)

        # Identity block: patch only `oauthAccount`, everything else in
        # ~/.claude.json (projects, history, onboarding) is not ours to touch.
        acct = payload.get("oauth_account")
        if acct:
            settings = _read_json(self.settings)
            if settings is None:
                settings = {}
            settings["oauthAccount"] = acct
            mode = 0o600
            try:
                mode = os.stat(self.settings).st_mode & 0o777
            except OSError:
                pass
            _write_json_secret(self.settings, settings, mode=mode)

    def identity(self, payload):
        acct = payload.get("oauth_account") or {}
        oauth = (payload.get("credentials") or {}).get("claudeAiOauth") or {}
        return {
            "email": acct.get("emailAddress"),
            "account_id": acct.get("accountUuid"),
            "org": acct.get("organizationName"),
            "plan": oauth.get("subscriptionType"),
        }


class CodexBackend(Backend):
    kind = "codex"
    title = "Codex"
    short = "Codex"

    def __init__(self):
        self.home = os.path.expanduser(os.environ.get("CODEX_HOME") or "~/.codex")
        self.auth = os.path.join(self.home, "auth.json")

    def present(self):
        return os.path.isdir(self.home)

    def read_live(self):
        data = _read_json(self.auth)
        return {"auth": data} if data is not None else None

    def write_live(self, payload):
        auth = payload.get("auth")
        if not auth:
            raise SwitchError("profile holds no codex credentials")
        _write_json_secret(self.auth, auth)

    def prepare_switch(self):
        return CodexMachine(self)

    def identity(self, payload):
        auth = payload.get("auth") or {}
        tokens = auth.get("tokens") or {}
        claims = _jwt_claims(tokens.get("id_token", "")) if tokens else {}
        openai = claims.get("https://api.openai.com/auth") or {}
        account_id = (
            tokens.get("account_id")
            or openai.get("chatgpt_account_id")
            or (("apikey:" + hashlib.sha256(
                auth["OPENAI_API_KEY"].encode()).hexdigest()[:12])
                if auth.get("OPENAI_API_KEY") else None)
        )
        return {
            "email": claims.get("email"),
            "account_id": account_id,
            "user_id": openai.get("chatgpt_user_id") or openai.get("user_id")
                       or claims.get("sub"),
            "org": None,
            "plan": openai.get("chatgpt_plan_type") or auth.get("auth_mode"),
        }


BACKENDS = {b.kind: b for b in (ClaudeBackend(), CodexBackend())}
KIND_ORDER = ["claude", "codex"]


def kind_of_agent(info):
    """Map a herdr agent label ("Claude Code", "codex", ...) to a backend."""
    label = " ".join(
        str(info.get(k) or "") for k in ("agent", "name", "display_agent")
    ).lower()
    for kind in KIND_ORDER:
        if kind in label:
            return kind
    return None


# ---- profile store --------------------------------------------------------

def _kind_dir(kind):
    if kind not in BACKENDS:
        raise SwitchError("unknown account kind")
    return os.path.join(PROFILES_DIR, kind)


def _valid_uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def _storage_name(value):
    # Legacy filenames stay in place. Never accept a path from profile JSON.
    if (not isinstance(value, str) or value in (".", "..")
            or not re.fullmatch(r"[a-zA-Z0-9._-]+", value)):
        raise SwitchError("invalid profile filename")
    return value


def list_profiles(kind):
    """Read records without migrating files or looking up provider identities."""
    out = []
    try:
        names = sorted(os.listdir(_kind_dir(kind)))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json") or os.path.islink(os.path.join(_kind_dir(kind), name)):
            continue
        data = _read_json(os.path.join(_kind_dir(kind), name))
        if isinstance(data, dict) and data.get("payload"):
            try:
                data["slug"] = _storage_name(name[:-5])
            except SwitchError:
                continue
            data["kind"] = kind
            if "id" not in data:
                # Compatibility for existing files only: a local filename,
                # not email/workspace/provider id, gives a stable UUID.
                data["id"] = str(uuid.uuid5(uuid.NAMESPACE_URL,
                    "account-hub/profile/" + kind + "/" + data["slug"]))
            if not _valid_uuid(data["id"]):
                continue
            data.setdefault("label", data["slug"])
            data["identity"] = BACKENDS[kind].identity(data["payload"])
            data["fingerprint"] = _fingerprint(data["identity"])
            out.append(data)
    # Renewal changes saved_at, not registration order. Legacy records have
    # no creation date; keep their ordering stable by UUID instead.
    out.sort(key=lambda p: (p.get("created_at") or 0, p["id"]))
    return out


def get_profile(kind, key):
    """UUID first; labels and legacy filenames must be unambiguous."""
    profiles = list_profiles(kind)
    for p in profiles:
        if key == p["id"]:
            return p
    if not isinstance(key, str):
        return None
    matches = [p for p in profiles if key == p["slug"]
               or key.casefold() == str(p["label"]).casefold()]
    if len(matches) > 1:
        raise SwitchError(f"ambiguous {kind} profile label {key!r}; use its UUID")
    return matches[0] if matches else None


def _profile_by_id(kind, profile_id):
    if not _valid_uuid(profile_id):
        raise SwitchError("an explicit profile UUID is required")
    profile = next((p for p in list_profiles(kind) if p["id"] == profile_id), None)
    if profile is None:
        raise SwitchError(f"no {kind} profile with UUID {profile_id!r}")
    return profile


def write_profile(profile):
    if not _valid_uuid(profile.get("id")):
        raise SwitchError("profile needs a UUID")
    filename = _storage_name(profile["slug"])
    with _Lock():
        _secure_dir(PROFILES_DIR)
        _secure_dir(_kind_dir(profile["kind"]))
        path = os.path.join(_kind_dir(profile["kind"]), filename + ".json")
        _write_json_secret(path, profile)
    return path


def _selection():
    value = _read_json(SELECTION)
    return value if isinstance(value, dict) else {}


def _select_profile(kind, profile_id):
    with _Lock():
        selected = _selection()
        if profile_id is None:
            selected.pop(kind, None)
        else:
            _profile_by_id(kind, profile_id)
            selected[kind] = profile_id
        _write_json_secret(SELECTION, selected)


def _forget_profile_usage(kind, profile_id):
    cache = _usage_cache()
    if cache.pop(f"{kind}:{profile_id}", None) is not None:
        _write_json_secret(USAGE_CACHE, cache)


def delete_profile(kind, profile_id):
    with _Lock():
        try:
            profile = _profile_by_id(kind, profile_id)
            os.remove(os.path.join(_kind_dir(kind), profile["slug"] + ".json"))
        except (OSError, SwitchError):
            return False
        if _selection().get(kind) == profile_id:
            _select_profile(kind, None)
        _forget_profile_usage(kind, profile_id)
        _clear_staged(kind, profile_id)
        if profile["slug"] != profile_id:
            _clear_staged(kind, profile["slug"])
        return True


def rename_profile(kind, profile_id, label):
    with _Lock():
        profile = _profile_by_id(kind, profile_id)
        profile["label"] = label
        write_profile(profile)


def make_profile(kind, label, payload):
    backend = BACKENDS[kind]
    identity = backend.identity(payload)
    profile_id = str(uuid.uuid4())
    return {
        "kind": kind,
        "id": profile_id,
        "slug": profile_id,
        "label": label,
        "identity": identity,
        "fingerprint": _fingerprint(identity),
        "saved_at": int(time.time()),
        "created_at": time.time(),
        "last_used": None,
        "payload": payload,
    }


def default_label(kind, identity):
    email = identity.get("email") or ""
    if "@" in email:
        return email.split("@", 1)[0]
    if identity.get("account_id"):
        return str(identity["account_id"])[:8]
    return f"{kind}-{int(time.time())}"


def _same_login(kind, profile, live):
    """Guard a selected record against an unrelated external login.

    Session claims are optional. Without them, Codex needs a token still in
    common; otherwise explicit reauth is safer than guessing who to overwrite.
    """
    backend = BACKENDS[kind]
    saved = profile.get("payload") or {}
    fp = _fingerprint(backend.identity(live)) if live else None
    if not fp or fp != _fingerprint(backend.identity(saved)):
        return False
    if kind != "codex" or saved == live:
        return True
    old = (saved.get("auth") or {}).get("tokens") or {}
    new = (live.get("auth") or {}).get("tokens") or {}
    markers = []
    for field in ("id_token", "access_token"):
        a, b = _jwt_claims(old.get(field, "")), _jwt_claims(new.get(field, ""))
        for claim in ("sid", "auth_time"):
            if claim in a or claim in b:
                if a.get(claim) != b.get(claim):
                    return False
                markers.append(claim)
    return bool(markers) or any(old.get(k) and old[k] == new.get(k)
                                for k in ("access_token", "refresh_token"))


def active_profile(kind, live=None):
    """The explicitly selected UUID, provided its login is still live."""
    backend = BACKENDS[kind]
    if live is None:
        live = backend.read_live()
    if live is None:
        return None
    profile_id = _selection().get(kind)
    if not _valid_uuid(profile_id):
        return None
    for p in list_profiles(kind):
        if p["id"] == profile_id and _same_login(kind, p, live):
            return p
    return None


def _live_account_saved(kind, live, profiles=None):
    """Detect an existing account without selecting or changing any UUID.

    A shared Codex workspace alone cannot identify a person. When person
    metadata is absent, only an identical credential snapshot proves a match.
    """
    if live is None:
        return False
    identity = BACKENDS[kind].identity(live)
    fingerprint = _fingerprint(identity)
    known_person = kind != "codex" or identity.get("user_id") or identity.get("email")
    records = list_profiles(kind) if profiles is None else profiles
    return any(p["payload"] == live or (fingerprint and known_person
               and p["fingerprint"] == fingerprint) for p in records)


def _backup(kind, payload):
    _secure_dir(BACKUP_DIR)
    path = os.path.join(BACKUP_DIR, f"{kind}-{int(time.time())}.json")
    _write_json_secret(path, payload)
    kept = sorted(
        f for f in os.listdir(BACKUP_DIR) if f.startswith(kind + "-")
    )
    for stale in kept[:-KEEP_BACKUPS]:
        try:
            os.remove(os.path.join(BACKUP_DIR, stale))
        except OSError:
            pass
    return path


# ---- the switch itself ----------------------------------------------------

def switch(kind, key):
    """Apply a saved account; Codex uses one account for this machine's home."""
    backend = BACKENDS[kind]
    with _Lock():
        target = get_profile(kind, key)
        if target is None:
            raise SwitchError(f"no {kind} profile named {key!r}")
        live = backend.read_live()
        outgoing = active_profile(kind, live) if live is not None else None
        runtime = backend.prepare_switch() if kind == "codex" else None
        if outgoing is not None and outgoing["id"] == target["id"] and runtime is None:
            sync_live_profile(kind, live)
            return f"{kind}: already on {target['label']}"
        if kind == "codex" and outgoing and outgoing["id"] == target["id"]:
            # Codex may already have rotated the selected login. Verify its
            # current credentials, never spend the older saved refresh token.
            sync_live_profile(kind, live)
            target = _profile_by_id(kind, target["id"])
        target["payload"] = proven_payload(kind, target)

        if live is not None:
            if outgoing is None:
                ident = backend.identity(live)
                write_profile(make_profile(kind, "autosaved-" + default_label(kind, ident), live))
            elif outgoing["id"] != target["id"]:
                outgoing["payload"] = live
                outgoing["identity"] = backend.identity(live)
                outgoing["saved_at"] = int(time.time())
                write_profile(outgoing)
            _backup(kind, live)

        # Renewing the account already on disk can spend its old refresh token.
        # Recovery for that same registration must retain the renewed grant.
        previous = (target["payload"] if outgoing and outgoing["id"] == target["id"]
                    else live)

        try:
            backend.write_live(target["payload"])
            check = backend.read_live()
            wanted = target["payload"]
            credential_key = "auth" if kind == "codex" else "credentials"
            got = _fingerprint(backend.identity(check)) if check else None
            if (not check or got != _fingerprint(backend.identity(wanted))
                    or check.get(credential_key) != wanted.get(credential_key)):
                raise SwitchError(f"{kind}: credential write did not take")
            result = runtime.apply(wanted) if runtime else ""
            # Adopt a rotation performed naturally by the restarted Codex.
            applied = backend.read_live()
            if not applied or (kind == "codex" and not _same_login(kind, target, applied)):
                raise SwitchError("Credentials changed during reload; account not confirmed")
            target["payload"] = applied
            target["identity"] = backend.identity(target["payload"])
            target["fingerprint"] = _fingerprint(target["identity"])
            target["last_used"] = int(time.time())
            write_profile(target)
            if runtime:
                runtime.record(target)
            _select_profile(kind, target["id"])
        except (SwitchError, OSError) as exc:
            try:
                if previous is not None:
                    backend.write_live(previous)
                elif kind == "codex":
                    os.unlink(backend.auth)
                if runtime and runtime.restarted:
                    if previous is None:
                        raise SwitchError("previous account was signed out")
                    runtime.apply(previous)
            except (SwitchError, OSError):
                raise SwitchError("Account change failed; previous login reload could not be confirmed. Check Codex before retrying.") from exc
            raise SwitchError(str(exc) + "; previous login restored") from exc

    ident = backend.describe(target.get("identity") or {})
    if kind == "codex":
        message = f"Codex: switched to {target['label']} for this machine; {result}"
        notify("Codex account switched", target["label"])
        return message
    notify(f"{backend.title}: {target['label']}", ident)
    return f"{kind}: switched to {target['label']} ({ident})"


def next_profile(kind):
    profiles = list_profiles(kind)
    if not profiles:
        raise SwitchError(f"no saved {kind} profiles yet — run the save action first")
    if len(profiles) == 1:
        return switch(kind, profiles[0]["id"])
    current = active_profile(kind)
    idx = 0
    if current:
        ids = [p["id"] for p in profiles]
        idx = (ids.index(current["id"]) + 1) % len(profiles)
    return switch(kind, profiles[idx]["id"])


# ---- Codex machine-wide authentication reload -----------------------------

CODEX = os.environ.get("ACCOUNT_HUB_CODEX_BIN", "codex")
CODEX_APPLICATION = os.path.join(STATE_DIR, "codex-application.json")
CODEX_RECONNECT_TIMEOUT_S = _num_env("ACCOUNT_HUB_CODEX_RECONNECT_TIMEOUT_S", 120.0)


def _codex_environment():
    environment = os.environ.copy()
    environment["CODEX_HOME"] = BACKENDS["codex"].home
    # Keep CODEX_SQLITE_HOME and the user's config (including sqlite_home).
    return environment


def _codex_shutdown_grace_seconds(home):
    """Match the official daemon's stop-settings defaults and 0..300s range."""
    settings = _read_json(os.path.join(home, "app-server-daemon", "settings.json"))
    grace = settings.get("shutdownGraceSeconds", 60) if isinstance(settings, dict) else 60
    return grace if type(grace) is int and 0 <= grace <= 300 else 60


def _check_codex_compatibility():
    try:
        result = _run([CODEX, "--version"], env=_codex_environment())
    except OSError as exc:
        raise SwitchError("Codex CLI is not available; account unchanged") from exc
    version = re.search(r"(\d+)\.(\d+)\.(\d+)", result.stdout)
    if (result.returncode or not version
            or tuple(map(int, version.groups())) < (0, 162, 0)):
        raise SwitchError("Automatic chat reload requires Codex CLI 0.162.0 or newer")


class CodexMachine:
    """Reload the existing shared daemon; native clients reconnect themselves.

    The credential store remains the source of truth. This object is a short
    lived switch transaction, not another Codex launcher or authentication host.
    """

    def __init__(self, backend):
        self.backend = backend
        self.environment = _codex_environment()
        self.info = None
        self.threads = {}
        self.restarted = False
        _check_codex_compatibility()
        result = _run([CODEX, "app-server", "daemon", "version"], env=self.environment)
        if result.returncode:
            # The official passive probe reports these OS errors for an absent
            # socket. A timeout, permission failure or malformed reply is not
            # evidence that no daemon is running.
            if ("failed to connect to" in result.stderr and any(
                    reason in result.stderr for reason in (
                        "No such file or directory", "Connection refused"))):
                self._check_file_store()
                return
            raise SwitchError("Cannot inspect the Codex daemon; account unchanged")
        self.info = self._parse_info(result)
        if self.info.get("status") == "notRunning":
            self.info = None
            self._check_file_store()
            return
        if self.info.get("status") != "running" or self.info.get("backend") != "pid":
            raise SwitchError("Codex is not using its managed local daemon; account unchanged")
        version = re.search(r"(\d+)\.(\d+)\.(\d+)", self.info.get("appServerVersion") or "")
        if not version or tuple(map(int, version.groups())) < (0, 162, 0):
            raise SwitchError("Automatic chat reload requires Codex daemon 0.162.0 or newer")
        rpc = self._connect(self.info)
        try:
            config = rpc.call("config/read", {"includeLayers": False}).get("config") or {}
            self._check_config(config)
            for thread_id in self._loaded(rpc):
                thread = self._thread(rpc, thread_id)
                if thread.get("ephemeral"):
                    raise SwitchError("An open chat is temporary and cannot be safely reloaded")
                self.threads[thread_id] = thread
        finally:
            rpc.close()

    def _check_config(self, config):
        if (config.get("cli_auth_credentials_store") or "file") != "file":
            raise SwitchError('Codex must use cli_auth_credentials_store = "file" to switch auth.json')
        if (config.get("model_provider") or "openai") != "openai":
            raise SwitchError("This Codex server uses a different model provider; account unchanged")

    def _check_file_store(self):
        path = os.path.join(self.backend.home, "config.toml")
        if not os.path.exists(path):
            return
        try:
            import tomllib
            with open(path, "rb") as handle:
                self._check_config(tomllib.load(handle))
        except (ImportError, ValueError, OSError) as exc:
            raise SwitchError("Cannot inspect Codex configuration; account unchanged") from exc

    @staticmethod
    def _parse_info(result):
        try:
            info = json.loads(result.stdout)
            if not isinstance(info, dict):
                raise ValueError()
            return info
        except (ValueError, TypeError) as exc:
            raise SwitchError("Codex returned an invalid daemon response") from exc

    def _connect(self, info):
        path = info.get("socketPath")
        try:
            metadata = os.stat(path)
            if not os.path.isabs(path) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
                raise OSError("unsafe socket")
            return CodexRPC(path, self.environment)
        except (OSError, TypeError, ValueError) as exc:
            raise SwitchError("Cannot connect to the private Codex control socket") from exc

    @staticmethod
    def _loaded(rpc):
        ids, seen, cursor = set(), set(), None
        for _ in range(1000):
            result = rpc.call("thread/loaded/list", {"cursor": cursor, "limit": 100})
            page = result.get("data")
            if not isinstance(page, list) or any(not _valid_uuid(value) for value in page):
                raise SwitchError("Cannot identify the open Codex chats")
            ids.update(page)
            cursor = result.get("nextCursor")
            if not cursor:
                return ids
            if cursor in seen:
                break
            seen.add(cursor)
        raise SwitchError("Codex returned an incomplete chat list")

    @staticmethod
    def _thread(rpc, thread_id):
        thread = rpc.call("thread/read", {"threadId": thread_id, "includeTurns": False}).get("thread") or {}
        if thread.get("id") != thread_id or not thread.get("cwd"):
            raise SwitchError("Cannot confirm the existing chat and directory")
        return thread

    def apply(self, payload):
        if self.info is None:
            return "new chats will use this account"
        self.restarted = True
        grace = _codex_shutdown_grace_seconds(self.backend.home)
        result = _run([CODEX, "app-server", "daemon", "restart"],
                      env=self.environment, timeout=grace + 90)
        if result.returncode:
            raise SwitchError("Codex could not restart; account change not confirmed")
        info = self._parse_info(result)
        if info.get("status") != "restarted" or info.get("backend") != "pid":
            raise SwitchError("Codex did not confirm its restart")
        rpc = self._connect(info)
        try:
            self._check_config(rpc.call("config/read", {"includeLayers": False}).get("config") or {})
            account = rpc.call("account/read", {"refreshToken": False}).get("account") or {}
            auth = payload.get("auth") or {}
            if auth.get("OPENAI_API_KEY"):
                confirmed = account.get("type") == "apiKey"
            else:
                expected = self.backend.identity(payload).get("email")
                confirmed = bool(expected and account.get("type") == "chatgpt"
                                 and account.get("email") == expected)
            if not confirmed:
                raise SwitchError("The restarted Codex did not adopt the selected account")
            deadline = time.monotonic() + CODEX_RECONNECT_TIMEOUT_S
            while True:
                if set(self.threads).issubset(self._loaded(rpc)):
                    break
                if time.monotonic() >= deadline:
                    raise SwitchError("Some chats have not reconnected; account change not confirmed")
                time.sleep(.2)
            for thread_id, before in self.threads.items():
                after = self._thread(rpc, thread_id)
                if any(before.get(key) != after.get(key) for key in ("id", "name", "cwd")):
                    raise SwitchError("Codex did not preserve a chat's ID, title or directory")
            live = self.backend.read_live()
            if not _same_login("codex", {"payload": payload}, live):
                raise SwitchError("Codex credentials changed during reload; account not confirmed")
            self.info = info
        finally:
            rpc.close()
        return "%d chats reloaded" % len(self.threads)

    def record(self, profile):
        info = self.info
        metadata = os.stat(info["socketPath"]) if info else None
        _write_json_secret(CODEX_APPLICATION, {
            "profile_id": profile["id"], "home": os.path.realpath(self.backend.home),
            "status": "applied" if info else "ready_for_new_chats",
            "socket": info.get("socketPath") if info else None,
            "socket_generation": [metadata.st_dev, metadata.st_ino] if metadata else None,
            "auth_hash": _codex_auth_hash(self.backend.read_live()),
            "chat_count": len(self.threads), "at": int(time.time()),
        })


def _codex_auth_hash(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def codex_application_status():
    """A stale file/socket generation never claims that a switch is applied."""
    state = _read_json(CODEX_APPLICATION) or {}
    backend = BACKENDS["codex"]
    if (state.get("profile_id") != _selection().get("codex")
            or state.get("home") != os.path.realpath(getattr(backend, "home", ""))
            or state.get("auth_hash") != _codex_auth_hash(backend.read_live())):
        return "reload unverified"
    if state.get("status") == "ready_for_new_chats":
        return "ready for new chats"
    try:
        metadata = os.stat(state["socket"])
        if [metadata.st_dev, metadata.st_ino] == state.get("socket_generation"):
            return "applied"
    except (KeyError, OSError, TypeError):
        pass
    return "reload unverified"


class _UnixWebSocket:
    """The local Codex control socket uses WebSocket frames, not raw JSONL."""

    MAX_MESSAGE = 8 * 1024 * 1024

    def __init__(self, path):
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(20)
        self.buffer = bytearray()
        self.send_lock = threading.Lock()
        try:
            self.socket.connect(path)
            key = base64.b64encode(os.urandom(16)).decode()
            self.socket.sendall(("GET / HTTP/1.1\r\nHost: localhost\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
            while b"\r\n\r\n" not in self.buffer:
                chunk = self.socket.recv(4096)
                if not chunk or len(self.buffer) > 16384:
                    raise SwitchError("Codex WebSocket handshake failed")
                self.buffer.extend(chunk)
            headers, remainder = bytes(self.buffer).split(b"\r\n\r\n", 1)
            self.buffer = bytearray(remainder)
            lines = headers.decode("ascii").split("\r\n")
            fields = {name.lower(): value.strip() for name, value in
                      (line.split(":", 1) for line in lines[1:] if ":" in line)}
            expected = base64.b64encode(hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
            if (lines[0].split()[1] != "101"
                    or fields.get("sec-websocket-accept", "") != expected):
                raise SwitchError("Codex WebSocket handshake was not accepted")
            self.socket.settimeout(None)
        except Exception:
            self.socket.close()
            raise

    def _read(self, size):
        while len(self.buffer) < size:
            chunk = self.socket.recv(min(65536, size - len(self.buffer)))
            if not chunk:
                raise OSError("Codex socket closed")
            self.buffer.extend(chunk)
        result = bytes(self.buffer[:size])
        del self.buffer[:size]
        return result

    def _send_frame(self, opcode, payload):
        if len(payload) > self.MAX_MESSAGE:
            raise SwitchError("Codex control message is too large")
        mask = os.urandom(4)
        size = len(payload)
        header = bytes([0x80 | opcode])
        if size < 126:
            header += bytes([0x80 | size])
        elif size < 65536:
            header += bytes([0x80 | 126]) + struct.pack("!H", size)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", size)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        with self.send_lock:
            self.socket.sendall(header + mask + masked)

    def send(self, message):
        self._send_frame(1, json.dumps(message).encode())

    def receive(self):
        fragments = bytearray()
        started = False
        while True:
            first, second = self._read(2)
            opcode, final = first & 15, bool(first & 128)
            if first & 0x70 or second & 128:
                raise OSError("unsupported Codex WebSocket frame")
            size = second & 127
            if size == 126:
                size = struct.unpack("!H", self._read(2))[0]
            elif size == 127:
                size = struct.unpack("!Q", self._read(8))[0]
            if size > self.MAX_MESSAGE or len(fragments) + size > self.MAX_MESSAGE:
                raise OSError("Codex message exceeds the control limit")
            payload = self._read(size)
            if opcode in (8, 9, 10):
                if not final or size > 125:
                    raise OSError("invalid Codex WebSocket control frame")
                if opcode == 8:
                    raise OSError("Codex connection closed")
                if opcode == 9:
                    self._send_frame(10, payload)
                continue
            if (opcode == 1 and not started) or (opcode == 0 and started):
                started = True
                fragments.extend(payload)
            else:
                raise OSError("unsupported Codex control message")
            if final:
                return json.loads(fragments.decode())

    def close(self):
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.socket.close()


class CodexRPC:
    """JSON-RPC over a private Unix WebSocket, with bounded request waits."""

    def __init__(self, path, environment, on_request=None):
        self.transport = _UnixWebSocket(path)
        self.messages = queue.Queue(maxsize=4096)
        self.counter = 0
        self.on_request = on_request
        threading.Thread(target=self._reader, daemon=True).start()
        try:
            self.call("initialize", {"clientInfo": {
                "name": "account_hub", "title": "Account Hub", "version": "0.2.0"},
                "capabilities": {"experimentalApi": False}})
            self.send({"method": "initialized"})
        except Exception:
            self.close()
            raise

    def _reader(self):
        try:
            while True:
                self.messages.put(self.transport.receive(), timeout=1)
        except (ValueError, OSError, queue.Full):
            pass
        finally:
            try:
                self.messages.put(None, timeout=1)
            except queue.Full:
                pass

    def send(self, message):
        try:
            self.transport.send(message)
        except (OSError, ValueError) as exc:
            raise SwitchError("Codex control connection closed") from exc

    def _handle(self, message):
        if message is None:
            raise SwitchError("Codex control connection closed")
        if not isinstance(message, dict):
            raise SwitchError("Codex sent an invalid control message")
        if message.get("method") and "id" in message:
            if self.on_request:
                self.on_request(message)
            else:
                self.send({"id": message["id"], "error": {
                    "code": -32601, "message": "Account Hub cannot serve this request"}})

    def call(self, method, params=None, timeout=20):
        self.counter += 1
        request_id = self.counter
        self.send({"id": request_id, "method": method, "params": params or {}})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                message = self.messages.get(timeout=max(.001, deadline - time.monotonic()))
            except queue.Empty:
                break
            self._handle(message)
            if message.get("id") == request_id and not message.get("method"):
                if "error" in message:
                    # Do not echo a server error containing credential values.
                    error = message["error"] or {}
                    code = error.get("code", "unknown")
                    raise SwitchError(f"Codex rejected {method} (RPC {code}); no confirmation")
                return message.get("result") or {}
        raise SwitchError(f"Codex did not confirm {method} before the timeout")

    def poll(self):
        while True:
            try:
                message = self.messages.get_nowait()
            except queue.Empty:
                return
            self._handle(message)

    def close(self):
        self.transport.close()


# ---- usage ----------------------------------------------------------------
#
# Both harnesses publish what is left of the current account's allowance. The
# numbers come from the account, so they hold whatever tool is spending it.
#
# Unofficial endpoints: they can change or vanish, so every read fails soft and
# the picker works without them.

CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_USAGE_BETA = "oauth-2025-04-20"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
USAGE_CACHE = os.path.join(STATE_DIR, "usage-cache.json")
USAGE_TTL_S = _num_env("ACCOUNT_HUB_USAGE_TTL_S", 120.0)
USAGE_TIMEOUT_S = _num_env("ACCOUNT_HUB_USAGE_TIMEOUT_S", 8.0)
# How long to leave an account alone after it answers 429.
USAGE_BACKOFF_S = _num_env("ACCOUNT_HUB_USAGE_BACKOFF_S", 300.0)
BAR_WIDTH = int(_num_env("ACCOUNT_HUB_USAGE_BAR_WIDTH", 10))
_BAR_CHARS = _setting("ACCOUNT_HUB_USAGE_BAR") or "█░"
BAR_FULL = _BAR_CHARS[0]
BAR_EMPTY = _BAR_CHARS[1] if len(_BAR_CHARS) > 1 else " "


def _thresholds():
    raw = _setting("ACCOUNT_HUB_USAGE_THRESHOLDS") or "60,85"
    try:
        warn, crit = (float(x) for x in raw.split(",", 1))
        return warn, crit
    except ValueError:
        return 60.0, 85.0


WARN_AT, CRIT_AT = _thresholds()

# Named colours, resolved to curses constants when the overlay opens and to
# ANSI codes for the one-line form.
COLOR_NAMES = {
    "black": 0, "red": 1, "green": 2, "yellow": 3,
    "blue": 4, "magenta": 5, "cyan": 6, "white": 7,
}


def _color_map():
    out = {"ok": "green", "warn": "yellow", "crit": "red", "stale": "blue"}
    raw = _setting("ACCOUNT_HUB_USAGE_COLORS") or ""
    for part in raw.split(","):
        if "=" in part:
            key, value = part.split("=", 1)
            key, value = key.strip().lower(), value.strip().lower()
            if key in out and value in COLOR_NAMES:
                out[key] = value
    return out


USAGE_COLORS = _color_map()


def severity_of(percent):
    if percent is None:
        return "stale"
    if percent >= CRIT_AT:
        return "crit"
    if percent >= WARN_AT:
        return "warn"
    return "ok"


def bar_for(percent, width=None):
    width = width or BAR_WIDTH
    if percent is None:
        return BAR_EMPTY * width
    filled = int(round(max(0.0, min(100.0, percent)) / 100.0 * width))
    return BAR_FULL * filled + BAR_EMPTY * (width - filled)


def _http_json(url, headers, body=None):
    headers = dict(headers)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, headers=headers, data=data)
    with urllib.request.urlopen(request, timeout=USAGE_TIMEOUT_S) as response:
        return json.load(response)


def _iso_epoch(text):
    if isinstance(text, (int, float)):
        return float(text)
    if not text:
        return None
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _jwt_expiry(token):
    """`exp` out of a JWT — only used to skip a token already known to be dead."""
    try:
        return float(_jwt_claims(token).get("exp"))
    except (TypeError, ValueError):
        return None


def _claude_auth(payload):
    """(token, expires_at) from a claude credential payload."""
    creds = (payload or {}).get("credentials")
    if isinstance(creds, str):
        try:
            creds = json.loads(creds)
        except ValueError:
            creds = None
    oauth = (creds or {}).get("claudeAiOauth") or {}
    token = oauth.get("accessToken")
    expires = oauth.get("expiresAt")
    if isinstance(expires, (int, float)) and expires > 10 ** 11:
        expires = expires / 1000.0  # milliseconds
    return token, expires


def _codex_auth(payload):
    """(token, account_id, expires_at) from a codex auth payload."""
    auth = (payload or {}).get("auth") or {}
    tokens = auth.get("tokens") or {}
    token = tokens.get("access_token")
    return token, tokens.get("account_id"), _jwt_expiry(token or "")


def _scoped_label(entry):
    """"weekly_scoped" says nothing; the scope names the model it applies to."""
    label = str(entry.get("kind") or entry.get("group") or "window")
    scope = entry.get("scope") or {}
    model = (scope.get("model") or {}).get("display_name")
    if not model:
        return label
    return "%s %s" % (label.replace("_scoped", ""), model)


def _claude_windows(body):
    out = []
    for entry in (body or {}).get("limits") or []:
        if not isinstance(entry, dict):
            continue
        out.append({
            "label": _scoped_label(entry),
            "percent": entry.get("percent"),
            "resets_at": _iso_epoch(entry.get("resets_at")),
            "blocked": None,
            # Which window is currently the one that would stop you.
            "binding": bool(entry.get("is_active")),
        })
    if out:
        return out
    for name in ("five_hour", "seven_day"):
        block = (body or {}).get(name)
        if isinstance(block, dict):
            out.append({
                "label": name,
                "percent": block.get("utilization"),
                "resets_at": _iso_epoch(block.get("resets_at")),
                "blocked": None,
                "binding": False,
            })
    return out


def _codex_windows(body):
    rate = (body or {}).get("rate_limit") or {}
    reached = rate.get("limit_reached")
    out = []
    for name in ("primary_window", "secondary_window"):
        window = rate.get(name)
        if not isinstance(window, dict):
            continue
        seconds = window.get("limit_window_seconds")
        hours = (seconds or 0) / 3600.0
        # Name it for what it is, so it reads the same as the claude rows.
        if hours >= 24 * 6:
            label = "weekly"
        elif hours >= 24:
            label = "%gd" % round(hours / 24.0, 1)
        elif hours:
            label = "%gh" % round(hours, 1)
        else:
            label = name.split("_")[0]
        resets = window.get("reset_at")
        if resets is None and window.get("reset_after_seconds") is not None:
            resets = time.time() + window["reset_after_seconds"]
        out.append({
            "label": label,
            "percent": window.get("used_percent"),
            "resets_at": _iso_epoch(resets),
            "seconds": seconds or None,
            # codex says outright whether it is blocked; nothing to infer.
            "blocked": bool(reached) if reached is not None else None,
            "binding": False,
        })
    return out


# ---- token renewal --------------------------------------------------------
#
# A parked profile's access token has usually expired, so its usage cannot be
# read without renewing it first. Renewing SPENDS the stored refresh token: the
# reply carries a fresh pair and the old one dies. Lose that reply and the
# account needs a browser login, so the new pair is written into the profile
# before it is used for anything else. This mirrors what the CLIs themselves do
# on every expiry, and what openusage does to keep its panel current.

CLAUDE_RENEW_URL = "https://platform.claude.com/v1/oauth/token"
CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CODEX_RENEW_URL = "https://auth.openai.com/oauth/token"
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
# Renew this far ahead of expiry rather than after it, so a token cannot die
# mid-read. openusage uses the same five minutes.
RENEW_MARGIN_S = _num_env("ACCOUNT_HUB_RENEW_MARGIN_S", 300.0)
USAGE_RENEW = _flag("ACCOUNT_HUB_USAGE_RENEW", default=True)
# Ask the provider whether a saved login still works before installing it.
VERIFY_SWITCH = _flag("ACCOUNT_HUB_VERIFY_SWITCH", default=True)


# The token endpoint answers the CLI's own HTTP client, and refuses a default
# urllib User-Agent, so the request is shaped like the one the CLI makes.
TOKEN_USER_AGENT = "axios/1.15.2"
TOKEN_ACCEPT = "application/json, text/plain, */*"
# The endpoint is order-sensitive about scopes; this is the order the CLI sends.
CANONICAL_SCOPES = [
    "org:create_api_key", "user:profile", "user:inference",
    "user:sessions:claude_code", "user:mcp_servers", "user:file_upload",
]


def _canonical_scopes(scopes):
    present = [s for s in (scopes or []) if s]
    ordered = [s for s in CANONICAL_SCOPES if s in present]
    ordered += [s for s in present if s not in CANONICAL_SCOPES]
    return " ".join(ordered)


def _post_json(url, body):
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": TOKEN_ACCEPT,
            "User-Agent": TOKEN_USER_AGENT,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=USAGE_TIMEOUT_S) as response:
        return json.load(response)


def _renewed_claude(oauth):
    """A renewed claudeAiOauth block, or None. Spends the stored refresh token."""
    stored = (oauth or {}).get("refreshToken")
    if not stored:
        return None
    scopes = oauth.get("scopes") or []
    body = _post_json(CLAUDE_RENEW_URL, {
        "grant_type": "refresh_token",
        "refresh_token": stored,
        "client_id": CLAUDE_CLIENT_ID,
        "scope": _canonical_scopes(scopes) or _canonical_scopes(CANONICAL_SCOPES[1:]),
    })
    access = body.get("access_token")
    if not access:
        return None
    out = dict(oauth)
    out["accessToken"] = access
    # The reply rotates the refresh token. Keep the old one only when none came
    # back, because dropping a live one strands the account.
    if body.get("refresh_token"):
        out["refreshToken"] = body["refresh_token"]
    if body.get("expires_in"):
        out["expiresAt"] = int((time.time() + float(body["expires_in"])) * 1000)
    return out


def _renewed_codex(tokens):
    """A renewed codex tokens block, or None. Spends the stored refresh token."""
    stored = (tokens or {}).get("refresh_token")
    if not stored:
        return None
    body = _post_json(CODEX_RENEW_URL, {
        "grant_type": "refresh_token",
        "refresh_token": stored,
        "client_id": CODEX_CLIENT_ID,
    })
    access = body.get("access_token")
    if not access:
        return None
    out = dict(tokens)
    out["access_token"] = access
    if body.get("refresh_token"):
        out["refresh_token"] = body["refresh_token"]
    if body.get("id_token"):
        out["id_token"] = body["id_token"]
    return out


def _due_for_renewal(expires_at):
    return expires_at is not None and expires_at - time.time() <= RENEW_MARGIN_S


STAGED_DIR = os.path.join(STATE_DIR, "rotated")


def _staged_path(kind, slug):
    return os.path.join(STAGED_DIR, "%s-%s.json" % (kind, slug))


def _stage(kind, slug, payload):
    """Park a rotated payload on disk the instant it exists.

    Between the endpoint answering and the profile being rewritten, the reply is
    the only usable pair that account has. Writing it here first means a crash
    in that window costs a stale profile, not a login.
    """
    _secure_dir(STAGED_DIR)
    _write_json_secret(_staged_path(kind, slug), payload)


def _adopt_staged(kind, slug, payload):
    """Take up a payload staged by a run that died before it could finish."""
    staged = _read_json(_staged_path(kind, slug))
    return staged if staged else payload


def _clear_staged(kind, slug):
    try:
        os.remove(_staged_path(kind, slug))
    except OSError:
        pass


def renew_profile(kind, profile, force=False):
    """Renew one parked profile's tokens in place; returns its payload or None.

    Ordering is the whole point. Back up before going near the network, stage
    the reply the moment it lands, then rewrite the profile. From the moment the
    endpoint answers, the old refresh token is dead and the reply is the only
    usable pair that account has.

    `force` renews a token the clock still calls valid. Both providers can
    supersede a session — signing into another account on the same provider does
    it — and the old access token keeps claiming a future expiry regardless. The
    server is the authority, so a 401 asks for this.
    """
    with _Lock():
        current = _profile_by_id(kind, profile["id"])
        slug = current["id"]
        # A legacy sidecar can be the only surviving rotated token. Adopt it
        # on this record's normal renewal; do not bulk-migrate credential files.
        original = current.get("payload") or {}
        if current["slug"] != slug:
            original = _adopt_staged(kind, current["slug"], original)
        payload = _adopt_staged(kind, slug, original)
        if kind == "claude":
            creds = payload.get("credentials")
            if isinstance(creds, str):
                try:
                    creds = json.loads(creds)
                except ValueError:
                    return None
            if not force and not _due_for_renewal(_claude_auth(payload)[1]):
                return payload
            _backup(kind, payload)
            renewed = _renewed_claude((creds or {}).get("claudeAiOauth") or {})
            if not renewed:
                return None
            creds = dict(creds or {})
            creds["claudeAiOauth"] = renewed
            payload = dict(payload)
            payload["credentials"] = creds
        elif kind == "codex":
            auth = dict(payload.get("auth") or {})
            tokens = auth.get("tokens") or {}
            if not force and not _due_for_renewal(
                    _jwt_expiry(tokens.get("access_token") or "")):
                return payload
            _backup(kind, payload)
            renewed = _renewed_codex(tokens)
            if not renewed:
                return None
            auth["tokens"] = renewed
            auth["last_refresh"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            payload = dict(payload)
            payload["auth"] = auth
        else:
            return None
        _stage(kind, slug, payload)
        current = dict(current)
        current["payload"] = payload
        # The credential changed, which is what saved_at records. Leaving it
        # still made a repaired login look untouched to anything keyed on it.
        current["saved_at"] = int(time.time())
        write_profile(current)
        _clear_staged(kind, slug)
        if current["slug"] != slug:
            _clear_staged(kind, current["slug"])
    return payload


class UsageWindows(list):
    """Usage windows plus optional earned-reset metadata from the same read.

    Remains a list for the existing usage consumers; cache writers retain the
    summary without a second request or a separate polling engine.
    """
    def __init__(self, windows, reset_credits):
        super().__init__(windows)
        self.reset_credits = reset_credits


def _reset_summary(body):
    count = body.get("available_count") if isinstance(body, dict) else None
    if type(count) is not int or count < 0:
        return None
    return {"available_count": count}


def _codex_usage_reading(body):
    return UsageWindows(_codex_windows(body),
                        _reset_summary((body or {}).get("rate_limit_reset_credits")))


def fetch_usage(kind, payload):
    """Windows for one account, or None when it cannot be read."""
    if kind == "claude":
        token, expires = _claude_auth(payload)
        if not token or (expires and expires < time.time()):
            return None
        return _claude_windows(_http_json(CLAUDE_USAGE_URL, {
            "Authorization": "Bearer %s" % token,
            "anthropic-beta": CLAUDE_USAGE_BETA,
        }))
    if kind == "codex":
        token, account_id, expires = _codex_auth(payload)
        if not token or (expires and expires < time.time()):
            return None
        return _codex_usage_reading(_http_json(CODEX_USAGE_URL, {
            "Authorization": "Bearer %s" % token,
            "chatgpt-account-id": account_id or "",
            "User-Agent": "codex-cli",
            "Accept": "application/json",
        }))
    return None


def _auth_error_code(exc):
    """Return a known provider code only; never display its raw error message."""
    if hasattr(exc, "_account_hub_error_code"):
        return exc._account_hub_error_code
    known = {"invalid_grant", "refresh_token_reused", "refresh_token_invalidated",
             "refresh_token_expired", "token_revoked", "token_invalidated",
             "invalid_token", "token_expired"}
    code = None
    try:
        body = json.loads(exc.read(16384).decode() or "{}")
        if isinstance(body, dict):
            error = body.get("error")
            values = ([error.get("code"), error.get("type")] if isinstance(error, dict)
                      else [error]) + [body.get("code"), body.get("error_description")]
            code = next((value for value in values if isinstance(value, str) and value in known), None)
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    exc._account_hub_error_code = code
    return code


def _refresh_refused(exc):
    """True when a token endpoint turned a refresh token down for good.

    The definitive answers are listed once, in
    docs/solutions/runtime-errors/saved-login-revoked-outside-the-plugin.md —
    keep that table and this function in step. Anything vaguer than those is not
    proof that the account is dead.
    """
    if exc.code not in (400, 401):
        return False
    if exc.code == 401:
        return True
    return _auth_error_code(exc) in {"invalid_grant", "refresh_token_reused",
                                    "refresh_token_invalidated", "refresh_token_expired"}


def proven_payload(kind, profile):
    """The profile's payload, proven still usable, renewed if it had to be.

    switch() checks that the credential store took the write. That says nothing
    about whether the login still works, and the two come apart exactly when it
    matters: a snapshot the provider has since retired installs cleanly,
    reports success, and signs you out. So ask the provider first, while the
    working login is still in place.

    Only a definitive refusal stops a switch — the provider answering 401 to a
    token it was just handed, or turning the refresh token down in words. Being
    offline, timing out, or getting rate limited proves nothing either way, and
    none of them block the switch.

    Returns the payload to install. It is not always the one that came in:
    renewing rotates the tokens, and the caller must write *this* one back, or
    it parks a spent pair over a live one.
    """
    payload = profile.get("payload") or {}
    if not VERIFY_SWITCH:
        return payload
    label = profile.get("label") or profile.get("slug") or kind
    key = "%s:%s" % (kind, profile["id"])
    try:
        payload = renew_profile(kind, profile) or payload
        try:
            windows = fetch_usage(kind, payload)
        except urllib.error.HTTPError as exc:
            if exc.code != 401:
                raise
            # 401 on a token the clock still calls valid: the session was
            # superseded. Renew past the clock and ask once more, because the
            # server is the authority and the expiry is only a claim.
            renewed = renew_profile(kind, profile, force=True)
            if not renewed:
                raise
            payload = renewed
            windows = fetch_usage(kind, payload)
        if windows:
            _remember_usage(key, windows)
            _clear_login_problem(key)
    except urllib.error.HTTPError as exc:
        if exc.code == 401 or _refresh_refused(exc):
            if kind == "codex":
                reason = {
                    "refresh_token_reused": "saved refresh token already used",
                    "refresh_token_invalidated": "saved login revoked",
                    "token_invalidated": "saved login revoked",
                    "token_revoked": "saved login revoked",
                    "refresh_token_expired": "saved login expired",
                    "invalid_grant": "saved login no longer accepted",
                }.get(_auth_error_code(exc), "saved login not accepted")
                _record_login_problem(key, reason, _profile_by_id(kind, profile["id"])["payload"])
                raise SwitchError(
                    f"Codex: {label}: {reason} — active account unchanged; "
                    "press a to sign in to this registration again"
                ) from None
            _record_login_problem(key, "saved login not accepted",
                                  _profile_by_id(kind, profile["id"])["payload"])
            raise SwitchError(
                "%s: %s was refused — nothing changed; log that account in "
                "again and save it" % (kind, label)
            )
        if exc.code == 429:
            _rest_usage(key, _usage_retry_delay(exc))
        return payload
    except Exception:
        return payload
    return payload


def _usage_cache():
    return _read_json(USAGE_CACHE) or {}


def _remember_usage(key, windows):
    cache = _usage_cache()
    entry = cache.get(key) or {}
    now = time.time()
    if isinstance(windows, UsageWindows):
        entry.update(reset_credits=windows.reset_credits, reset_credits_at=now)
    # Stamp each window with the moment it was read. A rate worked out from a
    # cached reading has to be measured against that moment, not against a clock
    # that has kept running since — see _spend_rate.
    windows = [dict(w, read_at=now) if isinstance(w, dict) else w
               for w in (windows or [])]
    entry.update({"at": now, "windows": windows})
    entry.pop("retry_after", None)
    entry.pop("problem", None)
    cache[key] = entry
    _secure_dir(STATE_DIR)
    _write_json_secret(USAGE_CACHE, cache)


def _rest_usage(key, seconds):
    """Stop asking for a while. Keeps whatever windows were last read."""
    cache = _usage_cache()
    entry = cache.get(key) or {}
    entry["retry_after"] = time.time() + seconds
    entry["problem"] = "rate limited"
    cache[key] = entry
    _secure_dir(STATE_DIR)
    _write_json_secret(USAGE_CACHE, cache)


def _usage_problem(key, problem):
    """Keep a failed read visible when another UI tick reloads the cache."""
    cache = _usage_cache()
    entry = cache.get(key) or {}
    entry["problem"] = problem
    cache[key] = entry
    _secure_dir(STATE_DIR)
    _write_json_secret(USAGE_CACHE, cache)


def _record_login_problem(key, reason, payload):
    """A refused saved grant stays flagged even if another live grant reads usage."""
    cache = _usage_cache()
    entry = cache.get(key) or {}
    entry.update(login_problem=reason, login_payload_hash=_codex_auth_hash(payload))
    cache[key] = entry
    _secure_dir(STATE_DIR)
    _write_json_secret(USAGE_CACHE, cache)


def _clear_login_problem(key):
    cache = _usage_cache()
    entry = cache.get(key) or {}
    if "login_problem" in entry:
        entry.pop("login_problem", None)
        entry.pop("login_payload_hash", None)
        cache[key] = entry
        _write_json_secret(USAGE_CACHE, cache)


def _usage_retry_delay(exc):
    """Respect the reporting endpoint's cooldown, with a safe fallback."""
    raw = (exc.headers or {}).get("Retry-After", "")
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        try:
            seconds = parsedate_to_datetime(raw).timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            return USAGE_BACKOFF_S
    return seconds if 0 < seconds < float("inf") else USAGE_BACKOFF_S


def usage_rows(kinds=None, refresh=True, force=False):
    """Serialize cache checks and requests across picker/panel processes.

    Taking the lock before reading the cache means a waiting reader observes
    the first reader's result or cooldown instead of repeating its request.
    The same lock protects token renewal and switching, and is re-entrant.
    """
    with _Lock():
        return _usage_rows(kinds, refresh, force)


def _usage_rows(kinds=None, refresh=True, force=False):
    """One row per saved profile: its windows, and how current they are.

    A parked profile usually holds an expired token, so its numbers come from
    the last time it was live. That is said out loud rather than hidden: the
    row carries `age`, and `state` is live, cached or unknown.
    """
    rows = []
    cache = _usage_cache()
    for kind in kinds or KIND_ORDER:
        backend = BACKENDS[kind]
        live = backend.read_live()
        # The live tokens are already in hand here, and this runs on every
        # picker, panel and `list`, so it is where the saved copy stays current
        # without waiting for a switch.
        sync_live_profile(kind, live)
        current = active_profile(kind, live) if live else None
        live_identity = _fingerprint(backend.identity(live)) if live else None
        for profile in list_profiles(kind):
            key = "%s:%s" % (kind, profile["id"])
            is_live = current is not None and profile["id"] == current["id"]
            # Fresh CLI sign-in can change the grant without changing the
            # person/workspace. Read that account's usage with its live token,
            # but never overwrite or select a registration just by identity.
            from_live = bool(live_identity and live_identity == _fingerprint(profile["identity"]))
            payload = live if from_live else profile.get("payload")
            windows, state = None, "unknown"
            entry = cache.get(key) or {}
            login_problem = (entry.get("login_problem")
                             if entry.get("login_payload_hash") == _codex_auth_hash(profile.get("payload"))
                             else None)
            problem = entry.get("problem")
            # The endpoint rate-limits, so the live account observes the cache
            # too. It used to refetch on every call, which is what earns a 429.
            stale = (time.time() - (entry.get("at") or 0)) > USAGE_TTL_S
            follow_up = entry.get("reset_usage_refresh") or {}
            follow_up_due = bool(follow_up and time.time() >= follow_up["after"])
            resting = time.time() < (entry.get("retry_after") or 0)
            if resting:
                problem = "rate limited"
            attempted_read = refresh and (stale or force or follow_up_due) and not resting
            if attempted_read:
                problem = None
                try:
                    # A parked account's token has usually expired, so renewing
                    # is what makes its numbers readable at all. The live one is
                    # kept fresh by the CLI, so that chain is left alone.
                    if USAGE_RENEW and not from_live:
                        payload = renew_profile(kind, profile) or payload
                    try:
                        windows = fetch_usage(kind, payload)
                    except urllib.error.HTTPError as exc:
                        # 401 on a token the clock still calls valid: the session
                        # was superseded, which signing into another account on
                        # the same provider does. Renew past the clock and retry
                        # once — the server is the authority, not the expiry.
                        if exc.code != 401 or from_live or not USAGE_RENEW:
                            raise
                        payload = renew_profile(kind, profile, force=True)
                        if not payload:
                            raise
                        windows = fetch_usage(kind, payload)
                except Exception as exc:
                    # Say why, briefly: this shares a narrow column with the
                    # account name, and a truncated traceback tells nobody
                    # anything.
                    code = getattr(exc, "code", None)
                    problem = {401: "needs re-login", 403: "refused",
                               404: "no usage endpoint"}.get(code)
                    if code == 429:
                        # Asked too often. Sit out, or every later call compounds it.
                        problem = "rate limited"
                        _rest_usage(key, _usage_retry_delay(exc))
                    elif problem is None:
                        problem = "unreachable" if isinstance(
                            exc, urllib.error.URLError) else type(exc).__name__
                    _usage_problem(key, problem)
                    windows = None
            if windows is not None:
                state = "live"
                _remember_usage(key, windows)
                if not from_live or payload == profile.get("payload"):
                    _clear_login_problem(key)
                    login_problem = None
                problem = None
                at = time.time()
            elif entry.get("windows"):
                windows, state, at = entry["windows"], "cached", entry.get("at")
            else:
                windows, at = [], None
            if attempted_read and follow_up_due:
                _advance_reset_usage_refresh(key)
            reset_entry = _usage_cache().get(key) or {}
            next_read = (reset_entry.get("reset_usage_refresh") or {}).get("after")
            rows.append({
                "kind": kind,
                "id": profile["id"],
                "slug": profile["slug"],
                "label": profile["label"],
                "active": is_live,
                "state": state,
                "at": at,
                "age": (time.time() - at) if at else None,
                "windows": windows,
                "problem": login_problem or problem,
                "reset_credits": reset_entry.get("reset_credits"),
                "reset_credits_at": reset_entry.get("reset_credits_at"),
                "refresh_after": max(next_read, reset_entry.get("retry_after") or 0)
                                 if next_read is not None else None,
            })
    return rows


# ---- earned Codex resets ---------------------------------------------------
# Use the same per-account HTTP/auth/cache path as usage. These routes and wire
# fields are those of the official Codex backend-client (rust-v0.162.0), behind
# account/rateLimits/read and account/rateLimitResetCredit/consume. No shared
# daemon login or machine account switch is needed to redeem a parked account.
CODEX_RESETS_URL = "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits"
RESET_ATTEMPTS = os.path.join(STATE_DIR, "reset-attempts.json")
RESET_USAGE_REFRESH_DELAYS_S = (2, 5, 10)


def _queue_reset_usage_refresh(key):
    cache = _usage_cache()
    entry = cache.setdefault(key, {})
    entry["reset_usage_refresh"] = {
        "after": time.time() + RESET_USAGE_REFRESH_DELAYS_S[0],
        "remaining_delays": list(RESET_USAGE_REFRESH_DELAYS_S[1:]),
    }
    _write_json_secret(USAGE_CACHE, cache)


def _advance_reset_usage_refresh(key):
    """Bound follow-up reads; cache-only calls never advance or query anything."""
    cache = _usage_cache()
    entry = cache.get(key) or {}
    follow_up = entry.get("reset_usage_refresh")
    if not follow_up:
        return
    delays = follow_up["remaining_delays"]
    if delays:
        follow_up["after"] = time.time() + delays.pop(0)
    else:
        entry.pop("reset_usage_refresh")
    _write_json_secret(USAGE_CACHE, cache)


def _reset_request(profile, url, body=None):
    """Authenticated request for exactly this registration; no account fallback."""
    backend = BACKENDS["codex"]
    live = backend.read_live()
    sync_live_profile("codex", live)
    profile = _profile_by_id("codex", profile["id"])
    identity = backend.identity(profile["payload"])
    from_live = bool(live and (identity.get("user_id") or identity.get("email"))
                     and _fingerprint(identity) == _fingerprint(backend.identity(live)))
    payload = live if from_live else profile["payload"]
    key = "codex:" + profile["id"]
    try:
        if not from_live and USAGE_RENEW:
            payload = renew_profile("codex", profile) or payload
        for attempt in range(2):
            token, account_id, expiry = _codex_auth(payload)
            if not token or not account_id:
                raise SwitchError("Earned resets require a ChatGPT Codex login")
            if expiry and expiry <= time.time():
                raise SwitchError("Codex login expired; press 'a' to sign in again")
            try:
                headers = {
                    "Authorization": "Bearer " + token,
                    "chatgpt-account-id": account_id,
                    "User-Agent": "codex-cli", "Accept": "application/json",
                }
                return (_http_json(url, headers, body) if body is not None
                        else _http_json(url, headers))
            except urllib.error.HTTPError as exc:
                if exc.code != 401 or from_live or not USAGE_RENEW or attempt:
                    raise
                renewed = renew_profile("codex", profile, force=True)
                if not renewed:
                    raise
                payload = renewed
    except urllib.error.HTTPError as exc:
        if exc.code == 401 or _refresh_refused(exc):
            saved = _profile_by_id("codex", profile["id"])["payload"]
            if not from_live or payload == saved:
                _record_login_problem(key, "saved login not accepted", saved)
            raise SwitchError("Codex sign-in required; press 'a' to sign in again") from None
        if exc.code == 429:
            _rest_usage(key, _usage_retry_delay(exc))
        raise SwitchError(f"Reset request refused (HTTP {exc.code}); press 't' to retry") from None
    except (OSError, ValueError) as exc:
        raise SwitchError("Reset service unreachable; press 't' to retry") from exc


def _cache_resets(profile_id, summary, problem=None):
    cache = _usage_cache()
    entry = cache.setdefault("codex:" + profile_id, {})
    entry.update(reset_credits=summary, reset_credits_at=time.time())
    if problem:
        entry["problem"] = problem
    _write_json_secret(USAGE_CACHE, cache)


def prepare_reset(kind, profile_id):
    """Read before confirmation; choose earliest expiry, random among ties.

    A pending redemption survives process exit and takes priority over any
    fresh credit: retry it with its original key, never spend another by guess.
    """
    if kind != "codex":
        raise SwitchError("Earned resets are available for Codex accounts only")
    with _Lock():
        profile = _profile_by_id(kind, profile_id)
        fingerprint = _fingerprint(BACKENDS[kind].identity(profile["payload"]))
        previous = (_read_json(RESET_ATTEMPTS) or {}).get(profile_id) or {}
        if previous.get("status") == "pending":
            if previous.get("fingerprint") != fingerprint:
                raise SwitchError("An unconfirmed reset belongs to a different login")
            return previous
        body = _reset_request(profile, CODEX_RESETS_URL)
        summary = _reset_summary(body)
        _cache_resets(profile_id, summary)
        if summary is None:
            raise SwitchError("Reset availability unknown; press 't' to retry")
        if not summary["available_count"]:
            raise SwitchError("No resets available for this account.")
        credits = body.get("credits")
        if not isinstance(credits, list):
            raise SwitchError("Reset expiry details unavailable; press 't' to retry")
        # The provider may cap detail rows. Do not claim to choose the earliest
        # expiry when some credits have no returned details.
        ids = {c.get("id") for c in credits if isinstance(c, dict)
               and isinstance(c.get("id"), str) and c["id"]}
        if len(ids) < summary["available_count"]:
            raise SwitchError("Reset details incomplete; cannot determine earliest expiry")
        available = {}
        for credit in credits:
            if (not isinstance(credit, dict) or not isinstance(credit.get("id"), str)
                    or credit["id"] not in ids
                    or credit.get("reset_type") != "codex_rate_limits"
                    or credit.get("status") != "available"):
                continue
            raw_expiry = credit.get("expires_at")
            expiry = _iso_epoch(raw_expiry)
            if raw_expiry is not None and (expiry is None or not float('-inf') < expiry < float('inf')):
                raise SwitchError("Reset expiry unavailable; press 't' to retry")
            if expiry is not None and expiry <= time.time():
                continue
            available[credit["id"]] = {"id": credit["id"], "expires_at": expiry}
        if not available:
            raise SwitchError("No resets available for this account.")
        nearest = min(c["expires_at"] if c["expires_at"] is not None else float("inf")
                      for c in available.values())
        chosen = random.choice([c for c in available.values()
                                if (c["expires_at"] if c["expires_at"] is not None
                                    else float("inf")) == nearest])
        return {"profile_id": profile_id, "fingerprint": fingerprint,
                "credit": chosen, "request_id": str(uuid.uuid4()),
                "previous_id": previous.get("request_id")}


def consume_reset(attempt):
    """Redeem the confirmed credit, journal first, then refresh provider values."""
    with _Lock():
        profile_id = attempt["profile_id"]
        profile = _profile_by_id("codex", profile_id)
        if attempt["fingerprint"] != _fingerprint(BACKENDS["codex"].identity(profile["payload"])):
            raise SwitchError("Account changed; press 't' to check resets again")
        journal = _read_json(RESET_ATTEMPTS) or {}
        previous = journal.get(profile_id) or {}
        same = previous.get("request_id") == attempt["request_id"]
        if same and previous.get("status") == "resolved":
            return _reset_result(previous["outcome"])
        key = "codex:" + profile_id
        if not same:
            if previous.get("request_id") != attempt.get("previous_id"):
                raise SwitchError("Another reset attempt changed this account; press 't' again")
            expiry = attempt["credit"]["expires_at"]
            if expiry is not None and expiry <= time.time():
                raise SwitchError("Reset expired during confirmation; press 't' to check again")
            journal[profile_id] = dict(attempt, status="pending")
            # Disk failure here prevents the outbound request. An unknown
            # response retains this same key across retry/restart/reauth.
            _write_json_secret(RESET_ATTEMPTS, journal)
        try:
            response = _reset_request(profile, CODEX_RESETS_URL + "/consume", {
                "credit_id": attempt["credit"]["id"],
                "redeem_request_id": attempt["request_id"],
            })
            outcome = response.get("code")
            if outcome not in ("reset", "already_redeemed", "nothing_to_reset", "no_credit"):
                raise SwitchError("Unexpected reset service response")
        except Exception as exc:
            _cache_resets(profile_id, None, "reset result unconfirmed")
            detail = str(exc) if isinstance(exc, SwitchError) else "Reset service unreachable"
            raise SwitchError("Reset not confirmed; press 't' to retry the same reset. " + detail) from None
        journal[profile_id].update(status="resolved", outcome=outcome)
        try:
            _write_json_secret(RESET_ATTEMPTS, journal)
        except OSError:
            return _reset_result(outcome) + "; receipt not saved; press 't' to confirm the same reset"
        # Never decrement counts or zero percentages speculatively. Invalidate
        # old values and read the account again, as the official API prescribes.
        try:
            if outcome in ("reset", "already_redeemed"):
                # Usage windows can publish independently. Recheck a bounded
                # number of times even if one window has already changed.
                _queue_reset_usage_refresh(key)
            _cache_resets(profile_id, None, "usage refresh required")
            reading = _codex_usage_reading(_reset_request(profile, CODEX_USAGE_URL))
            _remember_usage(key, reading)
        except Exception:
            if (_usage_cache().get(key) or {}).get("reset_usage_refresh"):
                return _reset_result(outcome) + "; usage will refresh automatically"
            return _reset_result(outcome) + "; press 'r' to refresh usage"
        return _reset_result(outcome)


def _reset_result(outcome):
    return {"reset": "Reset applied", "already_redeemed": "Reset already applied",
            "nothing_to_reset": "No eligible usage window to reset; no reset used",
            "no_credit": "No resets available; no reset used"}[outcome]


def _left(seconds):
    if seconds is None:
        return "—"
    seconds = int(seconds)
    if seconds <= 0:
        return "now"
    days, rest = divmod(seconds, 86400)
    hours, minutes = divmod(rest // 60, 60)
    if days:
        return "%dd%02dh" % (days, hours)
    if hours:
        return "%dh%02d" % (hours, minutes)
    return "%dm" % minutes


def _age(seconds):
    if seconds is None:
        return "never read"
    if seconds < 60:
        return "just now"
    return "%s ago" % _left(seconds)


# ---- credential synchronization -----------------------------------------

def sync_live_profile(kind, live=None):
    """Sync token rotation only into the selected UUID. Never discover or
    replace a record by provider id, workspace or email. Fresh Codex sessions
    require explicit reauth; registration always appends a new record.
    """
    backend = BACKENDS.get(kind)
    if backend is None:
        return False
    if live is None:
        try:
            live = backend.read_live()
        except Exception:
            return False
    if live is None:
        return False
    with _Lock():
        profile = active_profile(kind, live)
        if profile is None or profile.get("payload") == live:
            return False
        profile["payload"] = live
        profile["identity"] = backend.identity(live)
        profile["fingerprint"] = _fingerprint(profile["identity"])
        profile["saved_at"] = int(time.time())
        write_profile(profile)
    return True


# ---- kind resolution ------------------------------------------------------

def _context_pane():
    for env in ("HERDR_ACTIVE_PANE_ID", "HERDR_PANE_ID"):
        if os.environ.get(env):
            return os.environ[env]
    raw = os.environ.get("HERDR_PLUGIN_CONTEXT_JSON")
    if raw:
        try:
            ctx = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return ctx.get("focused_pane_id") or ctx.get("pane_id")
    return None


def resolve_kind(explicit=None):
    """Which agent kind an argument-less action should act on: the focused
    agent's kind, else the one kind that actually has profiles to cycle."""
    if explicit:
        if explicit not in BACKENDS:
            raise SwitchError(f"unknown agent kind {explicit!r} (claude|codex)")
        return explicit
    agents = live_agents() or {}
    pane = _context_pane()
    if pane and pane in agents:
        kind = kind_of_agent(agents[pane])
        if kind:
            return kind
    for info in agents.values():
        if info.get("focused"):
            kind = kind_of_agent(info)
            if kind:
                return kind
    switchable = [k for k in KIND_ORDER if len(list_profiles(k)) > 1]
    if len(switchable) == 1:
        return switchable[0]
    saved = [k for k in KIND_ORDER if list_profiles(k)]
    if len(saved) == 1:
        return saved[0]
    raise SwitchError(
        "can't tell which agent to switch — focus a claude/codex pane, or "
        "pass the kind (switcher.py next claude)"
    )


# ---- commands -------------------------------------------------------------

def cmd_open(argv):
    res = herdr(
        "plugin", "pane", "open",
        "--plugin", PLUGIN_ID,
        "--entrypoint", "picker",
        "--placement", "overlay",
    )
    if res.returncode != 0:
        sys.stderr.write(res.stderr or "account-hub: failed to open picker\n")
    return res.returncode


def cmd_next(argv):
    kind = resolve_kind(argv[0] if argv else None)
    print(next_profile(kind))
    return 0


def cmd_switch(argv):
    if len(argv) < 2:
        raise SwitchError("usage: switcher.py switch <claude|codex> <uuid|unique-label>")
    kind = resolve_kind(argv[0])
    print(switch(kind, argv[1]))
    return 0


def save_live(kind, label=None):
    """Save a live account only when it is absent; never reauthenticate it."""
    backend = BACKENDS[kind]
    with _Lock():
        live = backend.read_live()
        if live is None:
            raise SwitchError(f"{kind}: nothing logged in to save")
        if _live_account_saved(kind, live):
            return "Account already saved"
        identity = backend.identity(live)
        profile = make_profile(kind, label or default_label(kind, identity), live)
        write_profile(profile)
        _select_profile(kind, profile["id"])
    return (f"{kind}: saved {profile['label']} [{profile['id']}] "
            f"({backend.describe(profile['identity'])})")


def reauthenticate(kind, profile_id):
    """Capture an official CLI login into exactly the UUID requested."""
    backend = BACKENDS[kind]
    with _Lock():
        profile = _profile_by_id(kind, profile_id)
        live = backend.read_live()
        if live is None:
            raise SwitchError(f"{kind}: nothing logged in to capture; log in with the CLI first")
        profile["payload"] = live
        profile["identity"] = backend.identity(live)
        profile["fingerprint"] = _fingerprint(profile["identity"])
        profile["saved_at"] = int(time.time())
        write_profile(profile)
        _clear_staged(kind, profile_id)
        if profile["slug"] != profile_id:
            _clear_staged(kind, profile["slug"])
        _forget_profile_usage(kind, profile_id)
        _select_profile(kind, profile_id)
    return f"{kind}: reauthenticated {profile['label']} [{profile_id}]"


def cmd_reauth(argv):
    if len(argv) != 2:
        raise SwitchError("usage: switcher.py reauth <claude|codex> <uuid>")
    print(reauthenticate(resolve_kind(argv[0]), argv[1]))
    return 0


LOGIN_TIMEOUT_S = 15 * 60


def _run_official_login(kind, argv, environment, directory):
    """Wait for the official CLI to finish, with bounded cancellation/timeout."""
    title = BACKENDS[kind].short
    try:
        result = subprocess.run(argv, env=environment, cwd=directory, check=False,
                                timeout=LOGIN_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise SwitchError(f"{title} sign-in timed out after 15 minutes; current account unchanged") from None
    except KeyboardInterrupt:
        raise SwitchError(f"{title} sign-in cancelled; current account unchanged") from None
    except OSError:
        raise SwitchError(f"Could not start {title} sign-in; check that its CLI is installed") from None
    if result.returncode:
        raise SwitchError(f"{title} sign-in did not complete; current account unchanged")


def login_codex(profile_id=None, label=None, browser=False):
    """Official sign-in in an empty, temporary credential store.

    `codex login` revokes an existing login before starting. Never seed the
    temporary home with saved/current auth, and never launch chats in it.
    Only the resulting credentials enter this registration's own profile.
    """
    before = _profile_by_id("codex", profile_id) if profile_id else None
    _secure_dir(STATE_DIR)
    with tempfile.TemporaryDirectory(prefix="codex-login-", dir=STATE_DIR) as directory:
        environment = _codex_environment()
        environment["CODEX_HOME"] = directory
        environment["CODEX_SQLITE_HOME"] = directory
        argv = [CODEX, "login", "-c", 'cli_auth_credentials_store="file"']
        if not browser:
            argv.append("--device-auth")
        _run_official_login("codex", argv, environment, directory)
        auth = _read_json(os.path.join(directory, "auth.json"))
        if not isinstance(auth, dict) or not (auth.get("tokens") or {}).get("access_token"):
            raise SwitchError("Codex did not save a ChatGPT login; current account unchanged")
        payload = {"auth": auth}
        identity = BACKENDS["codex"].identity(payload)
        if not identity.get("user_id") or not identity.get("account_id"):
            raise SwitchError("Cannot identify the signed-in account; registration unchanged")
        if before and any(before["identity"].get(key) != identity.get(key)
                          for key in ("user_id", "account_id")):
            raise SwitchError("Signed in to another person or workspace; selected registration unchanged")
        with _Lock():
            if before:
                profile = _profile_by_id("codex", profile_id)
                if profile.get("payload") != before.get("payload"):
                    raise SwitchError("Registration changed during sign-in; retry after checking it")
                _backup("codex", profile["payload"])
                profile.update(payload=payload, identity=identity,
                               fingerprint=_fingerprint(identity), saved_at=int(time.time()))
                write_profile(profile)
                _clear_staged("codex", profile_id)
                if profile["slug"] != profile_id:
                    _clear_staged("codex", profile["slug"])
                _forget_profile_usage("codex", profile_id)
            else:
                profile = make_profile("codex", label or default_label("codex", identity), payload)
                write_profile(profile)
    return f"Codex: saved login for {profile['label']}; Enter applies it to this machine"


def login_claude(label=None):
    """Beta: collect a claude.ai login in an empty file-backed config folder."""
    # The CLI scopes Keychain entries to CLAUDE_CONFIG_DIR on macOS. Do not read
    # the shared service or guess the CLI's private service-name derivation.
    if IS_MAC:
        raise SwitchError("Automatic Claude sign-in is not supported on macOS yet (beta); use s to save an existing login")
    _secure_dir(STATE_DIR)
    with tempfile.TemporaryDirectory(prefix="claude-login-", dir=STATE_DIR) as directory:
        environment = os.environ.copy()
        environment["CLAUDE_CONFIG_DIR"] = directory
        for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
            environment.pop(name, None)
        argv = [_setting("ACCOUNT_HUB_CLAUDE_BIN") or "claude", "auth", "login", "--claudeai"]
        _run_official_login("claude", argv, environment, directory)
        credentials = _read_json(os.path.join(directory, ".credentials.json"))
        settings = _read_json(os.path.join(directory, ".claude.json"))
        oauth = credentials.get("claudeAiOauth") if isinstance(credentials, dict) else None
        account = settings.get("oauthAccount") if isinstance(settings, dict) else None
        if not isinstance(oauth, dict) or not oauth.get("accessToken"):
            raise SwitchError("Claude did not save a subscription login; current account unchanged")
        if not isinstance(account, dict):
            raise SwitchError("Cannot identify the signed-in Claude account; registration unchanged")
        payload = {"store": "file", "credentials": credentials,
                   "oauth_account": account}
        identity = BACKENDS["claude"].identity(payload)
        if not identity.get("account_id") or not identity.get("email"):
            raise SwitchError("Cannot identify the signed-in Claude account; registration unchanged")
        with _Lock():
            profile = make_profile("claude", label or default_label("claude", identity), payload)
            write_profile(profile)
    return f"Claude: saved login for {profile['label']}; Enter applies it"


def cmd_codex_login(argv):
    browser = "--browser" in argv
    args = [arg for arg in argv if arg != "--browser"]
    if args and args[0] == "--label" and len(args) == 2:
        message = login_codex(label=args[1], browser=browser)
    elif len(args) <= 1:
        message = login_codex(profile_id=args[0] if args else None, browser=browser)
    else:
        raise SwitchError("usage: switcher.py codex-login [profile-uuid | --label name] [--browser]")
    print(message)
    return 0


def cmd_save(argv):
    if argv:
        kind = resolve_kind(argv[0])
        print(save_live(kind, argv[1] if len(argv) > 1 else None))
        return 0
    saved_any = False
    for kind in KIND_ORDER:
        if not BACKENDS[kind].present():
            continue
        try:
            print(save_live(kind))
            saved_any = True
        except SwitchError as exc:
            print(exc, file=sys.stderr)
    if not saved_any:
        return 1
    return 0


def cmd_status(argv):
    for kind in KIND_ORDER:
        backend = BACKENDS[kind]
        profiles = list_profiles(kind)
        if not backend.present() and not profiles:
            continue
        live = backend.read_live()
        active = active_profile(kind, live) if live else None
        if live is None:
            current = "not logged in"
        else:
            desc = backend.describe(backend.identity(live))
            current = f"{active['label']} ({desc})" if active else f"unsaved ({desc})"
        if kind == "codex":
            current += "; " + codex_application_status()
        print(f"{backend.title:<12} {current}")
        for p in profiles:
            mark = "*" if active is not None and p["id"] == active["id"] else " "
            print(f"  {mark} {p['label']:<16} [{p['id']}] {backend.describe(p.get('identity') or {})}")
        if not profiles:
            print("    (no profiles saved — run the save action)")
    return 0


def cmd_list(argv):
    """Profiles as JSON, for another plugin to read. Never prints credentials.

    Each row carries what that account has left, so a caller can choose the
    account that frees up soonest rather than the one saved first. The numbers
    come off the cache alone: this is a poll-loop entry point, and fetching
    here would rate-limit the account for every reader.
    """
    kinds = KIND_ORDER
    if "--kind" in argv:
        wanted = argv[argv.index("--kind") + 1]
        kinds = [wanted] if wanted in BACKENDS else []
    if not kinds:
        print(json.dumps([]))
        return 0
    profiles = {(kind, p["id"]): p
                for kind in kinds for p in list_profiles(kind)}
    out = []
    # usage_rows walks the same kinds and profiles in the same order, and works
    # out which one is live once per kind, so it is the row source rather than
    # a second pass beside one.
    for row in usage_rows(kinds, refresh=False):
        profile = profiles.get((row["kind"], row["id"])) or {}
        out.append({
            "kind": row["kind"],
            "id": row["id"],
            "slug": row["slug"],
            "label": row["label"],
            "active": row["active"],
            "active_source": "credential_store",
            "saved_at": profile.get("saved_at"),
            "last_used": profile.get("last_used"),
            "tier": (profile.get("identity") or {}).get("subscription_type"),
            "state": row["state"],
            "at": row["at"],
            "age": row["age"],
            "windows": row["windows"],
            "problem": row["problem"],
        })
    print(json.dumps(out))
    return 0


def _usage_pairs(curses):
    """Severity name -> curses attribute, from the configured palette."""
    pairs = {}
    if not curses.has_colors():
        return pairs
    curses.start_color()
    try:
        curses.use_default_colors()
    except curses.error:
        pass
    for index, (name, colour) in enumerate(USAGE_COLORS.items(), start=1):
        try:
            curses.init_pair(index, COLOR_NAMES[colour], -1)
            pairs[name] = curses.color_pair(index)
        except curses.error:
            pairs[name] = 0
    try:
        # Account failures are always red, independently of the usage palette.
        index = len(USAGE_COLORS) + 1
        curses.init_pair(index, curses.COLOR_RED, -1)
        pairs["error"] = curses.color_pair(index)
    except curses.error:
        pairs["error"] = 0
    return pairs


ANSI_OFF = "\033[0m"


def _ansi(text, severity, enabled):
    """Colour by the same palette the overlay uses, so both agree."""
    if not enabled:
        return text
    name = USAGE_COLORS.get(severity)
    if name is None:
        return text
    return "\033[%dm%s%s" % (30 + COLOR_NAMES[name], text, ANSI_OFF)


def cmd_usage(argv):
    """What is left on every saved account, for both harnesses.

    `--kind` narrows it to one of them, the same way `list` does. Reading a
    reading costs a request per saved account, and a token renewal on a parked
    one, so a caller that cares about a single harness does not pay for the
    other. An unknown kind reports nothing, because `usage_rows` reads an empty
    kind list as "every kind".
    """
    kinds = KIND_ORDER
    if "--kind" in argv:
        wanted = argv[argv.index("--kind") + 1]
        kinds = [wanted] if wanted in BACKENDS else []
    if "--json" in argv:
        print(json.dumps(usage_rows(kinds) if kinds else [], indent=2))
        return 0
    color = "--color" in argv
    rows = usage_rows(kinds) if kinds else []
    if not rows:
        print("no profiles saved yet — run the save action")
        return 1
    for kind in KIND_ORDER:
        mine = [r for r in rows if r["kind"] == kind]
        if not mine:
            continue
        print(BACKENDS[kind].title)
        for row in mine:
            mark = "●" if row["active"] else " "
            note = "" if row["state"] == "live" else "  (%s)" % _age(row["age"])
            print("  %s %-14s%s" % (mark, row["label"], note))
            if not row["windows"]:
                print("      %s" % (row.get("problem") or
                                    ("never read" if row["state"] == "unknown"
                                     else "no windows reported")))
            for window in row["windows"]:
                percent = window.get("percent")
                sev = severity_of(percent if row["state"] == "live" else None)
                left = window.get("resets_at")
                left = _left(left - time.time()) if left else "—"
                text = "%-14s %s %5s%%  resets %s" % (
                    window.get("label", "window"),
                    bar_for(percent),
                    "?" if percent is None else round(percent),
                    left,
                )
                print("      " + _ansi(text, sev, color))
    return 0


# ---- overlay picker -------------------------------------------------------

def _rows():
    """Display model: ("head", kind, text) / ("profile", kind, profile) /
    ("empty", kind, text). Only "profile" rows are selectable."""
    rows = []
    for kind in ("codex", "claude"):
        backend = BACKENDS[kind]
        profiles = list_profiles(kind)
        if not backend.present() and not profiles:
            continue
        live = backend.read_live()
        current = active_profile(kind, live) if live else None
        head = backend.title
        if kind == "claude":
            head += " (beta)"
        if live is None:
            head += "  — not logged in"
        elif current is None:
            # A freshly authenticated grant may not belong to the selected
            # registration yet. That does not make the person/workspace new.
            # This is display-only: never select or overwrite a UUID by identity.
            identity = backend.identity(live)
            if not _live_account_saved(kind, live, profiles):
                head += (f"  — live: {backend.describe(identity)} "
                         "(press 's' to save this account)")
        rows.append(("head", kind, head))
        label_counts = {}
        for profile in profiles:
            label_counts[profile["label"]] = label_counts.get(profile["label"], 0) + 1
        for p in profiles:
            p["_active"] = current is not None and p["id"] == current["id"]
            if label_counts[p["label"]] > 1 and p.get("saved_at"):
                p["_saved_hint"] = datetime.fromtimestamp(p["saved_at"], timezone.utc).strftime("%m-%d %H:%M UTC")
            rows.append(("profile", kind, p))
        if not profiles:
            rows.append(("empty", kind, "  (nothing saved — press 's' to save the current login)"))
    if not rows:
        rows.append(("empty", None, "  no Claude Code or Codex install found"))
    return rows


LABEL_W = 16
USAGE_W = 20
RESETS_W = 12
RESETS_GAP = 4
# Where the usage column starts: " " + mark + " " + label + " ".
USAGE_COL = 3 + LABEL_W + 1


def _profile_line(kind, p, width, usage=None, problem=False, resets=None):
    backend = BACKENDS[kind]
    mark = "!" if problem else ("●" if p.get("_active") else " ")
    desc = backend.describe(p.get("identity") or {})
    if p.get("_saved_hint"):
        desc = "saved " + p["_saved_hint"] + " | " + desc
    label = str(p["label"])[:LABEL_W]
    if resets is not None:
        desc = "%-*s %s" % (RESETS_W, resets[:RESETS_W], desc)
    if usage is None:
        line = " %s %-*s %s" % (mark, LABEL_W, label, desc)
    else:
        line = " %s %-*s %-*s%s%s" % (
            mark, LABEL_W, label, USAGE_W, usage[:USAGE_W], " " * RESETS_GAP, desc)
    return line[: max(0, width - 1)]


def reset_summary(kind, row):
    if kind != "codex":
        return ""
    summary = _reset_summary((row or {}).get("reset_credits"))
    if summary is None or (row or {}).get("problem"):
        return "-"
    at = (row or {}).get("reset_credits_at") or 0
    old = time.time() - at > USAGE_TTL_S
    count = summary["available_count"]
    unit = "reset" if count <= 1 else "resets"
    return f"{'~' if old else ''}{count} {unit}"


def _picker_help(width):
    # Keep add-new/use-reset together. Wrap complete actions rather than
    # clipping the last shortcuts or splitting an action across two lines.
    actions = ["'enter' switch", "'s' save current acc", "'a' sign in again", "'r' refresh",
               "'g' rename", "'x' delete", "'d' details",
               "'u' add new | 't' use reset", "'esc' quit"]
    lines = []
    for action in actions:
        if lines and len(lines[-1]) + len(action) + 3 <= width:
            lines[-1] += " | " + action
        else:
            lines.append(action)
    return lines


def summary_window(row):
    """The window the one-line view shows: the short one.

    There is room for one figure, and ranking the windows against each other
    kept choosing the wrong one. A window that will not fill projects nothing,
    so it dropped out of the comparison entirely, and a nearly empty weekly
    window that did project beat a session window most of the way through. A
    real account read 9% with two days left while its session sat at 58% and
    six minutes from resetting.

    The short window is the one you meet again and again in a working day, and a
    weekly window at 9% is not what stops anybody. So it is simply the one
    shown, with no ranking to get wrong.

    A spent window is the exception. Once one is full the account is blocked,
    and saying that outranks saying how the session is doing.

    Every window is still there in the expanded view, each with its own
    projection.
    """
    windows = [w for w in shown_windows(row)
               if isinstance(w.get("percent"), (int, float))]
    if not windows:
        return None
    spent = [w for w in windows if w["percent"] >= 100]
    if spent:
        return min(spent, key=lambda w: w.get("resets_at") or 0)
    return min(windows, key=lambda w: window_seconds(w) or float("inf"))


FIVE_HOURS = 5 * 3600
SEVEN_DAYS = 7 * 86400
# Spend this much of a window and its rate means something, however new it is.
SETTLED_PERCENT = _num_env("ACCOUNT_HUB_SETTLED_PERCENT", 5.0)

DETAIL_FMT = "%-9s %3s%%  %s  %s"
# The collapsed view's one-line summary: bar, percent, and automatic reset.
SUMMARY_FMT = "%s %3d%% %s"
# Mark every known automatic reset countdown.
RESET_MARK = "↺"


def window_seconds(window):
    """How long this window runs, so elapsed time can be worked out."""
    if window.get("seconds"):
        return window["seconds"]
    label = (window.get("label") or "").lower()
    if label in ("session", "five_hour"):
        return FIVE_HOURS
    if label.startswith("weekly") or label.startswith("seven_day"):
        return SEVEN_DAYS
    # A duration used as a name, e.g. codex's "7d" / "168h" from an older cache.
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([dh])", label)
    if match:
        return float(match.group(1)) * (86400 if match.group(2) == "d" else 3600)
    return None


def window_name(label):
    """The short name to show, rather than the API's key."""
    text = str(label or "")
    lowered = text.lower()
    # "5h" is codex's key for the same thing claude calls a session.
    if lowered in ("session", "five_hour", "5h"):
        return "Session"
    if lowered in ("weekly_all", "seven_day", "weekly"):
        return "Weekly"
    if lowered.startswith("weekly "):
        return text.split(" ", 1)[1]        # the model's own name, e.g. Fable
    return text


def read_at_of(window):
    """When this window's numbers were read. Now, for a window with no stamp."""
    stamped = window.get("read_at")
    return stamped if isinstance(stamped, (int, float)) else time.time()


def _spend_rate(window):
    """Percent per second, measured at the moment the numbers were read.

    None until enough has been spent for that to mean anything. A window minutes
    old can give a rate wild enough to claim the limit is imminent, so there has
    to be some bar. Elapsed time alone is the wrong one: a five-hour window can
    be a quarter spent before 5% of it has run, and the start of a window is
    exactly when the answer matters. How much has been spent is evidence too.

    The rate belongs to the read, not to now. A cached reading holds its
    percentage still while the clock keeps running, so dividing by the time
    elapsed until now made a busy account look calmer the longer nobody looked
    at it — backwards, for a number about what is going to stop you.
    """
    percent, resets = window.get("percent"), window.get("resets_at")
    if not isinstance(percent, (int, float)) or percent <= 0 or not resets:
        return None
    total = window_seconds(window)
    left = resets - read_at_of(window)
    if not total or left <= 0:
        return None
    elapsed = total - left
    if elapsed <= 0:
        return None
    settled = elapsed >= max(600.0, total * 0.05) or percent >= SETTLED_PERCENT
    return (percent / elapsed) if settled else None


def time_to_limit(window):
    """Seconds until this window reaches its own limit, at the rate it is spent.

    Zero when it is already spent. None when it will reset before it fills, or
    when too little has been spent for a rate to mean anything.

    This is what says which window is about to stop you. A percentage cannot:
    42% of a weekly window is a day and a half away while 10% of a five-hour
    window can be under two hours away, and comparing the two numbers rates the
    weekly as the worse of them.
    """
    percent = window.get("percent")
    if isinstance(percent, (int, float)) and percent >= 100:
        return 0.0
    rate = _spend_rate(window)
    if rate is None:
        return None
    read_at = read_at_of(window)
    if percent + rate * (window["resets_at"] - read_at) < 100:
        return None
    # The limit lands a fixed time after the read, so what is left of it now is
    # that much less. A reading nobody has refreshed brings the answer closer.
    return max(0.0, read_at + (100 - percent) / rate - time.time())


def projection(window):
    """Where this window lands by its reset, at the rate it is being spent.

    This is the number that decides whether to switch accounts now: 66% used is
    fine with a day left and a problem with five. The reset comes along with it,
    because "limit in 1d12h" means nothing without knowing what it is racing.
    """
    percent, resets = window.get("percent"), window.get("resets_at")
    if not resets:
        return "—"
    until = _left(resets - time.time())
    if percent is not None and percent >= 100:
        return "spent (resets in %s)" % until
    rate = _spend_rate(window)
    if rate is None:
        return "resets in %s" % until
    hits = time_to_limit(window)
    if hits is None:
        landing = percent + rate * (resets - read_at_of(window))
        return "~%d%% left (resets in %s)" % (round(100 - landing), until)
    return "limit in %s (resets in %s)" % (_left(hits), until)


def shown_windows(row):
    """Every window the account reported.

    Codex windows used to be reduced to the longest one, on the reasoning that
    the weekly figure was the one worth the space. It is not: the five-hour
    window is what stops you hour to hour, and dropping it meant a five-hour
    window sitting at 100% was replaced in the picker by a roomy weekly number.
    The picker then reported capacity on an account that had none.
    """
    return list((row or {}).get("windows") or [])


def usage_detail(row):
    """[(text, severity)] — one line per window, for the expanded view."""
    if not row:
        return []
    notice = usage_notice(row)
    live = row.get("state") == "live" and not notice
    out = [(notice, "stale")] if notice else []
    for window in shown_windows(row):
        percent = window.get("percent")
        out.append((
            DETAIL_FMT % (
                window_name(window.get("label")),
                "?" if percent is None else round(percent),
                bar_for(percent, 10),
                projection(window),
            ),
            severity_of(percent if live else None),
        ))
    if not out:
        out.append(((row.get("problem") or "no windows read"), "stale"))
    return out


def usage_notice(row):
    """Keep an old reading from looking like current available capacity.

    Calculate age at render time: the picker can stay open long after its
    initial successful read has expired.
    """
    at = row.get("at")
    age = max(0, time.time() - at) if at is not None else row.get("age")
    problem = row.get("problem")
    if problem:
        return problem + ("; read " + _age(age) if age is not None else "")
    if age is not None and age > USAGE_TTL_S:
        return "read " + _age(age)
    return None


def usage_summary(row):
    """"How much is left" for one profile row, sized for the picker column.

    Only the binding window: which window that is, and the rest of them, is
    what the expanded picker is for.
    """
    if row is None:
        return "", "stale"
    if row.get("state") == "unknown":
        return (row.get("problem") or "no usage read")[:USAGE_W], "stale"
    notice = usage_notice(row)
    window = summary_window(row)
    if not window:
        return "no windows", "stale"
    percent = window["percent"]
    # The compact view always shows the automatic reset. Exhaustion estimates
    # remain in projection(), used by the detailed view only.
    resets = window.get("resets_at")
    left = (_left(resets - time.time()) + RESET_MARK) if resets else "—"
    text = SUMMARY_FMT % (bar_for(percent, 8), round(percent), left)
    if notice:
        # Keep capacity useful during a reporting outage. The marker identifies
        # the last-known percentage; details carry its age and refresh error.
        # Borrow one bar cell so even ~100% leaves the reset column aligned.
        text = "%s %5s %s" % (bar_for(percent, 7), "~%d%%" % round(percent), left)
    # Keep the reset marker visible if a longer countdown must be truncated.
    if len(text) > USAGE_W and text.endswith(RESET_MARK):
        text = text[:USAGE_W - 1] + RESET_MARK
    return text[:USAGE_W], severity_of(percent)


def _center_win(stdscr, height, width):
    import curses

    h, w = stdscr.getmaxyx()
    height, width = min(height, h), min(width, w)
    win = curses.newwin(
        height, width, max(0, (h - height) // 2), max(0, (w - width) // 2)
    )
    win.keypad(True)
    return win


def _ask(stdscr, title, question, initial=""):
    """Centred one-field dialog. Returns the text, or None on esc."""
    import curses

    hint = "enter | esc"
    buf = list(initial)
    width = min(
        max(len(question), len(title), len(hint), 44) + 6, stdscr.getmaxyx()[1]
    )
    try:
        while True:
            win = _center_win(stdscr, 8, width)
            win.erase()
            win.box()
            inner = width - 4
            win.addnstr(0, 2, " %s " % title, inner, curses.A_BOLD)
            win.addnstr(2, 2, question, inner, curses.A_BOLD)
            win.addnstr(4, 2, "> " + "".join(buf) + "_", inner)
            win.addnstr(6, 2, hint, inner, curses.A_DIM)
            win.refresh()
            ch = win.getch()
            if ch == 27:
                return None
            if ch in (curses.KEY_ENTER, 10, 13):
                return "".join(buf).strip()
            if ch in (curses.KEY_BACKSPACE, 127, 8):
                if buf:
                    buf.pop()
            elif 32 <= ch < 127:
                buf.append(chr(ch))
    finally:
        stdscr.touchwin()
        stdscr.refresh()


def _confirm(stdscr, title, question, note=None):
    """Centered confirmation. Only Enter confirms; Esc/other keys cancel."""
    import curses

    hint = "enter | esc"
    width = min(
        max(len(question), len(title), len(hint), len(note or ""), 44) + 6,
        stdscr.getmaxyx()[1],
    )
    height = 9 if note else 7
    try:
        win = _center_win(stdscr, height, width)
        win.erase()
        win.box()
        inner = width - 4
        win.addnstr(0, 2, " %s " % title, inner, curses.A_BOLD)
        win.addnstr(2, 2, question, inner, curses.A_BOLD)
        if note:
            win.addnstr(4, 2, note, inner, curses.A_DIM)
        win.addnstr(height - 2, 2, hint, inner, curses.A_DIM)
        win.refresh()
        return win.getch() in (curses.KEY_ENTER, 10, 13)
    finally:
        stdscr.touchwin()
        stdscr.refresh()


def _choose(stdscr, title, options):
    """Centered selection dialog: arrows/Enter choose, Esc cancels."""
    import curses
    hint = "↑/↓ | enter | esc"
    width = min(max(len(title), len(hint), *(len(label) for _, label in options)) + 6,
                stdscr.getmaxyx()[1])
    selected = 0
    try:
        while True:
            win = _center_win(stdscr, len(options) + 6, width)
            win.erase()
            win.box()
            inner = width - 4
            win.addnstr(0, 2, " %s " % title, inner, curses.A_BOLD)
            for index, (_, label) in enumerate(options):
                win.addnstr(index + 2, 2, label.ljust(inner), inner,
                            curses.A_REVERSE if index == selected else curses.A_NORMAL)
            win.addnstr(len(options) + 4, 2, hint, inner, curses.A_DIM)
            win.refresh()
            ch = win.getch()
            if ch == 27:
                return None
            if ch in (curses.KEY_ENTER, 10, 13):
                return options[selected][0]
            if ch in (curses.KEY_DOWN, ord("j")):
                selected = min(selected + 1, len(options) - 1)
            elif ch in (curses.KEY_UP, ord("k")):
                selected = max(selected - 1, 0)
    finally:
        stdscr.touchwin()
        stdscr.refresh()


def _ui_login(stdscr, kind, profile_id=None, label=None):
    """Give the terminal to official sign-in, then restore the account picker."""
    import curses
    curses.def_prog_mode()
    curses.endwin()
    try:
        print("\nAccount Hub — sign in to " + BACKENDS[kind].short)
        print("Complete the official login below. Current account stays active.")
        print("Saved automatically | Ctrl+C cancels | 15-minute limit\n", flush=True)
        if kind == "codex":
            return login_codex(profile_id=profile_id, label=label,
                               browser=_setting("ACCOUNT_HUB_CODEX_LOGIN_METHOD") == "browser")
        return login_claude(label=label)
    finally:
        curses.reset_prog_mode()
        curses.curs_set(0)
        stdscr.touchwin()
        stdscr.refresh()


def _ui_codex_login(stdscr, profile_id=None, label=None):
    return _ui_login(stdscr, "codex", profile_id=profile_id, label=label)


def cmd_ui(argv):
    import curses

    def run(stdscr):
        curses.curs_set(0)
        pairs = _usage_pairs(curses)
        # Slow refresh on purpose: each tick re-reads the credential store, and
        # on macOS that is a Keychain lookup per agent kind.
        stdscr.timeout(5000)
        sel = 0
        selected_key = None
        scroll = 0
        message = ""
        details = False
        # Opening requests fresh usage, even if a recent cache lacks new fields.
        # Later ticks follow the TTL or scheduled post-reset checks. Every read
        # still observes each account's Retry-After.
        usage = {"%s:%s" % (r["kind"], r["id"]): r for r in usage_rows(force=True)}
        usage_fetched = time.time()
        usage_profiles = set(usage)
        while True:
            rows = _rows()
            profile_keys = {"%s:%s" % (kind, body["id"])
                            for row_type, kind, body in rows if row_type == "profile"}
            if (time.time() - usage_fetched >= USAGE_TTL_S
                    or profile_keys != usage_profiles
                    or any(r.get("refresh_after") is not None
                           and time.time() >= r["refresh_after"] for r in usage.values())):
                usage = {"%s:%s" % (r["kind"], r["id"]): r for r in usage_rows()}
                usage_fetched = time.time()
                usage_profiles = {"%s:%s" % (kind, body["id"])
                                  for row_type, kind, body in rows if row_type == "profile"}
            else:
                # Another switcher pane may already have fetched newer usage.
                usage = {"%s:%s" % (r["kind"], r["id"]): r
                         for r in usage_rows(refresh=False)}
            pickable = [
                i for i, r in enumerate(rows)
                if r[0] == "profile" or (r[0] == "empty" and r[1])
            ]
            keys = [(rows[i][1], rows[i][2]["id"] if rows[i][0] == "profile" else None)
                    for i in pickable]
            if pickable:
                sel = (keys.index(selected_key) if selected_key in keys
                       else min(max(sel, 0), len(pickable) - 1))
                selected_key = keys[sel]
            stdscr.erase()
            h, w = stdscr.getmaxyx()
            count = len(profile_keys)
            stdscr.addnstr(0, 0, "%d account%s" % (count, "" if count == 1 else "s"),
                           w - 1, curses.A_BOLD)
            help_lines = _picker_help(w - 1)[:max(0, h - 4)]
            for y, text in enumerate(help_lines, start=1):
                stdscr.addnstr(y, 0, text.ljust(w - 1), w - 1, curses.A_DIM)
            navigation_y = 1 + len(help_lines)
            list_y = navigation_y + 1
            lines = []
            selected_span = None
            for i, (kind_row, kind, body) in enumerate(rows):
                start = len(lines)
                if kind_row == "head":
                    lines.append((body, curses.A_BOLD | curses.A_UNDERLINE, None, False))
                elif kind_row == "empty":
                    is_sel = pickable and i == pickable[sel]
                    attr = curses.A_REVERSE if is_sel else curses.A_DIM
                    lines.append((body, attr, None, False))
                    if is_sel:
                        selected_span = (start, len(lines))
                else:
                    is_sel = pickable and i == pickable[sel]
                    row = usage.get("%s:%s" % (kind, body["id"]))
                    # Expanded, the windows below carry the numbers, so the
                    # account line would only repeat one of them.
                    text, sev = ("", "ok") if details else usage_summary(row)
                    problem = bool(row and row.get("problem"))
                    line = _profile_line(kind, body, w, text, problem=problem,
                                         resets=reset_summary(kind, row)).ljust(w - 1)
                    lines.append((line, curses.A_REVERSE if is_sel else curses.A_NORMAL,
                                  (text, sev) if text and not is_sel else None, problem))
                    if details:
                        for line_text, line_sev in usage_detail(row):
                            lines.append((" " * USAGE_COL + line_text,
                                          pairs.get(line_sev, 0), None, False))
                    if is_sel:
                        selected_span = (start, len(lines))
                    if details:
                        lines.append(("", curses.A_NORMAL, None, False))
            available = max(0, h - 1 - list_y)
            if available and selected_span:
                start, end = selected_span
                if start < scroll:
                    scroll = start
                elif end > scroll + available:
                    scroll = min(start, end - available)
            scroll = max(0, min(scroll, max(0, len(lines) - available)))
            for y, (line, attr, coloured_usage, problem) in enumerate(
                    lines[scroll:scroll + available], start=list_y):
                stdscr.addnstr(y, 0, line.ljust(w - 1), w - 1, attr)
                if problem and w > 2:
                    stdscr.addnstr(y, 1, "!", 1, pairs.get("error", 0) | curses.A_BOLD)
                if coloured_usage and w > USAGE_COL + 1:
                    text, sev = coloured_usage
                    stdscr.addnstr(y, USAGE_COL, text, w - 1 - USAGE_COL,
                                   pairs.get(sev, 0))
            if navigation_y < h - 1 and len(lines) > available:
                navigation = "j/k scroll"
                if scroll:
                    navigation += " | ↑ more above"
                if scroll + available < len(lines):
                    navigation += " | ↓ more below"
                stdscr.addnstr(navigation_y, 0, navigation, w - 1, curses.A_DIM)
            if message and h > 1:
                stdscr.addnstr(h - 1, 0, message[: w - 1], w - 1, curses.A_BOLD)
            elif h > 1 and any(usage_notice(r) for r in usage.values()):
                stdscr.addnstr(h - 1, 0, "~ last known usage | d details", w - 1, curses.A_DIM)
            stdscr.refresh()

            try:
                ch = stdscr.getch()
            except KeyboardInterrupt:
                return
            if ch == -1:
                continue
            if ch in (ord("q"), 27):
                return

            row = rows[pickable[sel]] if pickable else None
            # A row is only a profile when it isn't the "nothing saved" filler.
            selected = row if row and row[0] == "profile" else None
            heads = [r[1] for r in rows if r[0] == "head"]
            kind = row[1] if row else (heads[0] if heads else None)

            if ch in (ord("j"), curses.KEY_DOWN) and pickable:
                sel = min(len(pickable) - 1, sel + 1)
                selected_key = keys[sel]
            elif ch in (ord("k"), curses.KEY_UP) and pickable:
                sel = max(0, sel - 1)
                selected_key = keys[sel]
            elif ch in (curses.KEY_ENTER, 10, 13) and selected:
                profile = selected[2]
                if selected[1] == "codex" and not _confirm(
                        stdscr, "Switch Codex account",
                        f"Switch to {profile['label']} ?",
                        "Reload open chats; keep history and titles."):
                    message = "Account change cancelled"
                    continue
                try:
                    if selected[1] == "codex":
                        title = "Switching Codex account and reloading chats..."
                        note = "(Daemon may wait for active work)"
                        progress = title + " " + note
                        progress_lines = ([progress] if len(progress) < w else [title, note])[-h:]
                        for index, line in enumerate(progress_lines):
                            stdscr.addnstr(h - len(progress_lines) + index, 0,
                                          line.ljust(w - 1), w - 1, curses.A_BOLD)
                        stdscr.refresh()
                    message = switch(selected[1], profile["id"])
                except SwitchError as exc:
                    message = str(exc)
            elif ch == ord("u"):
                provider = _choose(stdscr, "Add new account",
                                   [("codex", "Codex"), ("claude", "Claude (beta)")])
                if provider is None:
                    continue
                title = "Add " + BACKENDS[provider].short + " account"
                label = _ask(stdscr, title, "Name")
                if label is not None and _confirm(
                        stdscr, title, "Start official sign-in?"):
                    try:
                        message = _ui_login(stdscr, provider, label=label or None)
                    except SwitchError as exc:
                        message = str(exc)
            elif ch == ord("s") and kind:
                live = BACKENDS[kind].read_live()
                if live is None:
                    message = f"{kind}: nothing logged in to save"
                    continue
                if _live_account_saved(kind, live):
                    message = "Account already saved"
                    continue
                suggested = default_label(kind, BACKENDS[kind].identity(live))
                name = _ask(
                    stdscr, "Save profile", f"Name this {kind} login?", suggested
                )
                if name:
                    try:
                        message = save_live(kind, name)
                    except SwitchError as exc:
                        message = str(exc)
            elif ch == ord("g") and selected:
                name = _ask(
                    stdscr, "Rename profile",
                    f"New name for {selected[2]['label']}?", selected[2]["label"],
                )
                if name:
                    profile = selected[2]
                    try:
                        rename_profile(profile["kind"], profile["id"], name)
                        message = f"renamed to {name}"
                    except SwitchError as exc:
                        message = str(exc)
            elif ch == ord("a") and selected:
                profile = selected[2]
                if profile["kind"] == "codex":
                    if _confirm(stdscr, "Sign in to Codex account",
                                f"Sign in again to {profile['label']}?",
                                "Use the official login. Apply with Enter afterwards."):
                        try:
                            message = _ui_codex_login(stdscr, profile_id=profile["id"])
                            usage_fetched = 0
                        except SwitchError as exc:
                            message = str(exc)
                    continue
                live = BACKENDS[profile["kind"]].read_live()
                desc = (BACKENDS[profile["kind"]].describe(
                    BACKENDS[profile["kind"]].identity(live)) if live else "logged out")
                if _confirm(stdscr, "Reauthenticate profile",
                            f"Replace credentials for {profile['label']}?",
                            f"Capture current login: {desc}"):
                    try:
                        message = reauthenticate(profile["kind"], profile["id"])
                    except SwitchError as exc:
                        message = str(exc)
            elif ch == ord("c"):
                label = _ask(stdscr, "Add Codex account", "Name")
                if label and _confirm(stdscr, "Add Codex account",
                                      f"Sign in to add {label}?",
                                      "Use the official login. Current account stays active."):
                    try:
                        message = _ui_codex_login(stdscr, label=label)
                    except SwitchError as exc:
                        message = str(exc)
            elif ch == ord("d"):
                details = not details
            elif ch == ord("t") and selected:
                profile = selected[2]
                try:
                    stdscr.addnstr(h - 1, 0, "Checking resets...".ljust(w - 1),
                                  w - 1, curses.A_BOLD)
                    stdscr.refresh()
                    attempt = prepare_reset(selected[1], profile["id"])
                    expiry = attempt["credit"]["expires_at"]
                    note = ("Expires: " + datetime.fromtimestamp(expiry, timezone.utc).strftime(
                            "%Y-%m-%d %H:%M UTC") if expiry is not None else "Expires: not provided")
                    if not _confirm(stdscr, "Use reset", f"Use a reset for {profile['label']} ?", note):
                        message = "Reset cancelled"
                        continue
                    stdscr.addnstr(h - 1, 0, "Using reset...".ljust(w - 1),
                                  w - 1, curses.A_BOLD)
                    stdscr.refresh()
                    message = consume_reset(attempt)
                except SwitchError as exc:
                    message = str(exc)
                except OSError:
                    message = "Cannot save reset state; result not confirmed. Press 't' to retry."
            elif ch == ord("r"):
                message = "refreshing accounts…"
                stdscr.addnstr(h - 1, 0, message.ljust(w - 1), w - 1, curses.A_BOLD)
                stdscr.refresh()
                usage = {"%s:%s" % (r["kind"], r["id"]): r for r in usage_rows(force=True)}
                usage_fetched = time.time()
                usage_profiles = set(usage)
                stale = [r["label"] for r in usage.values() if r["state"] != "live"]
                message = "accounts refreshed"
                if stale:
                    message += "; cached for " + ", ".join(stale)
            elif ch == ord("x") and selected:
                profile = selected[2]
                # Deleting a profile removes this plugin's copy, never the
                # credential store the CLI reads — you stay logged in either way.
                note = None
                if profile.get("_active"):
                    note = "In use. You stay logged in; only the copy goes."
                if _confirm(
                    stdscr, "Delete profile",
                    f"Delete {profile['label']}?", note,
                ):
                    delete_profile(profile["kind"], profile["id"])
                    message = f"deleted {profile['label']}"
                    sel = max(0, sel - 1)

    curses.wrapper(run)
    return 0


# What each account-changing command was doing, for the message when it fails.
# These run from a herdr action, where stderr goes nowhere a person will see, so
# a refusal has to say so on screen the way a completed switch already does.
# Polling commands such as `list` do not send failure notifications.
NOTIFY_FAILURE = {"next": "switch", "switch": "switch", "save": "save", "reauth": "reauth",
                  "codex-login": "sign in"}

DISPATCH = {
    "ui": cmd_ui,
    "open": cmd_open,
    "next": cmd_next,
    "switch": cmd_switch,
    "save": cmd_save,
    "reauth": cmd_reauth,
    "codex-login": cmd_codex_login,
    "status": cmd_status,
    "list": cmd_list,
    "usage": cmd_usage,
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in DISPATCH:
        print(f"usage: switcher.py {{{'|'.join(DISPATCH)}}} [args]", file=sys.stderr)
        return 2
    try:
        return DISPATCH[sys.argv[1]](sys.argv[2:]) or 0
    except SwitchError as exc:
        print(f"account-hub: {exc}", file=sys.stderr)
        doing = NOTIFY_FAILURE.get(sys.argv[1])
        if doing:
            notify(f"Account Hub: {doing} failed", str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
