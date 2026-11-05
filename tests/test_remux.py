import asyncio
import shutil
import subprocess

import numpy as np
import pytest
from test_codec import FMT, sine

import pyflac_rs
from pyflac_rs import BlockType, FlacError, Metadata, MetadataBlock, Picture

PCM = sine(30_000, 2, 16)
TAGS = {"TITLE": "Ünïcode title", "ARTIST": ["A", "B"], "comment": "x=y"}
COVER = Picture(b"\x89PNG fake", mime="image/png", description="cover", width=2, height=3)
APP = MetadataBlock(BlockType.APPLICATION, b"test" + bytes(range(40)))


def rich() -> Metadata:
    return Metadata.build(tags=TAGS, pictures=[COVER], blocks=[APP])


def frames_of(data: bytes) -> bytes:
    return pyflac_rs.remux(data, "frames")


def test_default_metadata_reproduces_encode():
    vendor_only = Metadata.build()
    for seek, pad in [(0, 0), (10, 0), (0, 100), (7, 33), (1000, 0)]:
        ref = pyflac_rs.encode(PCM, seek_points=seek, padding=pad, **FMT)
        got = pyflac_rs.encode(PCM, seek_points=seek, padding=pad, metadata=vendor_only, **FMT)
        assert got == ref, (seek, pad)
    ref = pyflac_rs.encode_ogg(PCM, serial=7, **FMT)
    assert pyflac_rs.encode_ogg(PCM, serial=7, metadata=vendor_only, **FMT) == ref


def test_flac_to_ogg_matches_libflac_ogg():
    for serial in [0, 1, 2**32 - 5, 0x12345678]:
        flac = pyflac_rs.encode(PCM, seek_points=10, **FMT)
        assert pyflac_rs.remux(flac, "ogg", serial=serial) == pyflac_rs.encode_ogg(
            PCM, serial=serial, **FMT
        )


def test_ogg_to_flac_matches_native():
    ogg = pyflac_rs.encode_ogg(PCM, serial=3, **FMT)
    assert pyflac_rs.remux(ogg, "flac") == pyflac_rs.encode(PCM, **FMT)
    assert pyflac_rs.remux(ogg, "flac", seek_points=12, padding=64) == pyflac_rs.encode(
        PCM, seek_points=12, padding=64, **FMT
    )
    assert pyflac_rs.remux(ogg, "ogg") == ogg
    assert pyflac_rs.read_metadata(ogg).serial == 3


def test_frames_are_byte_identical():
    flac = pyflac_rs.encode(PCM, compression_level=8, metadata=rich(), **FMT)
    frames = frames_of(flac)
    assert frames == pyflac_rs.encode_frames(PCM, compression_level=8, **FMT)
    ogg = pyflac_rs.remux(flac, "ogg")
    assert frames_of(ogg) == frames
    assert frames_of(pyflac_rs.remux(ogg, "flac")) == frames


def test_metadata_survives_round_trip():
    flac = pyflac_rs.encode(PCM, metadata=rich(), seek_points=5, padding=50, **FMT)
    before = pyflac_rs.read_metadata(flac)
    ogg = pyflac_rs.remux(flac, "ogg")
    back = pyflac_rs.remux(ogg, "flac")
    for data in (ogg, back):
        info = pyflac_rs.read_metadata(data)
        assert info.stream_info == before.stream_info
        m = info.metadata
        assert m.vendor == pyflac_rs.LIBFLAC_VENDOR_STRING
        assert m.tags == (
            ("TITLE", "Ünïcode title"),
            ("ARTIST", "A"),
            ("ARTIST", "B"),
            ("comment", "x=y"),
        )
        assert m.get("Artist") == ["A", "B"] and m.get("COMMENT") == ["x=y"]
        assert m.pictures == (COVER,)
        assert APP in m.blocks
        assert MetadataBlock(BlockType.PADDING, bytes(50)) in m.blocks
    # Ogg has no SEEKTABLE; the flac source keeps its own.
    assert BlockType.SEEKTABLE not in {b.type for b in pyflac_rs.read_metadata(ogg).metadata.blocks}
    assert pyflac_rs.remux(flac, "flac") == flac
    np.testing.assert_array_equal(pyflac_rs.decode(back).to_numpy(), PCM)
    np.testing.assert_array_equal(pyflac_rs.decode_ogg(ogg).to_numpy(), PCM)


def test_replace_metadata_on_remux():
    flac = pyflac_rs.encode(PCM, metadata=rich(), padding=10, **FMT)
    src = pyflac_rs.read_metadata(flac).metadata
    new = src.with_tags({"TITLE": "new"}).with_pictures([]).without(BlockType.PADDING)
    out = pyflac_rs.remux(flac, "flac", metadata=new)
    m = pyflac_rs.read_metadata(out).metadata
    assert m.tags == (("TITLE", "new"),) and m.vendor == src.vendor
    assert m.pictures == () and m.blocks == new.blocks
    assert frames_of(out) == frames_of(flac)
    stripped = pyflac_rs.remux(flac, "flac", metadata=Metadata(), padding=0)
    assert pyflac_rs.read_metadata(stripped).metadata.blocks == ()
    # Ogg needs a VORBIS_COMMENT first; a vendor-only one is added.
    ogg = pyflac_rs.remux(flac, "ogg", metadata=Metadata((APP,)))
    assert pyflac_rs.read_metadata(ogg).metadata == Metadata((Metadata.build().blocks[0], APP))


def test_seektable_points_decode():
    ogg = pyflac_rs.encode_ogg(PCM, **FMT)
    flac = pyflac_rs.remux(ogg, "flac", seek_points=20)
    out = pyflac_rs.decode_seek(flac, 20_000)
    assert out.first_sample <= 20_000
    np.testing.assert_array_equal(out.to_numpy(), PCM[out.first_sample :])
    no_table = pyflac_rs.remux(flac, "flac", seek_points=0)
    assert no_table == pyflac_rs.remux(ogg, "flac")


def test_id3v2_prefix_is_skipped():
    flac = pyflac_rs.encode(PCM, **FMT)
    id3 = b"ID3\x04\x00\x00" + bytes([0, 0, 0, 20]) + bytes(20)
    assert pyflac_rs.remux(id3 + flac, "flac") == flac


def test_corrupt_inputs():
    flac = pyflac_rs.encode(PCM, **FMT)
    ogg = pyflac_rs.encode_ogg(PCM, **FMT)
    bad = bytearray(flac)
    bad[-100] ^= 1
    with pytest.raises(FlacError, match="corrupt or truncated FLAC frame"):
        pyflac_rs.remux(bytes(bad), "ogg")
    with pytest.raises(FlacError):
        pyflac_rs.remux(flac[:-3], "ogg")
    bad = bytearray(ogg)
    bad[-100] ^= 1
    with pytest.raises(FlacError, match="CRC"):
        pyflac_rs.remux(bytes(bad), "flac")
    with pytest.raises(FlacError):
        pyflac_rs.remux(b"RIFF....", "flac")
    with pytest.raises(FlacError):
        pyflac_rs.read_metadata(flac[:30])


def test_invalid_metadata():
    flac = pyflac_rs.encode(PCM, **FMT)
    with pytest.raises(ValueError, match="not allowed"):
        pyflac_rs.remux(flac, "flac", metadata=[MetadataBlock(0, bytes(34))])
    with pytest.raises(ValueError, match="more than one"):
        pyflac_rs.remux(flac, "flac", metadata=Metadata.build().blocks * 2)
    with pytest.raises(ValueError, match="SEEKTABLE"):
        pyflac_rs.encode(PCM, metadata=[MetadataBlock(3, bytes(18))], **FMT)
    with pytest.raises(ValueError, match="to must be"):
        pyflac_rs.remux(flac, "wav")
    with pytest.raises(ValueError, match="tag name"):
        Metadata.build(tags={"A=B": "c"})


def test_remux_async():
    flac = pyflac_rs.encode(PCM, metadata=rich(), **FMT)

    async def main():
        return await asyncio.gather(
            pyflac_rs.remux_async(flac, "ogg", serial=1),
            pyflac_rs.read_metadata_async(flac),
        )

    ogg, info = asyncio.run(main())
    assert ogg == pyflac_rs.remux(flac, "ogg", serial=1)
    assert info.metadata == rich()


@pytest.mark.needs_tool("flac")
def test_reference_tools_accept_remux(tmp_path):
    flac = pyflac_rs.encode(PCM, metadata=rich(), seek_points=8, **FMT)
    (tmp_path / "a.flac").write_bytes(flac)
    (tmp_path / "a.oga").write_bytes(pyflac_rs.remux(flac, "ogg", serial=9))
    for name in ["a.flac", "a.oga"]:
        subprocess.run(["flac", "-t", "-s", str(tmp_path / name)], check=True)
    if shutil.which("metaflac"):
        tags = subprocess.run(
            ["metaflac", "--export-tags-to=-", str(tmp_path / "a.flac")],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert tags.splitlines() == ["TITLE=Ünïcode title", "ARTIST=A", "ARTIST=B", "comment=x=y"]


@pytest.mark.needs_tool("flac")
@pytest.mark.parametrize("args", [[], ["-b", "1152", "-0"], ["-b", "192"], ["-8", "--ogg"]])
def test_remux_reference_files(tmp_path, args):
    wav = tmp_path / "in.raw"
    wav.write_bytes(PCM.astype("<i2").tobytes())
    out = tmp_path / "ref.flac"
    subprocess.run(
        [
            "flac",
            "-s",
            "-f",
            "--force-raw-format",
            "--endian=little",
            "--sign=signed",
            "--channels=2",
            "--bps=16",
            "--sample-rate=44100",
            "-T",
            "ARTIST=ref",
            "--padding=123",
            *args,
            "-o",
            str(out),
            str(wav),
        ],
        check=True,
    )
    ref = out.read_bytes()
    target = "flac" if ref[:4] == b"OggS" else "ogg"
    remuxed = pyflac_rs.remux(ref, target)
    assert frames_of(remuxed) == frames_of(ref)
    info = pyflac_rs.read_metadata(remuxed)
    assert info.metadata.get("artist") == ["ref"]
    assert MetadataBlock(BlockType.PADDING, bytes(123)) in info.metadata.blocks
    assert pyflac_rs.remux(remuxed, pyflac_rs.read_metadata(ref).container) is not None
    decode = pyflac_rs.decode if target == "flac" else pyflac_rs.decode_ogg
    np.testing.assert_array_equal(decode(remuxed).to_numpy(), PCM)
    (tmp_path / f"out.{target}").write_bytes(remuxed)
    subprocess.run(["flac", "-t", "-s", str(tmp_path / f"out.{target}")], check=True)
