<!-- SPDX-License-Identifier: 0BSD -->
# License texts of the Rust standard library

The extension module links parts of the Rust standard library. cargo does not
list them, so `scripts/licenses.py` names them (`STD_PARTS`), and this folder
holds their license texts, which it copies into `THIRD_PARTY_LICENSES.md`.

The files are third-party texts and are kept as published. They come from
Rust 1.98.1, the release in `rust-toolchain.toml`:

| File | Source |
|---|---|
| `rust-LICENSE-MIT` | `LICENSE-MIT` of [rust-lang/rust](https://github.com/rust-lang/rust), with the lines wrapped at 78 characters |
| `Unicode-3.0.txt` | `LICENSES/Unicode-3.0.txt` of rust-lang/rust, for the Unicode tables in `core` |
| `compiler-builtins-LICENSE.txt` | `library/compiler-builtins/LICENSE.txt` of rust-lang/rust |
| `libm-LICENSE.txt` | `library/compiler-builtins/libm/LICENSE.txt` of rust-lang/rust |

When the toolchain changes, compare the files with the new release, then set
`STD_TEXTS_RELEASE` in `scripts/licenses.py` to it. Until then the script
fails, so the notices never name a release their texts are not from.
