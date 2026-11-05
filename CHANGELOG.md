# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Tags and Cargo.toml use SemVer (`1.0.0-rc.1`); the wheels carry the PEP 440
form of the same version (`1.0.0rc1`).

Every version headline links to the diff against the previous release; the
links are collected at the bottom of this file.

## [Unreleased]

### Added

- `encode`, `encode_frames`, `encode_ogg` and their `_async` variants: FLAC and
  Ogg FLAC from NumPy arrays, buffers or WAV bytes, with the GIL released.
- `decode`, `decode_ogg`, `decode_frames` and `decode_seek` with their
  `_async` variants.
- `remux` between native FLAC and Ogg FLAC without re-encoding: the frames are
  copied byte for byte, and the metadata blocks are kept or replaced.
- `read_metadata` and the `Metadata`, `StreamInfo` and `Picture` types; the
  `metadata` option of `encode` and `encode_ogg`.
- Wheels for CPython 3.11–3.15 (abi3) on Linux (manylinux, musllinux), macOS
  and Windows, with the license notices of every compiled crate in
  `.dist-info/licenses/THIRD_PARTY_LICENSES.md`.

[Unreleased]: https://github.com/julianhille/pyflac-rs/commits/main
