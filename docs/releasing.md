<!-- SPDX-License-Identifier: 0BSD -->
# Releasing

Releases are tags `vX.Y.Z` (or `vX.Y.Z-alpha.N`, `-beta.N`, `-rc.N`) made from
a `release/vX.Y.Z` branch. The changelog follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions are
[SemVer](https://semver.org/). The wheels carry the PEP 440 form of the version
(`1.2.0rc1`).

1. Start the branch at `origin/main`:

    ```bash
    git fetch origin
    git switch -c release/v0.2.0 origin/main
    ```

2. Bump the version and date the changelog. This edits `Cargo.toml`,
   `Cargo.lock` and `CHANGELOG.md` and commits them:

    ```bash
    python scripts/release.py prepare 0.2.0
    ```

3. Review, then tag and push:

    ```bash
    git tag -a v0.2.0 -m v0.2.0
    git push origin release/v0.2.0 v0.2.0
    ```

4. Merge `release/v0.2.0` into `main` with a merge commit, not a squash, so the
   tag stays in `main`'s history.

The tag starts `.github/workflows/release.yml`:

1. `release.py check` makes sure the tag, `Cargo.toml`, `Cargo.lock` and the
   changelog agree. The changelog section becomes the release notes.
2. The whole CI suite runs on the tag, at the full test tier.
3. Wheels are built for manylinux and musllinux (x86_64, aarch64), macOS
   (x86_64, arm64) and Windows (x64, arm64). That is one abi3 wheel per
   platform for CPython 3.11–3.15, plus free-threaded wheels for 3.14t and
   3.15t. An sdist is built too.
4. Every wheel's license files are checked, and the test suite runs against
   the installed wheels on each Python on each OS.
5. The files are published to PyPI by
   [trusted publishing](https://docs.pypi.org/trusted-publishers/) from the
   `pypi` environment, with attestations.
6. A GitHub release gets the wheels, the sdist, `SHA256SUMS` and
   `THIRD_PARTY_LICENSES.md`. Prerelease tags are marked as prereleases.

Read the Docs builds the `pyflac-rs` project from `.readthedocs.yaml` on every
push.
