# SPDX-License-Identifier: 0BSD
"""Release helper.

``python scripts/release.py prepare X.Y.Z`` bumps the version in Cargo.toml and
Cargo.lock (maturin takes the package version from Cargo.toml), turns
``## [Unreleased]`` in CHANGELOG.md into ``## [X.Y.Z] - today`` (with a fresh
empty Unreleased section and updated compare links) and commits. It runs on a
fresh ``release/vX.Y.Z`` branch at origin/main. It never tags or pushes; it
prints the commands for that.

``python scripts/release.py check vX.Y.Z [NOTES]`` is run by the release
workflow: the tag must match every version, and CHANGELOG.md must have a
dated, non-empty section with a link for it. The section body is written to
NOTES (the GitHub release text).

``python scripts/release.py lint`` checks the structure of CHANGELOG.md alone.
CI runs it on every push: CHANGELOG.md merges with ``merge=union``
(.gitattributes), which never conflicts but can duplicate or reorder the
``### `` headings, or leave bullets under the wrong one.

Versions are SemVer (``1.2.0-rc.1``) in the tag, Cargo.toml and the changelog;
the wheels carry the PEP 440 form (``1.2.0rc1``) that maturin derives.
"""

from __future__ import annotations

import datetime
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = "https://github.com/julianhille/pyflac-rs"
NUM = r"(?:0|[1-9]\d*)"
PRE = r"(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
SEMVER = re.compile(rf"{NUM}\.{NUM}\.{NUM}(?:-{PRE}(?:\.{PRE})*)?")
# Prereleases maturin can express in PEP 440: alpha, beta and rc with a number.
PEP440_PRE = re.compile(r"(alpha|beta|rc)\.(0|[1-9]\d*)")
TYPES = ["Added", "Changed", "Deprecated", "Removed", "Fixed", "Security"]
EDITED = ["CHANGELOG.md", "Cargo.toml", "Cargo.lock"]


class ReleaseError(Exception):
    """A problem to report to the user (no traceback)."""


def read(name: str) -> str:
    return (ROOT / name).read_text()


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()


def version_key(v: str) -> tuple:
    core, _, pre = v.partition("-")
    nums = tuple(int(n) for n in core.split("."))
    if not pre:
        # A release sorts after its prereleases.
        return (nums, 1, ())
    ids = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split("."))
    return (nums, 0, ids)


def released_versions(log: str) -> list[str]:
    return re.findall(r"^## \[([^\]]+)\] - \d{4}-\d{2}-\d{2}$", log, re.M)


def versions() -> dict[str, str | None]:
    cargo = re.search(r'^version = "([^"]+)"', read("Cargo.toml"), re.M)
    lock = re.search(
        r'\[\[package\]\]\nname = "pyflac-rs"\nversion = "([^"]+)"', read("Cargo.lock")
    )
    return {
        "Cargo.toml": cargo and cargo[1],
        "Cargo.lock": lock and lock[1],
    }


def lint(log: str) -> None:
    """Each ``## `` section lists its ``### `` headings from TYPES, in that
    order and at most once each, and every bullet is under one."""
    section = None
    last = -1

    def where(line: int, msg: str):
        raise ReleaseError(
            f"CHANGELOG.md:{line}: {msg} (a merge=union artefact? see .gitattributes)"
        )

    for line, text in enumerate(log.split("\n"), 1):
        if text.startswith("## "):
            section, last = text[3:], -1
        elif section is None:
            continue
        elif text.startswith("### "):
            kind = text[4:]
            if kind not in TYPES:
                where(line, f'"{text}" is not one of {", ".join(TYPES)}')
            index = TYPES.index(kind)
            if index == last:
                where(line, f'{section} has "{text}" twice')
            if index < last:
                where(line, f'{section} has "{text}" after "### {TYPES[last]}"')
            last = index
        elif re.match(r"[-*] ", text) and last < 0:
            where(line, f'{section} has a bullet before its first "### " heading')


def section(log: str, v: str) -> str:
    head = re.search(rf"^## \[{re.escape(v)}\] - (\d{{4}}-\d{{2}}-\d{{2}})$", log, re.M)
    if head is None:
        raise ReleaseError(f'CHANGELOG.md has no "## [{v}] - YYYY-MM-DD" headline')
    rest = log[head.end() :]
    end = re.search(r"^## ", rest, re.M)
    lines = (rest[: end.start()] if end else rest).split("\n")
    version_link = re.compile(rf"^\[(Unreleased|\d[^\]]*)\]: {re.escape(REPO)}/")
    while lines and (not lines[-1].strip() or version_link.match(lines[-1])):
        lines.pop()
    body = "\n".join(lines).strip()
    if not body:
        raise ReleaseError(f"the CHANGELOG.md section for {v} is empty")
    if not re.search(rf"^\[{re.escape(v)}\]: {re.escape(REPO)}/", log, re.M):
        raise ReleaseError(f'CHANGELOG.md has no link line "[{v}]: {REPO}/…"')
    return body


def check_version(v: str) -> None:
    if not SEMVER.fullmatch(v):
        raise ReleaseError(f"{v} is not X.Y.Z[-pre]")
    _, _, pre = v.partition("-")
    if pre and not PEP440_PRE.fullmatch(pre):
        raise ReleaseError(f"{v}: use a prerelease of the form -alpha.N, -beta.N or -rc.N")


def check(tag: str, notes: str | None = None) -> None:
    if not tag.startswith("v"):
        raise ReleaseError(f"{tag} is not vX.Y.Z[-pre]")
    v = tag[1:]
    check_version(v)
    for file, got in versions().items():
        if got != v:
            raise ReleaseError(f"{file} has version {got or '(none)'}, the tag says {v}")
    log = read("CHANGELOG.md")
    lint(log)
    body = section(log, v)
    if notes is not None:
        Path(notes).write_text(body + "\n")
    print(f"release: {tag} is consistent")


def prepare(v: str) -> None:
    check_version(v)
    if git("status", "--porcelain", "--untracked-files=no"):
        raise ReleaseError("the working tree has uncommitted changes")
    branch = f"release/v{v}"
    if git("rev-parse", "--abbrev-ref", "HEAD") != branch:
        raise ReleaseError(f"releases are prepared on {branch}: git switch -c {branch} origin/main")
    git("fetch", "--quiet", "--tags", "origin", "main")
    if git("rev-parse", "HEAD") != git("rev-parse", "origin/main"):
        raise ReleaseError(f"{branch} is not at origin/main; start it from there")
    log = read("CHANGELOG.md")
    lint(log)
    released = released_versions(log)
    prev = max(released, key=version_key, default=None)
    if prev is not None and version_key(v) <= version_key(prev):
        raise ReleaseError(f"{v} is not newer than {prev}")
    if prev is not None:
        try:
            git("merge-base", "--is-ancestor", f"refs/tags/v{prev}", "HEAD")
        except subprocess.CalledProcessError:
            raise ReleaseError(
                f"tag v{prev} is missing or not in main's history; merge release/v{prev} "
                "into main with a merge commit (not a squash) first"
            ) from None
    if v in released or re.search(rf"^\[{re.escape(v)}\]: ", log, re.M):
        raise ReleaseError(f"CHANGELOG.md already has {v}")
    try:
        git("rev-parse", "--quiet", "--verify", f"refs/tags/v{v}")
        raise ReleaseError(f"tag v{v} already exists")
    except subprocess.CalledProcessError:
        pass
    if not re.search(r"^## \[Unreleased\]$", log, re.M):
        raise ReleaseError('CHANGELOG.md has no "## [Unreleased]" section')
    if not re.search(r"^\[Unreleased\]: ", log, re.M):
        raise ReleaseError('CHANGELOG.md has no "[Unreleased]: …" link')
    # The local date: the day the release is made where it is made.
    date = datetime.date.today().isoformat()
    compare = f"{REPO}/compare/v{prev}...v{v}" if prev else f"{REPO}/releases/tag/v{v}"
    log = re.sub(
        r"^\[Unreleased\]: .*$",
        lambda _: f"[Unreleased]: {REPO}/compare/v{v}...HEAD\n[{v}]: {compare}",
        log,
        count=1,
        flags=re.M,
    )
    log = re.sub(
        r"^## \[Unreleased\]$",
        lambda _: f"## [Unreleased]\n\n## [{v}] - {date}",
        log,
        count=1,
        flags=re.M,
    )
    section(log, v)
    try:
        (ROOT / "CHANGELOG.md").write_text(log)
        cargo = re.sub(
            r'^version = "[^"]+"', f'version = "{v}"', read("Cargo.toml"), count=1, flags=re.M
        )
        (ROOT / "Cargo.toml").write_text(cargo)
        subprocess.run(["cargo", "update", "--workspace", "--quiet"], cwd=ROOT, check=True)
        check(f"v{v}")
        git("commit", "--quiet", "-am", f"Release {v}")
    except BaseException:
        # Leave the tree as it was (it was clean before).
        git("checkout", "--", *EDITED)
        raise
    print(
        "release: committed. Review, then:\n"
        f"  git tag -a v{v} -m v{v}\n"
        f"  git push origin release/v{v} v{v}\n"
        f"and merge release/v{v} into main with a merge commit (not a squash)."
    )


def main(argv: list[str]) -> int:
    try:
        match argv:
            case ["check", tag]:
                check(tag)
            case ["check", tag, notes]:
                check(tag, notes)
            case ["prepare", v]:
                prepare(v)
            case ["lint"]:
                lint(read("CHANGELOG.md"))
            case _:
                raise ReleaseError(
                    "usage: release.py prepare X.Y.Z | release.py check vX.Y.Z [NOTES] | "
                    "release.py lint"
                )
    except ReleaseError as e:
        print(f"release: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
