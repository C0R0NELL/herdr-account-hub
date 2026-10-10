#!/usr/bin/env python3
"""Validate, commit, push, and publish a release from the manifest and changelog."""
import argparse
from datetime import date
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import tomllib


ROOT = Path(__file__).resolve().parents[1]
VERSION = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
SUBJECT = re.compile(
    r"(?P<type>feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)"
    r"(?:\([a-z0-9][a-z0-9._/-]*\))?(?P<breaking>!)?: [^\r\n]+")
SECRETS = re.compile(
    rb"(?:sk-[A-Za-z0-9_-]{32,}|gh[pousr]_[A-Za-z0-9]{30,}|"
    rb"github_pat_[A-Za-z0-9_]{30,}|eyJ[A-Za-z0-9_-]{20,}\."
    rb"[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)")


class ReleaseError(Exception):
    pass


def command(*args, input=None, check=True, capture=True):
    result = subprocess.run(args, cwd=ROOT, input=input, text=True,
                            capture_output=capture)
    if check and result.returncode:
        detail = (result.stderr or result.stdout or "").strip() if capture else ""
        raise ReleaseError(f"{args[0]} failed ({result.returncode})" + (": " + detail if detail else ""))
    return result


def git(*args):
    return command("git", *args).stdout.strip()


def release(ref=None):
    def read(filename):
        if ref is not None:
            return command("git", "show", ref + ":" + filename).stdout
        return (ROOT / filename).read_text()

    manifest = tomllib.loads(read("herdr-plugin.toml"))
    version = manifest.get("version", "")
    if not isinstance(version, str) or not re.fullmatch(VERSION, version):
        raise ReleaseError("The official manifest version must use MAJOR.MINOR.PATCH.")
    if not manifest.get("name") or not manifest.get("id"):
        raise ReleaseError("The official manifest must include its stable name and ID.")
    changelog = read("CHANGELOG.md")
    headings = list(re.finditer(r"(?m)^## .*?$", changelog))
    seen = set()
    for heading in headings:
        parsed = re.fullmatch(r"## (" + VERSION + r") - (\d{4}-\d{2}-\d{2})", heading[0])
        if not parsed:
            raise ReleaseError("Changelog headings must use '## MAJOR.MINOR.PATCH - YYYY-MM-DD'.")
        date.fromisoformat(parsed[2])
        if parsed[1] in seen:
            raise ReleaseError("Duplicate changelog version: " + parsed[1])
        seen.add(parsed[1])
    if not headings or not headings[0][0].startswith("## " + version + " - "):
        raise ReleaseError("The first changelog entry must match the official manifest version.")
    end = headings[1].start() if len(headings) > 1 else len(changelog)
    notes = changelog[headings[0].end():end].strip()
    if not re.search(r"(?m)^- \S", notes):
        raise ReleaseError("The matching changelog entry must contain nonempty bullet notes.")
    return manifest, notes


def validate_subject(subject):
    if not subject or subject != subject.strip() or not SUBJECT.fullmatch(subject):
        raise ReleaseError("Use an English Conventional Commit subject, e.g. 'fix(picker): preserve history'.")
    return SUBJECT.fullmatch(subject)


def branch():
    if git("branch", "--show-current") != "main":
        raise ReleaseError("Release operations require the main branch.")


def clean(staged=False):
    if staged:
        dirty = command("git", "diff", "--quiet", check=False).returncode
        untracked = git("ls-files", "--others", "--exclude-standard")
    else:
        dirty = bool(git("status", "--porcelain", "--untracked-files=all"))
        untracked = ""
    if dirty or untracked:
        raise ReleaseError("Review and stage all intended changes first; release operations require no unstaged or untracked files.")


def check_files(ref):
    args = ("ls-files", "-z") if ref == "" else ("ls-tree", "-r", "--name-only", "-z", ref)
    filenames = command("git", *args).stdout
    ignored = command("git", "check-ignore", "--no-index", "-z", "--stdin",
                      input=filenames, check=False)
    if ignored.returncode not in (0, 1):
        raise ReleaseError("Could not check Git exclusions.")
    if ignored.stdout:
        raise ReleaseError("Ignored/private files are tracked: " + ", ".join(ignored.stdout.rstrip("\0").split("\0")))
    for filename in filenames.rstrip("\0").split("\0"):
        parts = Path(filename).parts
        if (parts[0] in {".local", ".codex", ".claude", "profiles", "backups", "rotated"}
                or filename == "herdr-plugin.dev.toml"
                or re.fullmatch(r"(?:auth|credentials|\.credentials|\.claude)\.json(?:\..*)?", parts[-1])
                or parts[-1] == ".env" or parts[-1].startswith(".env.") and parts[-1] != ".env.example"):
            raise ReleaseError("Private/generated file is tracked: " + filename)
        result = subprocess.run(["git", "show", ref + ":" + filename], cwd=ROOT,
                                capture_output=True, check=True)
        if SECRETS.search(result.stdout):
            raise ReleaseError("Possible credential in tracked file: " + filename)


def checks():
    for path in ("tests/check_switcher.py", "tests/check_managed_upgrade.py"):
        print("Checking " + path, flush=True)
        command(sys.executable, "-B", path, capture=False)


def commit(version, subject):
    branch()
    clean(staged=True)
    parsed = validate_subject(subject)
    manifest, notes = release("")
    if manifest["version"] != version:
        raise ReleaseError("The explicitly approved version differs from the staged manifest.")
    old, _ = release("HEAD")
    if manifest["id"] != old["id"] or manifest["name"] != old["name"]:
        raise ReleaseError("Keep the official manifest ID and name stable.")
    previous = tuple(map(int, old["version"].split(".")))
    target = tuple(map(int, version.split(".")))
    changed = git("diff", "--cached", "--name-only").splitlines()
    if not changed:
        raise ReleaseError("Nothing is staged; the script never stages files automatically.")
    if target < previous:
        raise ReleaseError("The release version cannot decrease.")
    behavior = "switcher.py" in changed or {k: v for k, v in manifest.items() if k != "version"} != {
        k: v for k, v in old.items() if k != "version"}
    if behavior and target == previous:
        raise ReleaseError("Plugin changes require an approved version bump.")
    if parsed["type"] == "feat" and target[:2] <= previous[:2]:
        raise ReleaseError("Feature releases require at least a minor version bump.")
    if parsed["breaking"] and (target[:2] <= previous[:2] if previous[0] == 0 else target[0] <= previous[0]):
        raise ReleaseError("Breaking changes require a minor bump before 1.0 or a major bump afterwards.")
    check_files("")
    head = git("rev-parse", "HEAD")
    staged = git("diff", "--cached", "--binary")
    publication(manifest)
    checks()
    branch()
    clean(staged=True)
    if git("rev-parse", "HEAD") != head or git("diff", "--cached", "--binary") != staged:
        raise ReleaseError("HEAD or staged changes changed during checks; review and retry.")
    # Structured stdin preserves literal newlines and avoids shell interpolation.
    command("git", "commit", "--cleanup=verbatim", "--file=-",
            input=subject + "\n\n" + notes + "\n", capture=False)


def repository():
    sources = []
    for key in ("url", "pushurl"):
        configured = command("git", "config", "--get-all", "remote.origin." + key, check=False)
        if configured.returncode not in (0, 1):
            raise ReleaseError("Could not read origin configuration.")
        urls = configured.stdout.strip().splitlines()
        if key == "pushurl" and not urls:
            sources.append(sources[0])
            continue
        if len(urls) != 1:
            raise ReleaseError("Origin must have exactly one fetch URL and one push URL.")
        url = urls[0].removesuffix(".git")
        parsed = re.fullmatch(r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
                              r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", url)
        if not parsed:
            raise ReleaseError("Origin must point directly to a GitHub repository without embedded credentials.")
        sources.append(parsed[1])
    if sources[0].lower() != sources[1].lower():
        raise ReleaseError("Origin fetch and push must point to the same GitHub repository.")
    return sources[0]


def remote_refs(tag):
    refs = command("git", "ls-remote", "origin", "refs/heads/main",
                   "refs/tags/" + tag, "refs/tags/" + tag + "^{}").stdout
    return dict((ref, sha) for sha, ref in (line.split() for line in refs.splitlines()))


def publication(manifest, sha=None, notes=None):
    """Check publication prerequisites before creating a commit or pushing it."""
    tag = "v" + manifest["version"]
    title = manifest["name"] + " " + tag
    repo = repository()
    command("gh", "auth", "status", "--hostname", "github.com")
    pages = json.loads(command("gh", "api", "repos/" + repo + "/releases",
                               "--paginate", "--slurp").stdout)
    existing = next((item for page in pages for item in page if item["tag_name"] == tag), None)
    refs = remote_refs(tag)
    remote_tag = refs.get("refs/tags/" + tag + "^{}", refs.get("refs/tags/" + tag))
    local = command("git", "rev-parse", "--verify", "--quiet", "refs/tags/" + tag + "^{commit}", check=False)
    if sha is None and (remote_tag or local.returncode == 0 or existing):
        raise ReleaseError("This version already has a tag or GitHub release; prepare a new version before committing.")
    if (remote_tag and remote_tag != sha) or (local.returncode == 0 and local.stdout.strip() != sha):
        raise ReleaseError("The existing release tag points to a different commit; it will not be moved.")
    if existing:
        if (remote_tag != sha or existing.get("draft") or existing.get("prerelease")
                or existing["name"] != title or (existing.get("body") or "").strip() != notes):
            raise ReleaseError("This GitHub release already exists with different metadata; it will not be overwritten.")
    elif refs.get("refs/heads/main"):
        ancestor = command("git", "merge-base", "--is-ancestor", refs["refs/heads/main"],
                           sha or "HEAD", check=False)
        if ancestor.returncode:
            raise ReleaseError("Remote main is ahead, diverged, or not fetched; reconcile it before releasing.")
    return repo, tag, title, existing, remote_tag, local.returncode == 0


def publish(version, run_checks=True):
    branch()
    clean()
    manifest, notes = release("HEAD")
    if manifest["version"] != version:
        raise ReleaseError("The explicitly approved version differs from the committed manifest.")
    check_files("HEAD")
    sha = git("rev-parse", "HEAD")
    repo, tag, title, existing, remote_tag, local_tag = publication(manifest, sha, notes)
    if existing:
        print(title + " is already published; nothing changed.")
        return
    if run_checks:
        checks()
    branch()
    clean()
    if git("rev-parse", "HEAD") != sha:
        raise ReleaseError("HEAD changed during checks; review and retry.")
    push = [sha + ":refs/heads/main"]
    if not remote_tag:
        if not local_tag:
            command("git", "tag", "-a", tag, sha, "--cleanup=verbatim", "--file=-",
                    input=title + "\n\n" + notes + "\n")
        push.append("refs/tags/" + tag + ":refs/tags/" + tag)
    # No force; main and a new tag either both move or neither moves.
    command("git", "push", "--atomic", "--no-follow-tags", "origin", *push, capture=False)
    refs = remote_refs(tag)
    if (refs.get("refs/heads/main") != sha
            or refs.get("refs/tags/" + tag + "^{}", refs.get("refs/tags/" + tag)) != sha):
        raise ReleaseError("The remote commit/tag changed; stop and inspect before publishing.")
    with tempfile.TemporaryDirectory(prefix="account-hub-release-") as temporary:
        path = Path(temporary) / "notes.md"
        path.write_text(notes + "\n")
        command("gh", "release", "create", tag, "--repo", repo, "--verify-tag",
                "--title", title, "--notes-file", str(path), capture=False)


def run_pipeline(version, subject):
    validate_subject(subject)
    branch()
    clean(staged=True)
    changed = bool(git("diff", "--cached", "--name-only"))
    if changed:
        commit(version, subject)
    manifest, notes = release("HEAD")
    if manifest["version"] != version:
        raise ReleaseError("The explicitly approved version differs from the committed manifest.")
    if git("log", "-1", "--format=%B") != subject + "\n\n" + notes:
        raise ReleaseError("No matching release commit: stage the intended release changes or retry with its original subject.")
    publish(version, run_checks=not changed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", help="version explicitly agreed with the user; required unless --dry-run")
    parser.add_argument("--subject", help="English Conventional Commit subject; required unless --dry-run")
    parser.add_argument("--dry-run", action="store_true", help="preview only, without writes or network calls")
    args = parser.parse_args()
    if not args.dry_run and (not args.version or not args.subject):
        parser.error("--version and --subject are required; this command commits, pushes, and publishes")
    try:
        if args.dry_run:
            manifest, notes = release()
            if args.version and args.version != manifest["version"]:
                raise ReleaseError("The preview version differs from the official manifest.")
            if args.subject:
                validate_subject(args.subject)
            print("Preview only; no commits, tags, network calls, or publication.\n")
            print("Version: " + manifest["version"])
            print("Tag: v" + manifest["version"])
            print("Release: " + manifest["name"] + " v" + manifest["version"])
            if args.subject:
                print("Commit:\n" + args.subject + "\n")
            print(notes)
        else:
            run_pipeline(args.version, args.subject)
    except (ReleaseError, OSError, ValueError, subprocess.CalledProcessError) as error:
        print("release: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
