# Benchmark

`bench/run.py` compares pyflac-rs with the other ways to encode and decode FLAC
from Python:

| Library | Codec underneath |
| --- | --- |
| **pyflac-rs** | libflac-rs, a pure-Rust port of libFLAC 1.4.3 |
| [PyAV](https://pypi.org/project/av/) | FFmpeg's own FLAC encoder and decoder (libavcodec) |
| [soundfile](https://pypi.org/project/soundfile/) | libsndfile, which uses the C libFLAC |
| [pyFLAC](https://pypi.org/project/pyFLAC/) | the C libFLAC, through CFFI |
| `flac` CLI | the C libFLAC reference encoder, as a subprocess |

## Results

Intel Core Ultra 9 185H (22 logical CPUs), Linux 7.2, CPython 3.12.10,
compression level 5, median of 5 runs after one warm-up. Smaller times are
better. "× realtime" is audio duration divided by time. "Size" is the FLAC
file as a share of the raw PCM. "4 threads" is how much faster 4 encodes run
in 4 threads than one after the other (4.0× is perfect scaling).

### 16 kHz mono 16-bit, 2 min (speech)

| Library | Encode | × realtime | Decode | × realtime | Size | 4 threads |
|---|--:|--:|--:|--:|--:|--:|
| pyflac-rs | 91.0 ms | 1318× | 52.7 ms | 2279× | 72.1 % | 3.3× |
| PyAV (FFmpeg) | 47.0 ms | 2553× | 21.7 ms | 5542× | 73.2 % | 1.2× |
| soundfile (libsndfile) | 31.5 ms | 3811× | 29.4 ms | 4088× | 72.1 % | 3.2× |
| pyFLAC (libFLAC) | 45.6 ms | 2631× | 64.3 ms | 1867× | 72.1 % | 3.1× |
| flac CLI (libFLAC) | 40.5 ms | 2962× | 38.6 ms | 3109× | 72.1 % | 2.7× |

### 44.1 kHz stereo 16-bit, 1 min

| Library | Encode | × realtime | Decode | × realtime | Size | 4 threads |
|---|--:|--:|--:|--:|--:|--:|
| pyflac-rs | 185.7 ms | 323× | 139.0 ms | 432× | 68.8 % | 2.2× |
| PyAV (FFmpeg) | 58.9 ms | 1018× | 81.7 ms | 734× | 68.9 % | 1.6× |
| soundfile (libsndfile) | 96.1 ms | 624× | 42.5 ms | 1410× | 68.8 % | 3.1× |
| pyFLAC (libFLAC) | 92.1 ms | 651× | 293.9 ms | 204× | 68.8 % | 2.0× |
| flac CLI (libFLAC) | 148.7 ms | 403× | 91.4 ms | 657× | 68.8 % | 3.4× |

### 96 kHz stereo 24-bit, 30 s

| Library | Encode | × realtime | Decode | × realtime | Size | 4 threads |
|---|--:|--:|--:|--:|--:|--:|
| pyflac-rs | 262.4 ms | 114× | 214.7 ms | 140× | 78.6 % | 2.3× |
| PyAV (FFmpeg) | skipped | | | | | |
| soundfile (libsndfile) | 90.8 ms | 331× | 52.7 ms | 570× | 78.6 % | 1.6× |
| pyFLAC (libFLAC) | skipped | | | | | |
| flac CLI (libFLAC) | 231.1 ms | 130× | 156.8 ms | 191× | 78.6 % | 3.3× |

| Component | Version |
| --- | --- |
| pyflac-rs | 0.1.0 (libflac-rs 0.143.1, vendor string "reference libFLAC 1.4.3 20230623") |
| PyAV | 19.0.2 (FFmpeg libavcodec 63.1.102) |
| soundfile | 0.14.0 (libsndfile 1.2.2) |
| pyFLAC | 3.0.0 (bundled libFLAC) |
| flac CLI | 1.5.0 |
| numpy | 2.5.4 |

## Reading the numbers

- **Speed.** pyflac-rs encodes about 2–3× slower than the C libFLAC
  (soundfile, pyFLAC) and FFmpeg, and decodes about 1.7–3.3× slower than
  soundfile and PyAV. It decodes faster than pyFLAC. The `flac` CLI's times
  include starting a process per call, so at 24-bit it is close to pyflac-rs. Every library still runs more than 100× faster than realtime.
- **Size.** pyflac-rs, soundfile, pyFLAC and the `flac` CLI produce the same
  size, because pyflac-rs is a bit-exact port of libFLAC: at level 5 with no
  seek table or padding, its output equals x86 `flac -5` byte for byte. FFmpeg has
  its own encoder and produces slightly different files.
- **Threads.** pyflac-rs releases the GIL for the whole call, so encodes in
  separate threads run at the same time. PyAV scales least in this run.
- **Bit depths.** The PyAV and pyFLAC rows are skipped at 24-bit. PyAV's FLAC
  encoder takes s16 or s32 input, and 24-bit needs extra setup. pyFLAC takes
  the bit depth from the numpy dtype, which is 16 or 32. pyflac-rs takes any
  depth from 4 to 32 bits.

Each result is checked before it is reported. A library's decode of the
pyflac-rs file must equal the input. Its encoded file, decoded with pyflac-rs,
must equal the input too.

Caveats:

- These numbers come from one laptop. They vary with the CPU, power settings
  and background load.
- The `Benchmark` workflow runs the same script on GitHub's shared runners for
  every change. Its numbers are noisier, so compare trends between runs, not
  single numbers.
- pyFLAC's `OneShotDecoder` drops the last frames of a file, so the benchmark
  decodes with its `StreamDecoder` instead.

## Running it

```bash
sudo apt-get install flac          # or: brew install flac (optional)
uv sync --group bench
uv run python bench/run.py
```

Options:

- `--runs N`: timed runs per measurement (default 5).
- `--level 0..8`: compression level (default 5).
- `--preset voice|cd|hires`: repeat it to pick several presets (default all).
- `--json FILE`: write the raw results and versions as JSON.
- `--markdown FILE`: append the tables, for example to `$GITHUB_STEP_SUMMARY`.

The input is deterministic: the same few partials plus noise every run. A
library that is not installed, or that cannot handle the preset's bit depth,
is reported as skipped. The `bench` dependency group installs pyFLAC only on
Python 3.13 and older (see `pyproject.toml`).
