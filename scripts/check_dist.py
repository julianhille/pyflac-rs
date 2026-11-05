# SPDX-License-Identifier: 0BSD
"""Checks built distributions: every wheel and sdist in DIR ships LICENSE and
THIRD_PARTY_LICENSES.md identical to the repository's, the typing marker and
stubs, and nothing from tests/ or build output.

Usage: python scripts/check_dist.py DIR
"""

from __future__ import annotations

import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LICENSES = ["LICENSE", "THIRD_PARTY_LICENSES.md"]
WHEEL_FILES = [
    "pyflac_rs/__init__.py",
    "pyflac_rs/metadata.py",
    "pyflac_rs/py.typed",
    "pyflac_rs/_native.pyi",
]


def fail(msg: str) -> None:
    raise SystemExit(f"check_dist: {msg}")


def check_wheel(path: Path) -> None:
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        info = next((n.split("/")[0] for n in names if n.endswith(".dist-info/METADATA")), None)
        if info is None:
            fail(f"{path.name}: no METADATA")
        for lic in LICENSES:
            name = f"{info}/licenses/{lic}"
            if name not in names:
                fail(f"{path.name}: {name} is missing")
            if z.read(name) != (ROOT / lic).read_bytes():
                fail(f"{path.name}: {name} differs from the repository's")
            if f"License-File: {lic}" not in z.read(f"{info}/METADATA").decode():
                fail(f"{path.name}: METADATA has no License-File: {lic}")
        for name in WHEEL_FILES:
            if name not in names:
                fail(f"{path.name}: {name} is missing")
        if not any(n.startswith("pyflac_rs/_native.") and not n.endswith(".pyi") for n in names):
            fail(f"{path.name}: no compiled extension")
        if any(n.startswith("tests/") for n in names):
            fail(f"{path.name}: contains tests")
    print(f"  ok {path.name}")


def check_sdist(path: Path) -> None:
    with tarfile.open(path) as t:
        names = t.getnames()
        top = names[0].split("/")[0]
        for lic in LICENSES:
            try:
                member = t.extractfile(f"{top}/{lic}")
            except KeyError:
                member = None
            if member is None or member.read() != (ROOT / lic).read_bytes():
                fail(f"{path.name}: {lic} is missing or differs")
        for need in [
            "Cargo.lock",
            "rust-toolchain.toml",
            "src/lib.rs",
            "python/pyflac_rs/py.typed",
        ]:
            if f"{top}/{need}" not in names:
                fail(f"{path.name}: {need} is missing")
        if any("/target/" in n or "/.venv/" in n for n in names):
            fail(f"{path.name}: contains build output")
    print(f"  ok {path.name}")


def main() -> None:
    if len(sys.argv) != 2:
        fail("usage: check_dist.py DIR")
    found = sorted(Path(sys.argv[1]).iterdir())
    wheels = [p for p in found if p.suffix == ".whl"]
    sdists = [p for p in found if p.name.endswith(".tar.gz")]
    if not wheels and not sdists:
        fail(f"no wheels or sdists in {sys.argv[1]}")
    for p in wheels:
        check_wheel(p)
    for p in sdists:
        check_sdist(p)


if __name__ == "__main__":
    main()
