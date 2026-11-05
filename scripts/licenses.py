# SPDX-License-Identifier: 0BSD
"""License notices of everything compiled into the extension module.

``python scripts/licenses.py notices [OUT]`` writes THIRD_PARTY_LICENSES.md:
a table of the Rust crates linked into ``pyflac_rs/_native`` (normal
dependencies on every wheel target, not build or dev dependencies, and not
proc macros, which only run at compile time), the parts of the Rust standard
library linked with them, then each notice file verbatim. The wheels and the
sdist ship it in ``.dist-info/licenses/`` (``license-files`` in pyproject.toml).

``python scripts/licenses.py page [OUT]`` writes docs/third-party.md, the same
list with links for Read the Docs.

Both files are committed, so neither the docs build nor a wheel build needs
network access; CI regenerates them and fails when they are out of date. Run
``cargo fetch --locked`` first, so every crate's sources are in the registry.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = "https://github.com/julianhille/pyflac-rs"

# The wheel targets (the matrix in .github/workflows/release.yml). The crates
# are the union over all of them, so the notices are the same in every wheel.
TARGETS = [
    "x86_64-unknown-linux-gnu",
    "aarch64-unknown-linux-gnu",
    "x86_64-unknown-linux-musl",
    "aarch64-unknown-linux-musl",
    "x86_64-apple-darwin",
    "aarch64-apple-darwin",
    "x86_64-pc-windows-msvc",
    "aarch64-pc-windows-msvc",
]

# The license expressions whose conditions the notices meet. A crate with any
# other expression fails the script, so that a new license gets a decision.
# Kept in step with deny.toml.
KNOWN = {
    "0BSD",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "MIT",
    "Apache-2.0",
    "MIT OR Apache-2.0",
    "(MIT OR Apache-2.0) AND Unicode-3.0",
    "Apache-2.0 OR MIT",
    "Apache-2.0 WITH LLVM-exception",
}

NOTICE_FILE = re.compile(r"^(licen[cs]e|copying|copyright|notice|authors)", re.I)

# The Rust release whose texts are in scripts/std-licenses/. Change it only
# after comparing the texts with the new release (see the README.md there).
STD_TEXTS_RELEASE = "1.98.1"


@dataclass(frozen=True)
class StdPart:
    name: str
    license: str
    chosen: str | None
    repository: str
    notices: tuple[tuple[str, str], ...]
    """(title, file in scripts/std-licenses/)"""


# cargo does not list the standard library, so the parts linked into a native
# cdylib are written down here. The system C library and the Python runtime
# are not linked in statically; the wheels use those of the machine.
STD_PARTS = [
    StdPart(
        "core, alloc, std",
        "(MIT OR Apache-2.0) AND Unicode-3.0",
        "MIT",
        "https://github.com/rust-lang/rust",
        (("LICENSE-MIT", "rust-LICENSE-MIT"), ("LICENSES/Unicode-3.0.txt", "Unicode-3.0.txt")),
    ),
    StdPart(
        "compiler_builtins, libm",
        "MIT AND Apache-2.0 WITH LLVM-exception AND (MIT OR Apache-2.0)",
        None,
        "https://github.com/rust-lang/compiler-builtins",
        (
            ("LICENSE.txt", "compiler-builtins-LICENSE.txt"),
            ("libm/LICENSE.txt", "libm-LICENSE.txt"),
        ),
    ),
]

# Sources that are copied into this repository rather than depended on.
VENDORED = [
    (
        "src/ogg.rs",
        "libflac-rs",
        "the Ogg page writer of `src/ogg.rs`, with the reader removed",
    ),
]


@dataclass(frozen=True)
class Crate:
    name: str
    version: str
    license: str | None
    repository: str | None
    directory: Path
    license_file: str | None

    def notice_files(self) -> list[str]:
        """License texts, NOTICE and AUTHORS files (and every file in a
        directory with such a name, as in the REUSE layout), plus the
        manifest's ``license-file``."""
        files = []
        for entry in self.directory.iterdir():
            if not NOTICE_FILE.match(entry.name):
                continue
            if entry.is_dir():
                files += [
                    p.relative_to(self.directory).as_posix()
                    for p in entry.rglob("*")
                    if p.is_file()
                ]
            elif entry.is_file():
                files.append(entry.name)
        files.sort()
        if self.license_file and self.license_file not in files:
            files.append(self.license_file)
        if not files:
            raise SystemExit(f"{self.name} {self.version}: no license file in {self.directory}")
        return files


def cargo_metadata(target: str) -> dict:
    out = subprocess.run(
        ["cargo", "metadata", "--format-version", "1", "--locked", "--filter-platform", target],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return json.loads(out)


def shipped_crates() -> list[Crate]:
    """The crates linked into the extension on any wheel target, by name."""
    found: dict[str, Crate] = {}
    for target in TARGETS:
        meta = cargo_metadata(target)
        nodes = {n["id"]: n for n in meta["resolve"]["nodes"]}
        packages = {p["id"]: p for p in meta["packages"]}
        seen: set[str] = set()
        todo = [meta["resolve"]["root"]]
        while todo:
            for dep in nodes[todo.pop()]["deps"]:
                pkg = dep["pkg"]
                if pkg in seen or not any(k["kind"] is None for k in dep["dep_kinds"]):
                    continue
                if any("proc-macro" in t["kind"] for t in packages[pkg]["targets"]):
                    continue
                seen.add(pkg)
                todo.append(pkg)
        for pkg in seen:
            p = packages[pkg]
            found[pkg] = Crate(
                p["name"],
                p["version"],
                p["license"],
                p["repository"],
                Path(p["manifest_path"]).parent,
                p["license_file"],
            )
    return sorted(found.values(), key=lambda c: (c.name, c.version))


def rust_version() -> str:
    channel = tomllib.loads((ROOT / "rust-toolchain.toml").read_text())["toolchain"]["channel"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", channel):
        raise SystemExit('rust-toolchain.toml: the channel must be a release such as "1.98.1"')
    if channel != STD_TEXTS_RELEASE:
        raise SystemExit(
            f"rust-toolchain.toml pins Rust {channel}, but the texts in scripts/std-licenses/ "
            f"are from {STD_TEXTS_RELEASE}. Compare them with the new release, then update "
            "STD_TEXTS_RELEASE in scripts/licenses.py."
        )
    return channel


def cell(text: str) -> str:
    return text.replace("|", "\\|")


def code_block(text: str) -> str:
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{text}\n{fence}"


def check_licenses(crates: list[Crate]) -> None:
    for c in crates:
        if c.license is None:
            texts = [(c.directory / f).read_text() for f in c.notice_files()]
            if any("Redistributions in binary form must reproduce" in t for t in texts):
                continue
        if c.license not in KNOWN:
            raise SystemExit(f"{c.name} {c.version}: license {c.license} is not in KNOWN")


def notices() -> str:
    crates = shipped_crates()
    check_licenses(crates)
    rust = rust_version()
    parts = [
        "# Third-party software in pyflac-rs",
        "",
        "pyflac-rs itself is licensed 0BSD (see LICENSE). Its compiled extension",
        "module (`pyflac_rs/_native`) also contains the following Rust crates and",
        "parts of the Rust standard library. Their license notices follow the table.",
        "",
        "| Component | Version | License | Source |",
        "| --- | --- | --- | --- |",
    ]
    parts += [
        f"| {cell(c.name)} | {cell(c.version)} | {cell(c.license or 'see its notice')} "
        f"| {cell(c.repository or '')} |"
        for c in crates
    ]
    parts += [
        f"| Rust standard library: {cell(p.name)} | {rust} | {cell(p.license)} | {p.repository} |"
        for p in STD_PARTS
    ]
    parts += ["", "Copied into this repository:", ""]
    parts += [f"- `{path}`: {what}, from {crate}." for path, crate, what in VENDORED]
    for c in crates:
        parts += ["", f"## {c.name} {c.version}", "", f"- License: {c.license or 'see its notice'}"]
        if c.repository:
            parts.append(f"- Source: {c.repository}")
        for f in c.notice_files():
            text = (c.directory / f).read_text().rstrip()
            parts += ["", f"### {f}", "", code_block(text)]
    for p in STD_PARTS:
        chosen = f", {p.chosen} chosen" if p.chosen else ""
        parts += [
            "",
            f"## Rust standard library {rust}: {p.name}",
            "",
            f"- License: {p.license}{chosen}",
            f"- Source: {p.repository}",
        ]
        for title, file in p.notices:
            text = (ROOT / "scripts" / "std-licenses" / file).read_text().rstrip()
            parts += ["", f"### {title}", "", code_block(text)]
    return "\n".join(parts) + "\n"


def spdx_links(expr: str) -> str:
    def link(m: re.Match[str]) -> str:
        word = m.group()
        if word in {"AND", "OR", "WITH"}:
            return word
        return f"[{word}](https://spdx.org/licenses/{word.rstrip('+')}.html)"

    return re.sub(r"[A-Za-z0-9.+-]+", link, expr)


def page() -> str:
    crates = shipped_crates()
    check_licenses(crates)
    rust = rust_version()
    rows = []
    for c in crates:
        license = [spdx_links(c.license) if c.license else "see license text"]
        license += [
            f"[{f}](https://docs.rs/crate/{c.name}/{c.version}/source/{f})"
            for f in c.notice_files()
        ]
        source = f"[repository]({c.repository})" if c.repository else ""
        rows.append(
            f"| [{c.name}](https://crates.io/crates/{c.name}/{c.version}) | {c.version} "
            f"| {'<br>'.join(license)} | {source} |"
        )
    rows += [
        f"| Rust standard library: {p.name} | {rust} | {spdx_links(p.license)} "
        f"| [repository]({p.repository}) |"
        for p in STD_PARTS
    ]
    vendored = "\n".join(
        f"- [`{path}`]({REPO}/blob/main/{path}): {what}, from {crate}."
        for path, crate, what in VENDORED
    )
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    runtime = project["project"].get("dependencies", [])
    optional = project["project"].get("optional-dependencies", {})
    deps = ["- " + d for d in runtime] or ["None. The package has no required dependencies."]
    deps += [f"- `{extra}` extra: {', '.join(v)}" for extra, v in optional.items()]
    maturin = next(r for r in project["build-system"]["requires"] if r.startswith("maturin"))
    either = spdx_links("MIT OR Apache-2.0")
    return f"""<!-- SPDX-License-Identifier: 0BSD -->
<!-- Generated by scripts/licenses.py; do not edit. Run `python scripts/licenses.py page`. -->
# Third-party components

pyflac-rs itself is licensed 0BSD. The compiled extension module
(`pyflac_rs/_native`) also contains the Rust crates below, and the parts of
the Rust standard library that they use. Their full license notices ship in
every wheel and in the sdist as `THIRD_PARTY_LICENSES.md`, in the package's
`.dist-info/licenses/` folder (`importlib.metadata` lists it).

## Compiled into the extension module

| Crate | Version | License | Source |
|---|---|---|---|
{chr(10).join(rows)}

Copied into this repository:

{vendored}

## Python dependencies

{chr(10).join(deps)}

## Build tools (not shipped)

These build the package. They are not installed with it.

| Tool | Version | License |
|---|---|---|
| [Rust](https://www.rust-lang.org/) (rustc, cargo) | {rust} | {either} |
| [maturin](https://www.maturin.rs/) | `{maturin.removeprefix("maturin")}` | {either} |
"""


def main() -> None:
    kind = sys.argv[1] if len(sys.argv) > 1 else ""
    if kind == "notices":
        out, text = "THIRD_PARTY_LICENSES.md", notices()
    elif kind == "page":
        out, text = "docs/third-party.md", page()
    else:
        raise SystemExit("usage: licenses.py notices|page [OUT]")
    path = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / out
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    print(f"  {path}")


if __name__ == "__main__":
    main()
