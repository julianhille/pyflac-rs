"""Bit depths a FLAC frame header cannot name (4-7, 9-11, 13-15, 17-19, 21-23,
25-31): the header says "see STREAMINFO". pyflac-rs encodes them correctly,
which the reference decoder checks. libflac-rs 0.143.1 cannot decode them yet:
its frame parser rejects that header code. The xfail tests are strict, so they
fail as soon as an upgrade fixes it, and the restriction can be lifted."""

import subprocess

import numpy as np
import pytest

import pyflac_rs
from pyflac_rs import FlacError

ODD_BITS = [b for b in range(4, 33) if b not in (8, 12, 16, 20, 24, 32)]


def pcm_for(bits: int, frames: int = 5000, channels: int = 2) -> np.ndarray:
    rng = np.random.default_rng(bits)
    lo, hi = -(1 << (bits - 1)), (1 << (bits - 1)) - 1
    return rng.integers(lo, hi, (frames, channels), endpoint=True)


@pytest.mark.needs_tool("flac")
@pytest.mark.parametrize("bits", ODD_BITS)
@pytest.mark.parametrize("container", ["flac", "ogg"])
def test_reference_decoder_verifies_odd_depths(tmp_path, bits, container):
    # `flac -t` decodes every frame and compares the audio with the MD5 of the
    # input that the encoder stored in STREAMINFO. (Its raw output only takes
    # 8/16/24/32 bits, so the samples cannot be compared directly.)
    encode = pyflac_rs.encode if container == "flac" else pyflac_rs.encode_ogg
    src = tmp_path / f"in.{container}"
    src.write_bytes(encode(pcm_for(bits), channels=2, bits_per_sample=bits, sample_rate=44_100))
    subprocess.run(["flac", "-t", "-s", "-w", str(src)], check=True)
    if container == "ogg":
        return
    # Sanity: a wrong MD5 makes the same check fail.
    bad = bytearray(src.read_bytes())
    md5_at = bad.index(b"fLaC") + 4 + 4 + 18
    bad[md5_at] ^= 0xFF
    src.write_bytes(bytes(bad))
    assert subprocess.run(["flac", "-t", "-s", "-w", str(src)], capture_output=True).returncode


@pytest.mark.parametrize("bits", ODD_BITS)
def test_odd_depths_encode_and_report_metadata(bits):
    pcm = pcm_for(bits, frames=1000)
    flac = pyflac_rs.encode(pcm, channels=2, bits_per_sample=bits, sample_rate=48_000)
    info = pyflac_rs.read_metadata(flac).stream_info
    assert (info.bits_per_sample, info.channels, info.total_samples) == (bits, 2, 1000)
    # Remux never decodes, so it works at every depth.
    assert pyflac_rs.remux(pyflac_rs.remux(flac, "ogg"), "flac") == flac


@pytest.mark.xfail(raises=FlacError, strict=True, reason="libflac-rs 0.143.1 decoder")
@pytest.mark.parametrize("bits", ODD_BITS)
def test_odd_depths_decode(bits):
    pcm = pcm_for(bits, frames=1000)
    flac = pyflac_rs.encode(pcm, channels=2, bits_per_sample=bits, sample_rate=48_000)
    np.testing.assert_array_equal(pyflac_rs.decode(flac).to_numpy(), pcm)
