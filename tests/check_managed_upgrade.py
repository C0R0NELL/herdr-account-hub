"""Exercise the installed Herdr CLI against a local, fictitious Git source."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.test_storage import Storage, OFFICIAL

fixture = Storage()
fixture.setUp()
try:
    env = {k: v for k, v in fixture.env.items() if not k.startswith('HERDR_')}
    env.update(XDG_DATA_HOME=str(fixture.base / 'data'),
               HERDR_CONFIG_PATH=str(fixture.base / 'config/herdr/config.toml'),
               GIT_CONFIG_GLOBAL=str(fixture.base / 'git-config'),
               GIT_CONFIG_NOSYSTEM='1', GIT_TERMINAL_PROMPT='0')
    url = 'https://github.com/account-hub-fixture/plugin.git'
    Path(env['GIT_CONFIG_GLOBAL']).write_text(
        '[url "' + fixture.root.as_uri() + '"]\n\tinsteadOf = ' + url + '\n')
    herdr = shutil.which('herdr')
    session = 'hub-storage-test-' + uuid.uuid4().hex[:10]

    def run(args, cwd=None):
        result = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, timeout=40)
        if result.returncode:
            raise RuntimeError('Command failed: ' + ' '.join(args) + '\n' + result.stderr + result.stdout)
        return result.stdout

    def commit(message):
        run(['git', 'add', 'switcher.py', 'herdr-plugin.toml'], fixture.root)
        run(['git', '-c', 'user.name=Storage Fixture', '-c', 'user.email=fixture@example.invalid',
             '-c', 'commit.gpgsign=false', 'commit', '-m', message], fixture.root)

    def install():
        return run([herdr, '--session', session, 'plugin', 'install', 'account-hub-fixture/plugin', '--yes'])

    def info():
        return json.loads(run([herdr, '--session', session, 'plugin', 'list', '--json']))

    official = (ROOT / 'herdr-plugin.toml').read_text()
    (fixture.root / 'herdr-plugin.toml').write_text(re.sub(r'\[\[build\]\]\ncommand = [^\n]*\n', '', official))
    run(['git', 'init', '-b', 'main'], fixture.root)
    commit('Fictitious legacy release')
    install()
    listed = info()
    if isinstance(listed, dict):
        listed = listed.get('result', listed)
        if isinstance(listed, dict):
            listed = listed.get('plugins', listed)
    entry = next(item for item in listed if item['plugin_id'] == OFFICIAL)
    old_root = Path(entry['plugin_root'])
    source, profile_id, files = fixture.legacy(old_root)
    (fixture.root / 'herdr-plugin.toml').write_text(official)
    commit('Fictitious durable-storage release')
    state = fixture.base / 'state/herdr/plugins' / OFFICIAL
    config = fixture.base / 'config/herdr/plugins/config' / OFFICIAL
    # A conflict must abort installation while the legacy checkout is retained.
    conflict = state / 'selection.json'
    conflict.write_text('{"codex": "other-durable-account"}')
    refused = subprocess.run([herdr, '--session', session, 'plugin', 'install',
                              'account-hub-fixture/plugin', '--yes'], env=env,
                             capture_output=True, text=True, timeout=40)
    assert refused.returncode != 0, 'conflicting data was not rejected'
    assert 'both contain data' in refused.stderr, refused.stderr
    assert old_root.exists() and not source.is_symlink(), 'old installation was removed after failure'
    assert conflict.read_text() == '{"codex": "other-durable-account"}'
    for relative, value in files.items():
        assert json.loads((source / relative).read_text()) == value, relative
    conflict.unlink()
    install()
    for relative, value in files.items():
        assert json.loads((state / relative).read_text()) == value, relative
    assert (config / 'config.toml').read_text() == 'notify = false\nusage_ttl_s = 987\n'
    # A second upstream commit causes another managed checkout replacement.
    (fixture.root / 'switcher.py').write_text((fixture.root / 'switcher.py').read_text() + '\n# fixture next release\n')
    commit('Fictitious next release')
    install()
    for relative, value in files.items():
        assert json.loads((state / relative).read_text()) == value, relative
    # Uninstall also leaves the plugin-owned durable directories intact.
    run([herdr, '--session', session, 'plugin', 'uninstall', OFFICIAL])
    assert (state / ('profiles/codex/' + profile_id + '.json')).exists()
    install()
    assert (state / ('profiles/codex/' + profile_id + '.json')).exists()
    print('PASS: real Herdr managed conflict rejection, legacy upgrade, next upgrade, uninstall and reinstall; all fictitious records unchanged')
finally:
    fixture.tearDown()
