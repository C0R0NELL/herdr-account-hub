"""Release workflow against temporary Git repositories and a fake GitHub CLI."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
NOTES = "### Fixed\n\n- Preserve account history.\n- Retry a busy refresh."


class ReleaseWorkflow(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hub-release-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "checkout"
        self.root.mkdir()
        self.remote = self.base / "remote.git"
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("GIT_", "GH_", "GITHUB_", "ACCOUNT_HUB_", "HERDR_"))}
        self.env.update(GIT_CONFIG_GLOBAL=str(self.base / "gitconfig"),
                        GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0",
                        PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
                        RELEASE_FIXTURE=str(self.base), PYTHONDONTWRITEBYTECODE="1")
        Path(self.env["GIT_CONFIG_GLOBAL"]).write_text(
            '[url "' + str(self.remote) + '"]\n'
            '\tinsteadOf = https://github.com/release-fixture/account-hub.git\n')
        self.git("init", "--bare", str(self.remote))
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Release Fixture")
        self.git("config", "user.email", "release@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        (self.root / "scripts").mkdir()
        shutil.copyfile(ROOT / "scripts/release.py", self.root / "scripts/release.py")
        (self.root / "tests").mkdir()
        for filename in ("check_switcher.py", "check_managed_upgrade.py"):
            (self.root / "tests" / filename).write_text(
                "from pathlib import Path\n"
                "Path(__file__).resolve().parents[2].joinpath('checks-ran').open('a').write(__file__ + '\\n')\n")
        (self.root / "switcher.py").write_text("# Fictitious plugin\n")
        (self.root / ".gitignore").write_text(".local/\nherdr-plugin.dev.toml\nauth.json\n")
        self.metadata("0.1.0", "- Initial release.")
        self.git("add", ".")
        self.git("commit", "-m", "chore: initialize fictitious repository")
        self.git("remote", "add", "origin", "https://github.com/release-fixture/account-hub.git")
        self.git("push", "origin", "main")
        fake = self.bin / "gh"
        fake.write_text("#!" + sys.executable + "\n" + r'''
import json, os, sys
from pathlib import Path
base = Path(os.environ["RELEASE_FIXTURE"])
args = sys.argv[1:]
with (base / "gh-calls.jsonl").open("a") as log:
    log.write(json.dumps(args) + "\n")
state = base / "release.json"
if args[:2] == ["auth", "status"]:
    if (base / "fail-auth").exists():
        sys.exit("simulated authentication failure")
    pass
elif args[0] == "api":
    print(json.dumps([[json.loads(state.read_text())] if state.exists() else []]))
elif args[:2] == ["release", "create"]:
    if (base / "fail-release").exists():
        sys.exit("simulated GitHub failure")
    notes = Path(args[args.index("--notes-file") + 1]).read_text()
    state.write_text(json.dumps({"tag_name": args[2], "body": notes,
                                "name": args[args.index("--title") + 1],
                                "draft": False, "prerelease": False}))
    print("https://github.com/release-fixture/account-hub/releases/tag/" + args[2])
else:
    sys.exit("unexpected GitHub command")
''')
        fake.chmod(0o755)

    def git(self, *args, check=True):
        return subprocess.run(["git", *args], cwd=self.root, env=self.env,
                              capture_output=True, text=True, check=check).stdout.strip()

    def metadata(self, version="0.1.1", notes=NOTES, extra=""):
        (self.root / "herdr-plugin.toml").write_text(
            'id = "fixture.account-hub"\nname = "Account Hub"\nversion = "' + version + '"\n')
        (self.root / "CHANGELOG.md").write_text(
            "# Changelog\n\n## " + version + " - 2026-10-10\n\n" + notes + "\n" + extra)

    def invoke(self, *args):
        return subprocess.run([sys.executable, "-B", "scripts/release.py", *args],
                              cwd=self.root, env=self.env, capture_output=True,
                              text=True, timeout=30)

    def prepared(self):
        self.metadata(extra="\n## 0.1.0 - 2026-10-09\n\n- Older release.\n")
        (self.root / "switcher.py").write_text("# Fictitious plugin with a fix\n")
        self.git("add", "herdr-plugin.toml", "CHANGELOG.md", "switcher.py")

    def committed(self):
        self.prepared()
        self.git("commit", "--cleanup=verbatim", "-m",
                 "fix(picker): preserve account history\n\n" + NOTES)
        return self.git("rev-parse", "HEAD")

    def test_single_invocation_commits_pushes_and_publishes_matching_notes(self):
        self.prepared()
        before = self.git("rev-parse", "HEAD")
        result = self.invoke("--version", "0.1.1", "--subject",
                             "fix(picker): preserve account history")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        sha = self.git("rev-parse", "HEAD")
        self.assertNotEqual(sha, before)
        self.assertEqual(self.git("log", "-1", "--format=%B"),
                         "fix(picker): preserve account history\n\n" + NOTES)
        self.assertEqual(self.git("--git-dir=" + str(self.remote), "rev-parse", "main"), sha)
        self.assertEqual(self.git("--git-dir=" + str(self.remote), "rev-parse", "v0.1.1^{}"), sha)
        release = json.loads((self.base / "release.json").read_text())
        self.assertEqual(release["name"], "Account Hub v0.1.1")
        self.assertEqual(release["body"], NOTES + "\n")
        self.assertEqual(len((self.base / "checks-ran").read_text().splitlines()), 2)

    def test_single_invocation_auth_failure_stops_before_commit(self):
        self.prepared()
        before = self.git("rev-parse", "HEAD")
        (self.base / "fail-auth").touch()
        result = self.invoke("--version", "0.1.1", "--subject",
                             "fix(picker): preserve account history")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertFalse((self.base / "checks-ran").exists())
        self.assertFalse((self.base / "release.json").exists())

    def test_single_invocation_failure_can_resume_without_another_commit(self):
        self.prepared()
        before = self.git("rev-parse", "HEAD")
        (self.base / "fail-release").touch()
        failed = self.invoke("--version", "0.1.1", "--subject",
                             "fix(picker): preserve account history")
        self.assertNotEqual(failed.returncode, 0)
        sha = self.git("rev-parse", "HEAD")
        self.assertNotEqual(sha, before)
        (self.base / "fail-release").unlink()
        retry = self.invoke("--version", "0.1.1", "--subject",
                            "fix(picker): preserve account history")
        self.assertEqual(retry.returncode, 0, retry.stderr + retry.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), sha)
        self.assertEqual(self.git("--git-dir=" + str(self.remote), "rev-parse", "v0.1.1^{}"), sha)

    def test_taken_version_stops_before_creating_a_commit(self):
        self.prepared()
        before = self.git("rev-parse", "HEAD")
        self.git("tag", "v0.1.1", before)
        result = self.invoke("--version", "0.1.1", "--subject",
                             "fix(picker): preserve account history")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already has a tag", result.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertFalse((self.base / "checks-ran").exists())
        self.assertFalse((self.base / "release.json").exists())

    def test_remote_work_stops_before_creating_a_commit(self):
        self.prepared()
        before = self.git("rev-parse", "HEAD")
        self.git("--git-dir=" + str(self.remote), "symbolic-ref", "HEAD", "refs/heads/main")
        other = self.base / "other"
        self.git("clone", str(self.remote), str(other))
        subprocess.run(["git", "-c", "user.name=Other", "-c", "user.email=other@example.invalid",
                        "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "chore: other work"],
                       cwd=other, env=self.env, check=True, capture_output=True)
        subprocess.run(["git", "push", "origin", "main"], cwd=other, env=self.env,
                       check=True, capture_output=True)
        remote_before = self.git("--git-dir=" + str(self.remote), "rev-parse", "main")
        result = self.invoke("--version", "0.1.1", "--subject",
                             "fix(picker): preserve account history")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Remote main", result.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual(self.git("--git-dir=" + str(self.remote), "rev-parse", "main"), remote_before)
        self.assertFalse((self.base / "checks-ran").exists())
        self.assertFalse((self.base / "release.json").exists())

    def test_retry_requires_the_exact_version_subject_and_notes(self):
        sha = self.committed()
        for version, subject in (("0.1.2", "fix(picker): preserve account history"),
                                 ("0.1.1", "fix(picker): an unrelated change")):
            with self.subTest(version=version, subject=subject):
                result = self.invoke("--version", version, "--subject", subject)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.git("rev-parse", "HEAD"), sha)
        self.git("commit", "--amend", "--cleanup=verbatim", "-m",
                 "fix(picker): preserve account history\n\n- Incorrect notes.")
        result = self.invoke("--version", "0.1.1", "--subject", "fix(picker): preserve account history")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.base / "gh-calls.jsonl").exists())

    def test_preview_is_read_only_and_extracts_only_the_matching_release(self):
        self.prepared()
        before = self.git("rev-parse", "HEAD")
        result = self.invoke("--dry-run", "--version", "0.1.1", "--subject",
                             "fix(picker): preserve account history")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(NOTES, result.stdout)
        self.assertNotIn("Older release", result.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertFalse((self.base / "gh-calls.jsonl").exists())
        self.assertFalse((self.base / "checks-ran").exists())

    def test_incomplete_arguments_never_start_the_pipeline(self):
        self.prepared()
        before = self.git("rev-parse", "HEAD")
        for arguments in ((), ("--version", "0.1.1"),
                          ("--subject", "fix(picker): preserve account history")):
            with self.subTest(arguments=arguments):
                result = self.invoke(*arguments)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertFalse((self.base / "gh-calls.jsonl").exists())

    def test_bad_or_duplicate_changelog_entries_never_commit(self):
        self.prepared()
        before = self.git("rev-parse", "HEAD")
        for extra, notes in (("\n## 0.1.1 - 2026-10-09\n\n- Duplicate.\n", NOTES),
                             ("", "### Fixed"),
                             ("\n## Unreleased\n\n- Unknown format.\n", NOTES)):
            with self.subTest(extra=extra, notes=notes):
                self.metadata(extra=extra, notes=notes)
                self.git("add", "CHANGELOG.md")
                result = self.invoke("--version", "0.1.1", "--subject", "fix(picker): keep history")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertFalse((self.base / "checks-ran").exists())
        self.assertFalse((self.base / "gh-calls.jsonl").exists())

    def test_mismatched_version_and_nonconventional_subject_never_commit(self):
        self.prepared()
        before = self.git("rev-parse", "HEAD")
        for version, subject in (("0.1.2", "fix(picker): keep history"),
                                 ("0.1.1", "Release Account Hub v0.1.1"),
                                 ("0.1.1", "fix(picker): keep history\nother subject")):
            with self.subTest(version=version, subject=subject):
                result = self.invoke("--version", version, "--subject", subject)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertFalse((self.base / "checks-ran").exists())
        self.assertFalse((self.base / "gh-calls.jsonl").exists())

    def test_wrong_changelog_version_and_invalid_dates_stop_before_checks(self):
        self.prepared()
        for heading in ("## 0.1.2 - 2026-10-10", "## 0.1.1 - 2026-02-30"):
            with self.subTest(heading=heading):
                (self.root / "CHANGELOG.md").write_text(heading + "\n\n" + NOTES + "\n")
                self.git("add", "CHANGELOG.md")
                result = self.invoke("--version", "0.1.1", "--subject", "fix(picker): keep history")
                self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.base / "checks-ran").exists())

    def test_feature_and_breaking_change_require_the_correct_version_bump(self):
        self.prepared()
        for subject in ("feat(picker): add controls", "fix(picker)!: change a contract"):
            with self.subTest(subject=subject):
                result = self.invoke("--version", "0.1.1", "--subject", subject)
                self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.base / "checks-ran").exists())

    def test_private_files_are_rejected_even_if_ignore_rules_are_removed(self):
        self.prepared()
        (self.root / ".gitignore").write_text("")
        (self.root / "auth.json").write_text('{"fake": true}')
        self.git("add", ".gitignore", "auth.json")
        result = self.invoke("--version", "0.1.1", "--subject", "fix(picker): keep history")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Private/generated file", result.stderr)
        self.assertFalse((self.base / "checks-ran").exists())

    def test_changes_made_during_checks_prevent_commit(self):
        self.prepared()
        before = self.git("rev-parse", "HEAD")
        (self.root / "tests/check_switcher.py").write_text(
            "from pathlib import Path\n"
            "Path(__file__).resolve().parents[1].joinpath('switcher.py').write_text('# Unreviewed change\\n')\n")
        self.git("add", "tests/check_switcher.py")
        result = self.invoke("--version", "0.1.1", "--subject", "fix(picker): keep history")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.git("rev-parse", "HEAD"), before)

    def test_unstaged_changes_private_files_and_failed_checks_stop_commit(self):
        self.prepared()
        before = self.git("rev-parse", "HEAD")
        (self.root / "switcher.py").write_text("# Unreviewed edit\n")
        result = self.invoke("--version", "0.1.1", "--subject", "fix(picker): keep history")
        self.assertNotEqual(result.returncode, 0)
        self.git("checkout", "--", "switcher.py")
        (self.root / ".local").mkdir()
        (self.root / ".local/auth.json").write_text('{"fake": true}')
        self.git("add", "-f", ".local/auth.json")
        result = self.invoke("--version", "0.1.1", "--subject", "fix(picker): keep history")
        self.assertNotEqual(result.returncode, 0)
        self.git("reset", "--", ".local/auth.json")
        (self.root / "tests/check_switcher.py").write_text("raise SystemExit(1)\n")
        self.git("add", "tests/check_switcher.py")
        result = self.invoke("--version", "0.1.1", "--subject", "fix(picker): keep history")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.git("rev-parse", "HEAD"), before)

    def test_publish_pushes_exact_commit_and_reuses_changelog_notes(self):
        sha = self.committed()
        result = self.invoke("--version", "0.1.1", "--subject", "fix(picker): preserve account history")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(self.git("--git-dir=" + str(self.remote), "rev-parse", "main"), sha)
        self.assertEqual(self.git("--git-dir=" + str(self.remote), "rev-parse", "v0.1.1^{}"), sha)
        release = json.loads((self.base / "release.json").read_text())
        self.assertEqual(release["body"], NOTES + "\n")
        self.assertEqual(release["name"], "Account Hub v0.1.1")
        again = self.invoke("--version", "0.1.1", "--subject", "fix(picker): preserve account history")
        self.assertEqual(again.returncode, 0, again.stderr + again.stdout)
        calls = [json.loads(line) for line in (self.base / "gh-calls.jsonl").read_text().splitlines()]
        self.assertEqual(sum(call[:2] == ["release", "create"] for call in calls), 1)

    def test_conflicting_tag_or_remote_main_prevents_publication(self):
        sha = self.committed()
        self.git("tag", "v0.1.1", "HEAD^")
        result = self.invoke("--version", "0.1.1", "--subject", "fix(picker): preserve account history")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.base / "release.json").exists())
        self.git("tag", "-d", "v0.1.1")
        self.git("push", "origin", "main")
        self.git("--git-dir=" + str(self.remote), "symbolic-ref", "HEAD", "refs/heads/main")
        other = self.base / "other"
        self.git("clone", str(self.remote), str(other))
        subprocess.run(["git", "-c", "user.name=Other", "-c", "user.email=other@example.invalid",
                        "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "chore: other work"],
                       cwd=other, env=self.env, check=True, capture_output=True)
        subprocess.run(["git", "push", "origin", "main"], cwd=other, env=self.env,
                       check=True, capture_output=True)
        remote_before = self.git("--git-dir=" + str(self.remote), "rev-parse", "main")
        result = self.invoke("--version", "0.1.1", "--subject", "fix(picker): preserve account history")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.git("--git-dir=" + str(self.remote), "rev-parse", "main"), remote_before)
        self.assertFalse(self.git("--git-dir=" + str(self.remote), "tag", "--list", "v0.1.1"))
        self.assertFalse((self.base / "release.json").exists())

    def test_retry_after_github_failure_keeps_the_same_commit_and_tag(self):
        sha = self.committed()
        (self.base / "fail-release").touch()
        failed = self.invoke("--version", "0.1.1", "--subject", "fix(picker): preserve account history")
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(self.git("rev-parse", "v0.1.1^{}"), sha)
        (self.base / "fail-release").unlink()
        retry = self.invoke("--version", "0.1.1", "--subject", "fix(picker): preserve account history")
        self.assertEqual(retry.returncode, 0, retry.stderr + retry.stdout)
        self.assertEqual(self.git("--git-dir=" + str(self.remote), "rev-parse", "v0.1.1^{}"), sha)

    def test_existing_release_metadata_and_dirty_checkout_are_not_published(self):
        sha = self.committed()
        (self.root / "unreviewed.txt").touch()
        dirty = self.invoke("--version", "0.1.1", "--subject", "fix(picker): preserve account history")
        self.assertNotEqual(dirty.returncode, 0)
        self.assertFalse((self.base / "gh-calls.jsonl").exists())
        (self.root / "unreviewed.txt").unlink()
        self.git("tag", "v0.1.1", sha)
        self.git("push", "origin", "main", "v0.1.1")
        (self.base / "release.json").write_text(json.dumps({
            "tag_name": "v0.1.1", "name": "Account Hub v0.1.1",
            "body": "Different published notes", "draft": False, "prerelease": False}))
        conflict = self.invoke("--version", "0.1.1", "--subject", "fix(picker): preserve account history")
        self.assertNotEqual(conflict.returncode, 0)
        self.assertEqual(json.loads((self.base / "release.json").read_text())["body"], "Different published notes")


if __name__ == "__main__":
    unittest.main()
