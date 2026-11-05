# SPDX-License-Identifier: 0BSD
"""Compares pyflac-rs with other ways to encode and decode FLAC from Python.

    uv sync --group bench
    uv run python bench/run.py [--runs 5] [--preset cd ...] [--level 5]
                               [--json out.json] [--markdown out.md]

Each library encodes the same in-memory PCM to a .flac byte string and decodes
a .flac byte string back to int32 PCM; time is the median of ``--runs`` after
one warm-up. Every result is checked: decoded samples must equal the input. A
library that cannot run a preset (missing, or no support for its bit depth)
is reported as skipped. ``threads`` encodes 4 copies in 4 threads at once, to
show which libraries release the GIL.

The `flac` CLI runs as a subprocess per call (stdin to stdout), so its time
includes process start-up; it is there as the reference libFLAC encoder.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import io
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import numpy as np

import pyflac_rs


@dataclass(frozen=True)
class Preset:
    name: str
    rate: int
    channels: int
    bits: int
    seconds: float
    about: str


PRESETS = {
    p.name: p
    for p in [
        Preset("voice", 16_000, 1, 16, 120, "16 kHz mono 16-bit, 2 min (speech)"),
        Preset("cd", 44_100, 2, 16, 60, "44.1 kHz stereo 16-bit, 1 min"),
        Preset("hires", 96_000, 2, 24, 30, "96 kHz stereo 24-bit, 30 s"),
    ]
}
THREADS = 4


def signal(p: Preset) -> np.ndarray:
    """Deterministic music-like test audio: a few partials plus noise, so the
    encoder has real work (pure tones or silence compress unrealistically)."""
    rng = np.random.default_rng(1234)
    n = int(p.rate * p.seconds)
    t = np.arange(n) / p.rate
    amp = (1 << (p.bits - 1)) - 1
    cols = []
    for c in range(p.channels):
        x = sum(
            np.sin(2 * np.pi * f * (1 + 0.01 * c) * t + rng.uniform(0, 6))
            * (0.3 / k)
            * (1 + 0.5 * np.sin(2 * np.pi * 0.3 * k * t))
            for k, f in enumerate([110, 220, 330, 523, 880, 1760], 1)
        )
        x += rng.normal(0, 0.02, n)
        cols.append(np.clip(np.round(x * amp * 0.5), -amp - 1, amp))
    return np.stack(cols, axis=1).astype(np.int32)


# --- the libraries -----------------------------------------------------------
# Each entry: encode(pcm, preset, level) -> bytes and decode(bytes) -> int32
# (frames, channels). Raise Skip when a preset is not supported.


class Skip(Exception):
    pass


def rs_encode(pcm, p, level):
    return pyflac_rs.encode(
        pcm,
        channels=p.channels,
        bits_per_sample=p.bits,
        sample_rate=p.rate,
        compression_level=level,
    )


def rs_decode(data):
    return pyflac_rs.decode(data).to_numpy()


def av_encode(pcm, p, level):
    import av

    if p.bits != 16:
        raise Skip("PyAV's FLAC encoder input is s16 or s32 (24-bit needs extra setup)")
    buf = io.BytesIO()
    with av.open(buf, "w", format="flac") as out:
        layout = "mono" if p.channels == 1 else "stereo"
        st = out.add_stream("flac", rate=p.rate, layout=layout)
        st.format = "s16"
        st.codec_context.options = {"compression_level": str(level)}
        frame = av.AudioFrame.from_ndarray(
            pcm.astype(np.int16).reshape(1, -1), format="s16", layout=layout
        )
        frame.sample_rate = p.rate
        for packet in st.encode(frame):
            out.mux(packet)
        for packet in st.encode(None):
            out.mux(packet)
    return buf.getvalue()


def av_decode(data):
    import av

    with av.open(io.BytesIO(data)) as f:
        parts = [fr.to_ndarray() for fr in f.decode(audio=0)]
        ch = f.streams.audio[0].codec_context.channels
        shift = 16 if parts[0].dtype == np.int32 and f.streams.audio[0].format.bits == 32 else 0
    pcm = np.concatenate([x.reshape(-1) for x in parts]).reshape(-1, ch).astype(np.int32)
    return pcm >> shift if shift else pcm


def sf_encode(pcm, p, level):
    import soundfile as sf

    buf = io.BytesIO()
    # libsndfile reads int32 as full scale (24-bit samples go in the top bits)
    # and takes a 0-1 compression level that it maps onto libFLAC's 0-8.
    sf.write(
        buf, pcm << 8 if p.bits == 24 else pcm.astype(np.int16), p.rate, format="FLAC",
        subtype=f"PCM_{p.bits}", compression_level=level / 8,
    )  # fmt: skip
    return buf.getvalue()


def sf_decode(data):
    import soundfile as sf

    pcm, _ = sf.read(io.BytesIO(data), dtype="int32", always_2d=True)
    return pcm


def sf_decode_for(bits):
    # libsndfile returns int32 scaled to full range: undo it.
    def decode(data):
        return sf_decode(data) >> (32 - bits)

    return decode


def pyflac_encode(pcm, p, level):
    import pyflac

    if p.bits != 16:
        raise Skip("pyFLAC takes the bit depth from the numpy dtype (16 or 32)")
    chunks = []
    enc = pyflac.StreamEncoder(p.rate, lambda b, n, s, f: chunks.append(b), compression_level=level)
    enc.process(pcm.astype(np.int16))
    enc.finish()
    return b"".join(chunks)


def pyflac_decode(data):
    import pyflac

    # StreamDecoder, not OneShotDecoder: pyFLAC 3.0's OneShotDecoder drops the
    # last frames of the stream.
    parts = []
    dec = pyflac.StreamDecoder(lambda a, r, c, n: parts.append(a.copy()))
    dec.process(data)
    dec.finish()
    pcm = np.concatenate(parts).astype(np.int32)
    return pcm.reshape(len(pcm), -1)


def cli_encode(pcm, p, level):
    if not shutil.which("flac"):
        raise Skip("flac CLI not installed")
    raw = pcm.astype(f"<i{(p.bits + 7) // 8 if p.bits != 24 else 4}")
    if p.bits == 24:
        raw = raw.view(np.uint8).reshape(-1, 4)[:, :3]
    return subprocess.run(
        ["flac", "-s", f"-{level}", "--force-raw-format", "--endian=little", "--sign=signed",
         f"--channels={p.channels}", f"--bps={p.bits}", f"--sample-rate={p.rate}",
         "--no-seektable", "--no-padding", "-c", "-"],
        input=np.ascontiguousarray(raw).tobytes(), capture_output=True, check=True,
    ).stdout  # fmt: skip


def cli_decode_for(p):
    def decode(data):
        out = subprocess.run(
            ["flac", "-s", "-d", "--force-raw-format", "--endian=little", "--sign=signed",
             "-c", "-"],
            input=data, capture_output=True, check=True,
        ).stdout  # fmt: skip
        if p.bits == 24:
            b = np.frombuffer(out, np.uint8).reshape(-1, 3)
            v = (
                b[:, 0].astype(np.int32)
                | b[:, 1].astype(np.int32) << 8
                | b[:, 2].astype(np.int32) << 16
            )
            v = (v << 8) >> 8
        else:
            v = np.frombuffer(out, "<i2").astype(np.int32)
        return v.reshape(-1, p.channels)

    return decode


def module_version(dist: str) -> str | None:
    try:
        return importlib.metadata.version(dist)
    except importlib.metadata.PackageNotFoundError:
        return None


def versions() -> dict[str, str | None]:
    v: dict[str, str | None] = {
        "python": sys.version.split()[0],
        "implementation": platform.python_implementation()
        + (" (free-threaded)" if not getattr(sys, "_is_gil_enabled", lambda: True)() else ""),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpus": str(os.cpu_count()),
        "numpy": np.__version__,
        "pyflac-rs": module_version("pyflac-rs"),
        "pyflac-rs codec": pyflac_rs.LIBFLAC_VENDOR_STRING + " (libflac-rs 0.143.1)",
        "av": module_version("av"),
        "soundfile": module_version("soundfile"),
        "pyflac": module_version("pyflac"),
        "flac CLI": None,
    }
    try:
        import av

        lib = av.library_versions.get("libavcodec")
        v["av codec"] = f"FFmpeg libavcodec {'.'.join(map(str, lib))}" if lib else None
    except ImportError:
        pass
    try:
        import soundfile

        v["soundfile codec"] = soundfile.__libsndfile_version__ and (
            f"libsndfile {soundfile.__libsndfile_version__} (with its libFLAC)"
        )
    except (ImportError, OSError):
        pass
    if module_version("pyflac"):
        v["pyflac codec"] = "bundled libFLAC"
    if shutil.which("flac"):
        v["flac CLI"] = subprocess.run(
            ["flac", "--version"], capture_output=True, text=True
        ).stdout.strip()
    return v


# --- running -----------------------------------------------------------------


def timed(fn: Callable[[], object], runs: int) -> tuple[float, object]:
    out = fn()  # warm-up, and the result to check
    times = []
    for _ in range(runs):
        t = time.perf_counter()
        fn()
        times.append(time.perf_counter() - t)
    return statistics.median(times), out


def parallel(fn: Callable[[], object]) -> list:
    with ThreadPoolExecutor(THREADS) as pool:
        return [f.result() for f in [pool.submit(fn) for _ in range(THREADS)]]


def libraries(p: Preset) -> dict[str, tuple[Callable, Callable]]:
    return {
        "pyflac-rs": (rs_encode, rs_decode),
        "PyAV (FFmpeg)": (av_encode, av_decode),
        "soundfile (libsndfile)": (sf_encode, sf_decode_for(p.bits)),
        "pyFLAC (libFLAC)": (pyflac_encode, pyflac_decode),
        "flac CLI (libFLAC)": (cli_encode, cli_decode_for(p)),
    }


def bench_preset(p: Preset, runs: int, level: int) -> list[dict]:
    pcm = signal(p)
    reference = rs_encode(pcm, p, level)
    raw_bytes = pcm.shape[0] * p.channels * ((p.bits + 7) // 8)
    rows = []
    for name, (enc, dec) in libraries(p).items():
        row: dict = {"library": name, "preset": p.name}
        try:
            t_enc, data = timed(lambda enc=enc: enc(pcm, p, level), runs)
            # Every library decodes the same pyflac-rs file, and its own output
            # must decode (with pyflac-rs) back to the input.
            t_dec, back = timed(lambda dec=dec: dec(reference), runs)
            if not np.array_equal(back, pcm):
                raise AssertionError(f"{name} decoded different samples")
            if not np.array_equal(pyflac_rs.decode(data).to_numpy(), pcm):
                raise AssertionError(f"{name} encoded different samples")
            t_par, _ = timed(
                lambda enc=enc: parallel(lambda: enc(pcm, p, level)),
                max(1, runs // 2),
            )
            row |= {
                "encode_s": t_enc,
                "decode_s": t_dec,
                "encode_x_realtime": p.seconds / t_enc,
                "decode_x_realtime": p.seconds / t_dec,
                "ratio": len(data) / raw_bytes,
                "bytes": len(data),
                "threads_speedup": THREADS * t_enc / t_par,
            }
        except ImportError as e:
            row["skipped"] = f"not installed ({e.name})"
        except Skip as e:
            row["skipped"] = str(e)
        rows.append(row)
        done = row.get("skipped") or f"{row['encode_s'] * 1e3:.1f} ms encode"
        print(f"  {name}: {done}", file=sys.stderr)
    return rows


def markdown(results: dict) -> str:
    out = []
    for name, rows in results["presets"].items():
        p = PRESETS[name]
        out.append(f"### {p.about}, level {results['level']}, median of {results['runs']}\n")
        out.append(
            "| Library | Encode | × realtime | Decode | × realtime | Size | "
            f"{THREADS} threads |\n|---|--:|--:|--:|--:|--:|--:|"
        )
        for r in rows:
            if "skipped" in r:
                out.append(f"| {r['library']} | skipped: {r['skipped']} | | | | | |")
                continue
            out.append(
                f"| {r['library']} | {r['encode_s'] * 1e3:.1f} ms | {r['encode_x_realtime']:.0f}× "
                f"| {r['decode_s'] * 1e3:.1f} ms | {r['decode_x_realtime']:.0f}× "
                f"| {r['ratio'] * 100:.1f} % | {r['threads_speedup']:.1f}× |"
            )
        out.append("")
    out.append("<details><summary>Versions</summary>\n")
    out.append("| Component | Version |\n|---|---|")
    out += [f"| {k} | {v or '—'} |" for k, v in results["versions"].items()]
    out.append("\n</details>\n")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--level", type=int, default=5, choices=range(9))
    ap.add_argument("--preset", action="append", choices=PRESETS, help="repeatable; default all")
    ap.add_argument("--json", help="write the results as JSON")
    ap.add_argument("--markdown", help="append a markdown table (e.g. $GITHUB_STEP_SUMMARY)")
    args = ap.parse_args(argv)
    results = {"runs": args.runs, "level": args.level, "versions": versions(), "presets": {}}
    for name in args.preset or PRESETS:
        print(f"{name}:", file=sys.stderr)
        results["presets"][name] = bench_preset(PRESETS[name], args.runs, args.level)
    if args.json:
        with open(args.json, "w") as f:
            json.dump(results, f, indent=2)
    text = markdown(results)
    if args.markdown:
        with open(args.markdown, "a") as f:
            f.write(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
