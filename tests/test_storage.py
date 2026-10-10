"""Durable dev/prod stores and upgrade migration with fictitious data only."""
import importlib.util
import errno
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

from tests.test_account_records import login

ROOT = Path(__file__).resolve().parents[1]
OFFICIAL = "c0r0nell.account-hub"
DEVELOPMENT = "c0r0nell.dev-account-hub"


class Storage(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hub-storage-")
        self.base = Path(self.temp.name)
        self.root = self.checkout("checkout")
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("HERDR_PLUGIN_", "ACCOUNT_HUB_"))}
        self.env.update(XDG_CONFIG_HOME=str(self.base / "config"),
                        XDG_STATE_HOME=str(self.base / "state"),
                        CODEX_HOME=str(self.base / "codex"),
                        CLAUDE_CONFIG_DIR=str(self.base / "claude"))

    def tearDown(self):
        self.temp.cleanup()

    def checkout(self, name):
        root = self.base / name
        root.mkdir()
        for filename in ("switcher.py", "herdr-plugin.toml"):
            shutil.copyfile(ROOT / filename, root / filename)
        return root

    def load(self, overrides=None, root=None):
        root = root or self.root
        with patch.dict(os.environ, dict(self.env, **(overrides or {})), clear=True):
            spec = importlib.util.spec_from_file_location("storage_fixture", root / "switcher.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        return module

    def run_storage(self, root=None, overrides=None):
        return subprocess.run([sys.executable, "-B", str((root or self.root) / "switcher.py"),
                               "storage-migrate"], env=dict(self.env, **(overrides or {})),
                              capture_output=True, text=True, timeout=10)

    def legacy(self, root=None):
        root = root or self.root
        source = root / ".local"
        profile_id = str(uuid.uuid4())
        files = {
            f"profiles/codex/{profile_id}.json": {
                "id": profile_id, "label": "Fixture", "payload": login()},
            "selection.json": {"codex": profile_id},
            "rotated/codex-" + profile_id + ".json": login(credential="rotated"),
            "reset-attempts.json": {profile_id: {"status": "pending", "request_id": "same-operation"}},
            "usage-cache.json": {"codex:" + profile_id: {"at": 123, "windows": []}},
            "backups/original.json": login(),
        }
        for relative, value in files.items():
            path = source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value))
        (source / "config").mkdir()
        (source / "config/config.toml").write_text('notify = false\nusage_ttl_s = 987\n')
        return source, profile_id, files

    def test_default_state_and_config_are_outside_the_checkout(self):
        S = self.load()
        self.assertEqual(Path(S.STATE_DIR), self.base / "state/herdr/plugins" / OFFICIAL)
        self.assertEqual(Path(S.CONFIG_DIR), self.base / "config/herdr/plugins/config" / OFFICIAL)
        self.assertFalse(Path(S.STATE_DIR).is_relative_to(self.root))

    def test_injected_herdr_directories_are_used_for_the_matching_plugin(self):
        state, config = self.base / "host-state", self.base / "host-config"
        S = self.load({"HERDR_PLUGIN_ID": OFFICIAL,
                       "HERDR_PLUGIN_STATE_DIR": str(state),
                       "HERDR_PLUGIN_CONFIG_DIR": str(config)})
        self.assertEqual(Path(S.STATE_DIR), state)
        self.assertEqual(Path(S.CONFIG_DIR), config)

    def test_development_and_production_are_separate_and_stable_across_checkouts(self):
        dev_manifest = (self.root / "herdr-plugin.toml").read_text().replace(
            'id = "' + OFFICIAL + '"', 'id = "' + DEVELOPMENT + '"', 1)
        (self.root / "herdr-plugin.dev.toml").write_text(dev_manifest)
        dev = self.load()
        prod = self.load(root=self.checkout("production"))
        self.assertEqual(dev.PLUGIN_ID, DEVELOPMENT)
        self.assertNotEqual(dev.STATE_DIR, prod.STATE_DIR)
        shutil.rmtree(self.root)
        replacement = self.checkout("new-development")
        (replacement / "herdr-plugin.dev.toml").write_text(dev_manifest)
        self.assertEqual(self.load(root=replacement).STATE_DIR, dev.STATE_DIR)

    def test_legacy_migration_preserves_every_record_and_survives_checkout_replacement(self):
        source, profile_id, files = self.legacy()
        result = self.run_storage()
        self.assertEqual(result.returncode, 0, result.stderr)
        S = self.load()
        for relative, value in files.items():
            self.assertEqual(json.loads((Path(S.STATE_DIR) / relative).read_text()), value)
        self.assertEqual((Path(S.CONFIG_DIR) / "config.toml").read_text(),
                         'notify = false\nusage_ttl_s = 987\n')
        self.assertTrue(source.is_symlink(), "old pickers need a compatibility path")
        self.assertEqual(S._selection()["codex"], profile_id)
        self.assertEqual(S.list_profiles("codex")[0]["id"], profile_id)
        shutil.rmtree(self.root)
        fresh = self.checkout("replacement")
        result = self.run_storage(root=fresh)
        self.assertEqual(result.returncode, 0, result.stderr)
        new = self.load(root=fresh)
        self.assertEqual(new.list_profiles("codex")[0]["id"], profile_id)
        self.assertEqual(new.USAGE_TTL_S, 987)
        self.assertFalse(new.NOTIFY)

    def test_install_hook_imports_the_registered_old_checkout_before_cleanup(self):
        old = self.checkout("old-managed-installation")
        source, profile_id, _ = self.legacy(old)
        registry = self.base / "config/herdr/plugins.json"
        registry.parent.mkdir(parents=True)
        registry.write_text(json.dumps([
            {"plugin_id": OFFICIAL, "plugin_root": str(old)},
            {"plugin_id": "unrelated.plugin", "plugin_root": str(self.checkout("unrelated"))}]))
        result = self.run_storage()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(source.is_symlink())
        shutil.rmtree(old)
        self.assertEqual(self.load().list_profiles("codex")[0]["id"], profile_id)

    def test_repeated_migration_keeps_newer_data_and_the_shared_lock_inode(self):
        source, _, _ = self.legacy()
        lock = source / "switch.lock"
        lock.touch()
        inode = lock.stat().st_ino
        self.assertEqual(self.run_storage().returncode, 0)
        state = Path(self.load().STATE_DIR)
        (state / "selection.json").write_text('{"codex": "newer-selection"}')
        self.assertEqual(self.run_storage().returncode, 0)
        self.assertEqual(lock.stat().st_ino, inode)
        self.assertEqual((state / "selection.json").read_text(), '{"codex": "newer-selection"}')
        for directory, _, files in os.walk(state):
            self.assertEqual(os.stat(directory).st_mode & 0o777, 0o700)
            for filename in files:
                path = Path(directory) / filename
                if not path.is_symlink():
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(Path(self.load().CONFIG_DIR).stat().st_mode & 0o777, 0o700)

    def test_existing_durable_accounts_are_never_overwritten_or_merged(self):
        source, _, _ = self.legacy()
        state = Path(self.load().STATE_DIR)
        state.mkdir(parents=True)
        saved = state / "selection.json"
        saved.write_text('{"codex": "existing-selection"}')
        result = self.run_storage()
        self.assertEqual(result.returncode, 1)
        self.assertIn("both contain data", result.stderr)
        self.assertFalse(source.is_symlink())
        self.assertEqual(saved.read_text(), '{"codex": "existing-selection"}')

    def test_configuration_conflict_keeps_both_copies_and_can_be_retried(self):
        source, _, _ = self.legacy()
        S = self.load()
        config = Path(S.CONFIG_DIR)
        config.mkdir(parents=True)
        target = config / "config.toml"
        target.write_text('notify = true\n')
        result = self.run_storage()
        self.assertEqual(result.returncode, 1)
        self.assertIn("configuration differ", result.stderr)
        self.assertEqual(target.read_text(), 'notify = true\n')
        self.assertEqual((source / "config/config.toml").read_text(), 'notify = false\nusage_ttl_s = 987\n')
        target.unlink()
        self.assertEqual(self.run_storage().returncode, 0)
        self.assertEqual(target.read_text(), 'notify = false\nusage_ttl_s = 987\n')

    def test_configuration_target_symlinks_are_rejected_without_modifying_external_data(self):
        source, _, _ = self.legacy()
        config = Path(self.load().CONFIG_DIR)
        config.mkdir(parents=True)
        unrelated = self.base / "unrelated-config"
        text = 'notify = false\nusage_ttl_s = 987\n'
        unrelated.write_text(text)
        (config / "config.toml").symlink_to(unrelated)
        result = self.run_storage()
        self.assertEqual(result.returncode, 1)
        self.assertIn("link", result.stderr)
        self.assertEqual(unrelated.read_text(), text)
        self.assertTrue((source / "config/config.toml").exists())

    def test_configuration_write_failure_is_atomic_and_retryable(self):
        _, _, _ = self.legacy()
        S = self.load()
        replace = S.os.replace

        def fail_config(source, destination):
            if Path(destination) == Path(S.CONFIG_DIR) / "config.toml":
                raise OSError("simulated interrupted config write")
            return replace(source, destination)

        with patch.dict(os.environ, self.env, clear=True), patch.object(S.os, "replace", side_effect=fail_config):
            with self.assertRaises(OSError):
                S.initialize_storage()
        self.assertFalse((Path(S.CONFIG_DIR) / "config.toml").exists())
        self.assertEqual(self.run_storage().returncode, 0)

    def test_waiting_for_an_old_operation_times_out_without_moving_its_store(self):
        source, _, _ = self.legacy()
        with (source / "switch.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            result = self.run_storage(overrides={"ACCOUNT_HUB_LOCK_TIMEOUT_S": "0.1"})
        self.assertEqual(result.returncode, 1)
        self.assertIn("waiting", result.stderr)
        self.assertFalse(source.is_symlink())
        self.assertEqual(self.run_storage().returncode, 0)

    def test_unrelated_host_and_explicit_test_stores_never_import_legacy_accounts(self):
        self.legacy()
        S = self.load({"HERDR_PLUGIN_ID": "unrelated.plugin",
                       "HERDR_PLUGIN_STATE_DIR": str(self.base / "other-state"),
                       "HERDR_PLUGIN_CONFIG_DIR": str(self.base / "other-config")})
        self.assertEqual(S.PLUGIN_ID, OFFICIAL)
        isolated = self.base / "isolated-test-state"
        result = self.run_storage(overrides={"ACCOUNT_HUB_STATE_DIR": str(isolated)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((isolated / "profiles").exists())
        self.assertFalse((self.root / ".local").is_symlink())

    def test_inside_checkout_paths_are_refused(self):
        result = self.run_storage(overrides={"ACCOUNT_HUB_STATE_DIR": str(self.root / ".local")})
        self.assertEqual(result.returncode, 1)
        self.assertIn("outside the source checkout", result.stderr)

    def test_legacy_dev_pointer_is_preserved_without_traversing_the_source_code(self):
        source, _, _ = self.legacy()
        (source / "current").symlink_to(self.root, target_is_directory=True)
        script_mode = (self.root / "switcher.py").stat().st_mode
        result = self.run_storage()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((source / "current").readlink(), self.root)
        self.assertEqual((self.root / "switcher.py").stat().st_mode, script_mode)

    def test_unexpected_credential_links_stop_migration_before_any_move(self):
        source, _, _ = self.legacy()
        external = self.base / "other-credentials.json"
        external.write_text('{}')
        external.chmod(0o640)
        (source / "profiles/codex/unexpected.json").symlink_to(external)
        result = self.run_storage()
        self.assertEqual(result.returncode, 1)
        self.assertIn("unexpected link", result.stderr)
        self.assertFalse(source.is_symlink())
        self.assertEqual(external.read_text(), '{}')
        self.assertEqual(external.stat().st_mode & 0o777, 0o640)

    def test_linked_lock_is_rejected_without_changing_external_permissions(self):
        source, _, _ = self.legacy()
        external = self.base / "other-private-file"
        external.write_text('unchanged')
        external.chmod(0o640)
        (source / 'switch.lock').symlink_to(external)
        result = self.run_storage()
        self.assertEqual(result.returncode, 1)
        self.assertIn('unexpected link', result.stderr)
        self.assertEqual(external.stat().st_mode & 0o777, 0o640)
        self.assertEqual(external.read_text(), 'unchanged')

    def test_cross_filesystem_move_fails_without_copying_or_losing_accounts(self):
        source, _, files = self.legacy()
        S = self.load()
        with patch.dict(os.environ, self.env, clear=True), \
                patch.object(S.os, 'rename', side_effect=OSError(errno.EXDEV, 'different filesystem')):
            with self.assertRaisesRegex(S.SwitchError, 'another filesystem'):
                S.initialize_storage()
        self.assertFalse(source.is_symlink())
        for relative, value in files.items():
            self.assertEqual(json.loads((source / relative).read_text()), value)
        self.assertFalse((Path(S.STATE_DIR) / 'profiles').exists())

    def test_failed_compatibility_link_restores_original_store_and_is_retryable(self):
        source, _, files = self.legacy()
        S = self.load()
        with patch.dict(os.environ, self.env, clear=True), \
                patch.object(S.os, 'symlink', side_effect=OSError('simulated unavailable link')):
            with self.assertRaises(OSError):
                S.initialize_storage()
        self.assertFalse(source.is_symlink())
        for relative, value in files.items():
            self.assertEqual(json.loads((source / relative).read_text()), value)
        self.assertEqual(self.run_storage().returncode, 0)


if __name__ == "__main__":
    unittest.main()
