# pyflac-rs

[![PyPI](https://img.shields.io/pypi/v/pyflac-rs)](https://pypi.org/project/pyflac-rs/)
[![Python](https://img.shields.io/pypi/pyversions/pyflac-rs)](https://pypi.org/project/pyflac-rs/)
[![CI](https://github.com/julianhille/pyflac-rs/actions/workflows/ci.yml/badge.svg)](https://github.com/julianhille/pyflac-rs/actions/workflows/ci.yml)
[![Docs](https://readthedocs.org/projects/pyflac-rs/badge/?version=latest)](https://pyflac-rs.readthedocs.io/)

FLAC encoding, decoding and remuxing for Python, built on
[libflac-rs](https://crates.io/crates/libflac-rs), a pure-Rust, bit-exact port
of libFLAC 1.4.3. Every call releases the GIL for the whole encode or decode,
and every function has an `async` variant.

- `.flac`, Ogg FLAC and raw FLAC frames, any sample rate. Encodes 4–32 bits;
  decodes 8, 12, 16, 20, 24 and 32 bits (see
  [bit depths](https://pyflac-rs.readthedocs.io/en/latest/usage/#bit-depths)).
- True remux between `.flac` and Ogg FLAC, with tags, pictures and all other
  metadata kept. No decoding and no re-encoding.
- Read and write Vorbis comments and pictures.
- Wheels for CPython 3.11–3.15 (one abi3 wheel) and free-threaded 3.14t and
  3.15t, on Linux (glibc and musl), macOS and Windows, x86_64 and arm64.

Documentation: <https://pyflac-rs.readthedocs.io/>

## Install

```bash
pip install pyflac-rs
```

## Usage

```python
import numpy as np
import pyflac_rs

pcm = np.zeros((44_100, 2), dtype=np.int16)          # (frames, channels)
flac = pyflac_rs.encode(pcm, channels=2, bits_per_sample=16, sample_rate=44_100)

audio = pyflac_rs.decode(flac)
audio.to_numpy()                                       # (frames, channels) int32
audio.md5_ok                                           # True

# async: runs in the loop's thread pool (or pass executor=...)
flac = await pyflac_rs.encode_async(pcm, channels=2, bits_per_sample=16, sample_rate=44_100)
audio = await pyflac_rs.decode_async(flac)
```

Remux and metadata:

```python
ogg = pyflac_rs.remux(flac, "ogg")                     # frames copied byte for byte

meta = pyflac_rs.read_metadata(flac).metadata
meta.get("artist")                                     # all ARTIST values, e.g. ["A", "B"]
flac = pyflac_rs.remux(flac, "flac", metadata=meta.with_tags({"TITLE": "New"}))
```

| Sync | Async | Does |
| --- | --- | --- |
| `encode` | `encode_async` | PCM → `.flac` (`compression_level=5`, `block_size`, `md5`, `seek_points`, `padding`, `metadata`) |
| `encode_ogg` | `encode_ogg_async` | PCM → Ogg FLAC (`serial`, `metadata`) |
| `encode_frames` | `encode_frames_async` | PCM → raw frames, no metadata |
| `decode` | `decode_async` | `.flac` → `DecodedAudio`, MD5-verified |
| `decode_ogg` | `decode_ogg_async` | Ogg FLAC → `DecodedAudio` |
| `decode_frames` | `decode_frames_async` | raw frames → `DecodedAudio` |
| `decode_seek` | `decode_seek_async` | `.flac` from `target_sample` to the end |
| `remux` | `remux_async` | `.flac` ↔ Ogg FLAC ↔ frames, keeping or replacing the metadata |
| `read_metadata` | `read_metadata_async` | STREAMINFO, tags, pictures and raw blocks |

The [documentation](https://pyflac-rs.readthedocs.io/) covers the sample
formats, containers, metadata, async and free-threaded use, and the full API.

With `compression_level=5` and no seek table or padding, the output equals
`flac -5 --no-seektable --no-padding` byte for byte on x86_64. libFLAC's ARM
builds round some float math differently and can choose other predictors.

## Benchmark

Compression level 5, median of 5 runs, on an Intel Core Ultra 9 185H with
CPython 3.12. "4 threads" is how much faster 4 encodes run in 4 threads than
one after the other.

44.1 kHz stereo 16-bit, 1 min:

| Library | Encode | Decode | Size | 4 threads |
|---|--:|--:|--:|--:|
| pyflac-rs 0.1.0 | 185.7 ms | 139.0 ms | 68.8 % | 2.2× |
| PyAV 19.0.2 (FFmpeg) | 58.9 ms | 81.7 ms | 68.9 % | 1.6× |
| soundfile 0.14.0 (libsndfile, libFLAC) | 96.1 ms | 42.5 ms | 68.8 % | 3.1× |
| pyFLAC 3.0.0 (libFLAC) | 92.1 ms | 293.9 ms | 68.8 % | 2.0× |
| flac CLI 1.5.0 (subprocess) | 148.7 ms | 91.4 ms | 68.8 % | 3.4× |

pyflac-rs encodes about 2–3× slower than the C libFLAC and FFmpeg, and still
runs more than 300× faster than realtime here. Its files are the same as
libFLAC's. It handles 24-bit input, which the PyAV and pyFLAC rows skip in the
benchmark. The [benchmark page](https://pyflac-rs.readthedocs.io/en/latest/benchmark/)
has the speech and 24-bit results, all versions, the method and how to run
`bench/run.py`.

## Development

```bash
uv sync
uv run pytest -q                       # PYFLAC_RS_TESTS=full for the long tier
uv run ruff check python tests scripts bench
```

Release build: `uv run maturin build --release`. Releases are tagged
`vX.Y.Z` from a `release/vX.Y.Z` branch and published to PyPI by the release
workflow. See [Releasing](https://pyflac-rs.readthedocs.io/en/latest/releasing/).

## License

pyflac-rs is licensed under 0BSD (`LICENSE`). The compiled extension includes
libflac-rs (BSD-3-Clause, like libFLAC) and other Rust crates. Their notices
are in `THIRD_PARTY_LICENSES.md`, which is shipped in every wheel and sdist.
