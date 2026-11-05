import array
import asyncio
import math
import platform
import subprocess
import threading
import time

import numpy as np
import pytest

import pyflac_rs
from pyflac_rs import FlacError


def sine(frames: int, channels: int, bits: int, rate: int = 44_100) -> np.ndarray:
    t = np.arange(frames) / rate
    amp = (1 << (bits - 1)) - 1
    cols = [
        np.round(amp * 0.8 * np.sin(2 * math.pi * (220 + 110 * c) * t)) for c in range(channels)
    ]
    return np.stack(cols, axis=1).astype(np.int32)


FMT = {"channels": 2, "bits_per_sample": 16, "sample_rate": 44_100}


@pytest.mark.parametrize("bits", [8, 12, 16, 20, 24, 32])
@pytest.mark.parametrize("channels", [1, 2, 6])
def test_roundtrip(bits, channels):
    pcm = sine(10_000, channels, bits)
    flac = pyflac_rs.encode(pcm, channels=channels, bits_per_sample=bits, sample_rate=44_100)
    assert flac[:4] == b"fLaC"
    out = pyflac_rs.decode(flac)
    assert (out.channels, out.bits_per_sample, out.sample_rate) == (channels, bits, 44_100)
    assert out.total_samples == 10_000
    assert out.md5 is not None and out.md5_ok is True
    np.testing.assert_array_equal(out.to_numpy(), pcm)


@pytest.mark.parametrize("level", range(9))
def test_levels(level):
    pcm = sine(5000, 2, 16)
    flac = pyflac_rs.encode(pcm, compression_level=level, **FMT)
    np.testing.assert_array_equal(pyflac_rs.decode(flac).to_numpy(), pcm)


def test_input_kinds_agree():
    pcm = sine(3000, 2, 16)
    ref = pyflac_rs.encode(pcm, **FMT)
    flat = pcm.reshape(-1)
    assert pyflac_rs.encode(flat, **FMT) == ref
    assert pyflac_rs.encode(flat.astype(np.int16), **FMT) == ref
    assert pyflac_rs.encode(flat.astype(np.int64), **FMT) == ref
    assert pyflac_rs.encode(array.array("i", flat.tolist()), **FMT) == ref
    assert pyflac_rs.encode(flat.tolist(), **FMT) == ref
    assert pyflac_rs.encode(memoryview(flat.tobytes()).cast("i"), **FMT) == ref
    assert pyflac_rs.encode(np.asfortranarray(pcm), **FMT) == ref


def test_decode_accepts_buffers():
    flac = pyflac_rs.encode(sine(2000, 2, 16), **FMT)
    ref = pyflac_rs.decode(flac).samples
    assert pyflac_rs.decode(bytearray(flac)).samples == ref
    assert pyflac_rs.decode(memoryview(flac)).samples == ref
    assert pyflac_rs.decode(np.frombuffer(flac, np.uint8)).samples == ref


@pytest.mark.needs_tool("flac")
def test_matches_flac_cli_and_decodes_with_it(tmp_path):
    pcm = sine(20_000, 2, 16)
    raw = tmp_path / "in.raw"
    raw.write_bytes(pcm.astype("<i2").tobytes())
    ref = tmp_path / "ref.flac"
    subprocess.run(
        [
            "flac",
            "-s",
            "-5",
            "--no-seektable",
            "--no-padding",
            "--force-raw-format",
            "--endian=little",
            "--sign=signed",
            "--channels=2",
            "--bps=16",
            "--sample-rate=44100",
            "-o",
            str(ref),
            str(raw),
        ],
        check=True,
    )
    ours = pyflac_rs.encode(pcm, compression_level=5, **FMT)
    # libflac-rs matches libFLAC's scalar float path, which x86 builds use. The
    # ARM builds' NEON code rounds differently and may pick other predictors.
    if platform.machine().lower() in ("x86_64", "amd64"):
        # The vendor string differs between libFLAC releases; compare everything else.
        assert _streaminfo_and_frames(ours) == _streaminfo_and_frames(ref.read_bytes())
    np.testing.assert_array_equal(pyflac_rs.decode(ref.read_bytes()).to_numpy(), pcm)
    out = tmp_path / "out.flac"
    out.write_bytes(pyflac_rs.encode(pcm, seek_points=10, padding=8192, **FMT))
    subprocess.run(["flac", "-s", "-t", str(out)], check=True)


def _streaminfo_and_frames(flac: bytes) -> tuple[bytes, bytes]:
    pos, streaminfo = 4, b""
    while True:
        header = flac[pos]
        length = int.from_bytes(flac[pos + 1 : pos + 4], "big")
        if header & 0x7F == 0:
            streaminfo = flac[pos + 4 : pos + 4 + length]
        pos += 4 + length
        if header & 0x80:
            return streaminfo, flac[pos:]


def test_frames_and_ogg_roundtrip():
    pcm = sine(9000, 2, 16)
    frames = pyflac_rs.encode_frames(pcm, **FMT)
    assert frames[:4] != b"fLaC"
    out = pyflac_rs.decode_frames(frames)
    assert out.md5 is None and out.md5_ok is None and out.total_samples == 9000
    np.testing.assert_array_equal(out.to_numpy(), pcm)

    ogg = pyflac_rs.encode_ogg(pcm, serial=1234, **FMT)
    assert ogg[:4] == b"OggS"
    np.testing.assert_array_equal(pyflac_rs.decode_ogg(ogg).to_numpy(), pcm)


def test_seek():
    pcm = sine(50_000, 2, 16)
    flac = pyflac_rs.encode(pcm, seek_points=20, **FMT)
    out = pyflac_rs.decode_seek(flac, 30_001)
    assert out.first_sample == 30_001
    np.testing.assert_array_equal(out.to_numpy(), pcm[30_001:])
    with pytest.raises(FlacError):
        pyflac_rs.decode_seek(flac, 50_000)


def test_md5_off_and_padding():
    pcm = sine(1000, 2, 16)
    out = pyflac_rs.decode(pyflac_rs.encode(pcm, md5=False, **FMT))
    assert out.md5 is None
    padded = pyflac_rs.encode(pcm, padding=4096, **FMT)
    assert len(padded) - len(pyflac_rs.encode(pcm, **FMT)) == 4096 + 4


def test_empty_input():
    out = pyflac_rs.decode(pyflac_rs.encode([], **FMT))
    assert out.total_samples == 0 and out.samples == b""


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"channels": 0}, "channels"),
        ({"channels": 9}, "channels"),
        ({"bits_per_sample": 3}, "bits_per_sample"),
        ({"bits_per_sample": 33}, "bits_per_sample"),
        ({"sample_rate": 0}, "sample_rate"),
        ({"compression_level": 9}, "compression_level"),
        ({"block_size": 15}, "block_size"),
        ({"padding": 1 << 24}, "padding"),
    ],
)
def test_invalid_options(kwargs, message):
    with pytest.raises(ValueError, match=message):
        pyflac_rs.encode([0, 0], **{**FMT, **kwargs})


def test_invalid_samples():
    with pytest.raises(ValueError, match="multiple of channels"):
        pyflac_rs.encode([1, 2, 3], **FMT)
    with pytest.raises(ValueError, match="outside the 16-bit range"):
        pyflac_rs.encode([0, 40_000], **FMT)
    with pytest.raises(ValueError, match="2-D samples"):
        pyflac_rs.encode(np.zeros((10, 3), np.int32), **FMT)
    with pytest.raises(TypeError, match="raw bytes"):
        pyflac_rs.encode(b"\0" * 8, **FMT)
    with pytest.raises(TypeError):
        pyflac_rs.encode([0.5, 1.0], **FMT)


def test_corrupt_input():
    flac = bytearray(pyflac_rs.encode(sine(5000, 2, 16), **FMT))
    with pytest.raises(FlacError):
        pyflac_rs.decode(b"not flac")
    flac[len(flac) // 2] ^= 0xFF
    with pytest.raises(FlacError):
        pyflac_rs.decode(bytes(flac))
    with pytest.raises(FlacError):
        pyflac_rs.decode(bytes(flac[: len(flac) // 3]))
    assert issubclass(FlacError, ValueError)


def _python_progress_during(fn) -> int:
    """How many pure-Python loop iterations the main thread gets while ``fn`` runs."""
    done = threading.Event()
    t = threading.Thread(target=lambda: (fn(), done.set()))
    t.start()
    time.sleep(0.01)
    ticks = 0
    while not done.is_set():
        ticks += 1
    t.join()
    return ticks


def test_gil_released_while_encoding_and_decoding():
    pcm = sine(44_100 * 60, 2, 16)
    flac = pyflac_rs.encode(pcm, **FMT)
    assert (
        _python_progress_during(lambda: pyflac_rs.encode(pcm, compression_level=8, **FMT)) > 10_000
    )
    assert _python_progress_during(lambda: pyflac_rs.decode(flac)) > 10_000


def test_async_api_runs_concurrently():
    pcm = sine(44_100 * 10, 2, 16)

    async def main():
        flacs = await asyncio.gather(*(pyflac_rs.encode_async(pcm, **FMT) for _ in range(4)))
        assert len(set(flacs)) == 1
        decoded = await asyncio.gather(*(pyflac_rs.decode_async(f) for f in flacs))
        for d in decoded:
            np.testing.assert_array_equal(d.to_numpy(), pcm)
        frames = await pyflac_rs.encode_frames_async(pcm, **FMT)
        assert (await pyflac_rs.decode_frames_async(frames)).total_samples == len(pcm)
        ogg = await pyflac_rs.encode_ogg_async(pcm, **FMT)
        assert (await pyflac_rs.decode_ogg_async(ogg)).total_samples == len(pcm)
        seek = await pyflac_rs.decode_seek_async(flacs[0], 100)
        assert seek.first_sample == 100
        with pytest.raises(FlacError):
            await pyflac_rs.decode_async(b"junk")

    asyncio.run(main())
