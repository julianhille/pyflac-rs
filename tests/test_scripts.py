"""The release and packaging helpers in scripts/. Skipped where scripts/ is not
checked out (the wheel tests use a sparse checkout of tests/ only)."""

import importlib.util
import io
import tarfile
import zipfile
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if not (SCRIPTS / "release.py").exists():
    pytest.skip("scripts/ is not checked out", allow_module_level=True)


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release = load("release")
check_dist = load("check_dist")
REPO = release.REPO

CHANGELOG = f"""# Changelog

## [Unreleased]

### Added

- Next thing.

## [1.1.0] - 2026-02-01

### Added

- A feature.

### Fixed

- A bug.

## [1.0.0] - 2026-01-01

### Added

- First release.

[Unreleased]: {REPO}/compare/v1.1.0...HEAD
[1.1.0]: {REPO}/compare/v1.0.0...v1.1.0
[1.0.0]: {REPO}/releases/tag/v1.0.0
"""


def test_version_order():
    order = [
        "0.9.9",
        "1.0.0-alpha.1",
        "1.0.0-alpha.2",
        "1.0.0-beta.1",
        "1.0.0-rc.1",
        "1.0.0-rc.10",
        "1.0.0",
        "1.0.1",
        "1.10.0",
    ]
    assert sorted(reversed(order), key=release.version_key) == order


@pytest.mark.parametrize("v", ["1.2.3", "0.0.0", "1.0.0-rc.1", "2.0.0-alpha.0", "10.20.30-beta.7"])
def test_valid_versions(v):
    release.check_version(v)


@pytest.mark.parametrize(
    "v",
    [
        "1.2",
        "01.2.3",
        "1.2.3-rc",
        "1.2.3-dev.1",
        "1.2.3-rc.01",
        "v1.2.3",
        "1.2.3-rc.1.2",
        "1.2.3+build",
    ],
)
def test_invalid_versions(v):
    with pytest.raises(release.ReleaseError):
        release.check_version(v)


def test_lint_accepts_good_changelog():
    release.lint(CHANGELOG)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (("### Fixed\n\n- A bug.", "### Fixed\n\n- A bug.\n\n### Fixed\n\n- Again."), "twice"),
        (
            ("### Added\n\n- A feature.\n\n### Fixed", "### Fixed\n\n- A feature.\n\n### Added"),
            "after",
        ),
        (("### Added\n\n- First release.", "### Misc\n\n- First release."), "is not one of"),
        (
            ("## [1.0.0] - 2026-01-01\n\n### Added\n", "## [1.0.0] - 2026-01-01\n\n- stray\n"),
            "bullet before",
        ),
    ],
)
def test_lint_finds_union_merge_damage(change, message):
    bad = CHANGELOG.replace(*change, 1)
    assert bad != CHANGELOG
    with pytest.raises(release.ReleaseError, match=message):
        release.lint(bad)


def test_section_body_and_errors():
    assert (
        release.section(CHANGELOG, "1.1.0") == "### Added\n\n- A feature.\n\n### Fixed\n\n- A bug."
    )
    assert release.section(CHANGELOG, "1.0.0") == "### Added\n\n- First release."
    with pytest.raises(release.ReleaseError, match="no"):
        release.section(CHANGELOG, "2.0.0")
    no_link = CHANGELOG.replace(f"[1.1.0]: {REPO}/compare/v1.0.0...v1.1.0\n", "")
    with pytest.raises(release.ReleaseError, match="link"):
        release.section(no_link, "1.1.0")
    empty = CHANGELOG.replace("### Added\n\n- First release.\n", "")
    with pytest.raises(release.ReleaseError, match="empty"):
        release.section(empty, "1.0.0")


def test_released_versions():
    assert release.released_versions(CHANGELOG) == ["1.1.0", "1.0.0"]


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG)
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "pyflac-rs"\nversion = "1.1.0"\n')
    (tmp_path / "Cargo.lock").write_text('[[package]]\nname = "pyflac-rs"\nversion = "1.1.0"\n')
    monkeypatch.setattr(release, "ROOT", tmp_path)
    return tmp_path


def test_check_writes_notes(repo):
    notes = repo / "notes.md"
    release.check("v1.1.0", str(notes))
    assert notes.read_text() == "### Added\n\n- A feature.\n\n### Fixed\n\n- A bug.\n"


@pytest.mark.parametrize(
    ("tag", "message"),
    [("1.1.0", "vX.Y.Z"), ("v1.0.0", "Cargo.toml has version 1.1.0"), ("v1.1", "X.Y.Z")],
)
def test_check_rejects(repo, tag, message):
    with pytest.raises(release.ReleaseError, match=message):
        release.check(tag)


def test_check_needs_matching_lock(repo):
    (repo / "Cargo.lock").write_text('[[package]]\nname = "pyflac-rs"\nversion = "1.0.0"\n')
    with pytest.raises(release.ReleaseError, match=r"Cargo\.lock"):
        release.check("v1.1.0")


def test_main_exit_codes(repo, capsys):
    assert release.main(["check", "v1.1.0"]) == 0
    assert release.main(["check", "v9.9.9"]) == 1
    assert "release:" in capsys.readouterr().err
    assert release.main(["nonsense"]) == 1
    assert release.main(["lint"]) == 0


# check_dist


@pytest.fixture
def dist_root(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    (root / "LICENSE").write_bytes(b"license\n")
    (root / "THIRD_PARTY_LICENSES.md").write_bytes(b"notices\n")
    monkeypatch.setattr(check_dist, "ROOT", root)
    out = tmp_path / "dist"
    out.mkdir()
    return root, out


def make_wheel(path, files):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)


def good_wheel_files(info="pyflac_rs-1.0.0.dist-info"):
    files = {
        f"{info}/METADATA": "Name: pyflac-rs\n"
        "License-File: LICENSE\nLicense-File: THIRD_PARTY_LICENSES.md\n"
    }
    files |= {f"{info}/licenses/LICENSE": "license\n"}
    files |= {f"{info}/licenses/THIRD_PARTY_LICENSES.md": "notices\n"}
    files |= {name: "" for name in check_dist.WHEEL_FILES}
    files["pyflac_rs/_native.abi3.so"] = "\0"
    return files


def make_sdist(path, files):
    with tarfile.open(path, "w:gz") as t:
        for name, data in files.items():
            info = tarfile.TarInfo(f"pyflac_rs-1.0.0/{name}")
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))


def good_sdist_files():
    return {
        "LICENSE": b"license\n",
        "THIRD_PARTY_LICENSES.md": b"notices\n",
        "Cargo.lock": b"",
        "rust-toolchain.toml": b"",
        "src/lib.rs": b"",
        "python/pyflac_rs/py.typed": b"",
    }


def test_check_dist_accepts_good_files(dist_root):
    _, out = dist_root
    make_wheel(out / "pyflac_rs-1.0.0-cp311-abi3-linux_x86_64.whl", good_wheel_files())
    make_sdist(out / "pyflac_rs-1.0.0.tar.gz", good_sdist_files())
    check_dist.check_wheel(out / "pyflac_rs-1.0.0-cp311-abi3-linux_x86_64.whl")
    check_dist.check_sdist(out / "pyflac_rs-1.0.0.tar.gz")


@pytest.mark.parametrize(
    "damage",
    [
        lambda f: f.pop("pyflac_rs-1.0.0.dist-info/licenses/THIRD_PARTY_LICENSES.md"),
        lambda f: f.update({"pyflac_rs-1.0.0.dist-info/licenses/LICENSE": "other\n"}),
        lambda f: f.pop("pyflac_rs/py.typed"),
        lambda f: f.pop("pyflac_rs-1.0.0.dist-info/METADATA"),
        lambda f: f.pop("pyflac_rs/_native.abi3.so"),
        lambda f: f.update({"tests/test_codec.py": ""}),
        lambda f: f.update(
            {"pyflac_rs-1.0.0.dist-info/METADATA": "Name: pyflac-rs\nLicense-File: LICENSE\n"}
        ),
    ],
    ids=[
        "no-notices",
        "license-differs",
        "no-typing-marker",
        "no-metadata",
        "no-extension",
        "has-tests",
        "metadata-lacks-license-file",
    ],
)
def test_check_dist_rejects_bad_wheels(dist_root, damage):
    _, out = dist_root
    files = good_wheel_files()
    damage(files)
    wheel = out / "pyflac_rs-1.0.0-cp311-abi3-linux_x86_64.whl"
    make_wheel(wheel, files)
    with pytest.raises(SystemExit, match="check_dist"):
        check_dist.check_wheel(wheel)


@pytest.mark.parametrize(
    "damage",
    [
        lambda f: f.pop("THIRD_PARTY_LICENSES.md"),
        lambda f: f.pop("Cargo.lock"),
        lambda f: f.update({"target/release/x.so": b""}),
    ],
    ids=["no-notices", "no-lock", "build-output"],
)
def test_check_dist_rejects_bad_sdists(dist_root, damage):
    _, out = dist_root
    files = good_sdist_files()
    damage(files)
    sdist = out / "pyflac_rs-1.0.0.tar.gz"
    make_sdist(sdist, files)
    with pytest.raises(SystemExit, match="check_dist"):
        check_dist.check_sdist(sdist)


def test_repository_changelog_is_well_formed():
    release.lint((SCRIPTS.parent / "CHANGELOG.md").read_text())


def test_shipped_notices_cover_the_codec():
    notices = (SCRIPTS.parent / "THIRD_PARTY_LICENSES.md").read_text()
    assert "libflac-rs" in notices and "BSD-3-Clause" in notices
    assert "pyo3" in notices
