"""Property tests: random formats and signals round-trip through every container,
remuxing never touches the frames, metadata survives, and corrupt input only
ever raises FlacError."""

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st

import pyflac_rs
from pyflac_rs import BlockType, FlacError, Metadata, MetadataBlock, Picture

KINDS = ["noise", "constant", "extremes", "sine", "silence", "steps"]
# libflac-rs 0.143.1 decodes only the depths a frame header can name; other
# depths encode correctly (see test_odd_depths.py) but do not decode yet.
DECODABLE_BITS = [8, 12, 16, 20, 24, 32]


@st.composite
def audio(draw, max_frames=3000):
    bits = draw(st.sampled_from(DECODABLE_BITS))
    channels = draw(st.integers(1, 8))
    frames = draw(st.integers(1, max_frames))
    rate = draw(st.sampled_from([8000, 11_025, 16_000, 22_050, 44_100, 48_000, 96_000, 192_000]))
    kind = draw(st.sampled_from(KINDS))
    rng = np.random.default_rng(draw(st.integers(0, 2**32 - 1)))
    lo, hi = -(1 << (bits - 1)), (1 << (bits - 1)) - 1
    shape = (frames, channels)
    if kind == "noise":
        pcm = rng.integers(lo, hi, shape, endpoint=True)
    elif kind == "constant":
        pcm = np.full(shape, rng.integers(lo, hi, endpoint=True))
    elif kind == "extremes":
        pcm = rng.choice([lo, hi], shape)
    elif kind == "sine":
        t = np.arange(frames)[:, None] / rate
        freq = rng.uniform(20, rate / 2, channels)
        pcm = np.round(hi * np.sin(2 * np.pi * freq * t))
    elif kind == "silence":
        pcm = np.zeros(shape)
    else:
        pcm = np.repeat(rng.integers(lo, hi, (frames // 64 + 1, channels), endpoint=True), 64, 0)
        pcm = pcm[:frames]
    pcm = np.clip(pcm, lo, hi).astype(np.int64 if bits == 32 else np.int32)
    return pcm, {"channels": channels, "bits_per_sample": bits, "sample_rate": rate}


def as_int32(pcm):
    return pcm.astype(np.int32)


@given(audio(), st.integers(0, 8))
def test_roundtrip_every_container(case, level):
    pcm, fmt = case
    flac = pyflac_rs.encode(pcm, compression_level=level, **fmt)
    out = pyflac_rs.decode(flac)
    assert out.md5_ok
    assert (out.channels, out.bits_per_sample, out.sample_rate) == tuple(fmt.values())
    assert out.total_samples == len(pcm)
    np.testing.assert_array_equal(out.to_numpy(), as_int32(pcm))
    ogg = pyflac_rs.encode_ogg(pcm, compression_level=level, **fmt)
    np.testing.assert_array_equal(pyflac_rs.decode_ogg(ogg).to_numpy(), as_int32(pcm))
    frames = pyflac_rs.encode_frames(pcm, compression_level=level, **fmt)
    np.testing.assert_array_equal(pyflac_rs.decode_frames(frames).to_numpy(), as_int32(pcm))


@given(audio(), st.sampled_from([None, 16, 192, 576, 1152, 4096]))
def test_block_size(case, block_size):
    pcm, fmt = case
    flac = pyflac_rs.encode(pcm, block_size=block_size, **fmt)
    np.testing.assert_array_equal(pyflac_rs.decode(flac).to_numpy(), as_int32(pcm))


@given(audio(), st.integers(0, 2**32 - 1))
def test_remux_never_touches_frames(case, serial):
    pcm, fmt = case
    flac = pyflac_rs.encode(pcm, **fmt)
    frames = pyflac_rs.remux(flac, "frames")
    assert frames == pyflac_rs.encode_frames(pcm, **fmt)
    ogg = pyflac_rs.remux(flac, "ogg", serial=serial)
    assert ogg == pyflac_rs.encode_ogg(pcm, serial=serial, **fmt)
    assert pyflac_rs.read_metadata(ogg).serial == serial
    back = pyflac_rs.remux(ogg, "flac")
    assert back == flac
    assert pyflac_rs.remux(flac, "flac") == flac
    assert pyflac_rs.remux(ogg, "ogg") == ogg


@given(audio(max_frames=20_000), st.integers(0, 50), st.data())
def test_seek_matches_slice(case, seek_points, data):
    pcm, fmt = case
    flac = pyflac_rs.encode(pcm, seek_points=seek_points, **fmt)
    target = data.draw(st.integers(0, len(pcm) - 1))
    out = pyflac_rs.decode_seek(flac, target)
    assert out.first_sample <= target
    np.testing.assert_array_equal(out.to_numpy(), as_int32(pcm[out.first_sample :]))


@given(audio(max_frames=10_000), st.integers(0, 100), st.integers(0, 5000))
def test_seektable_and_padding_on_remux(case, seek_points, padding):
    pcm, fmt = case
    flac = pyflac_rs.encode(pcm, **fmt)
    out = pyflac_rs.remux(flac, "flac", seek_points=seek_points, padding=padding)
    blocks = {b.type: b for b in pyflac_rs.read_metadata(out).metadata.blocks}
    assert (BlockType.SEEKTABLE in blocks) == (seek_points > 0)
    if padding:
        assert blocks[BlockType.PADDING].data == bytes(padding)
    else:
        assert BlockType.PADDING not in blocks
    assert pyflac_rs.remux(out, "frames") == pyflac_rs.remux(flac, "frames")
    np.testing.assert_array_equal(pyflac_rs.decode(out).to_numpy(), as_int32(pcm))


TAG_NAME = st.text(
    st.characters(min_codepoint=0x20, max_codepoint=0x7D).filter(lambda c: c != "="),
    min_size=1,
    max_size=20,
)
TAG_VALUE = st.text(st.characters(exclude_categories=["Cs"]), max_size=200)
TAGS = st.lists(st.tuples(TAG_NAME, TAG_VALUE), max_size=20)
PICTURES = st.lists(
    st.builds(
        Picture,
        data=st.binary(max_size=500),
        mime=st.text(st.characters(min_codepoint=0x20, max_codepoint=0x7E), max_size=30),
        type=st.integers(0, 20),
        description=TAG_VALUE,
        width=st.integers(0, 2**32 - 1),
        height=st.integers(0, 2**32 - 1),
        depth=st.integers(0, 2**32 - 1),
        colors=st.integers(0, 2**32 - 1),
    ),
    max_size=3,
)
APPS = st.lists(
    st.builds(
        lambda app_id, body: MetadataBlock(BlockType.APPLICATION, app_id + body),
        st.binary(min_size=4, max_size=4),
        st.binary(max_size=200),
    ),
    max_size=3,
)


@given(TAGS, PICTURES, APPS, st.sampled_from(["flac", "ogg"]))
def test_metadata_round_trips(tags, pictures, apps, container):
    pcm = np.arange(-500, 500, dtype=np.int16).reshape(-1, 2)
    fmt = {"channels": 2, "bits_per_sample": 16, "sample_rate": 44_100}
    meta = Metadata.build(tags=tags, pictures=pictures, blocks=apps)
    encode = pyflac_rs.encode if container == "flac" else pyflac_rs.encode_ogg
    data = encode(pcm, metadata=meta, **fmt)
    m = pyflac_rs.read_metadata(data).metadata
    assert m.tags == tuple(tags)
    assert m.pictures == tuple(pictures)
    for app in apps:
        assert app in m.blocks
    for name, _ in tags:
        assert m.get(name.lower()) == [v for k, v in tags if k.upper() == name.upper()]
    # Both directions of remux keep all of it.
    other = pyflac_rs.remux(data, "ogg" if container == "flac" else "flac")
    m2 = pyflac_rs.read_metadata(other).metadata
    assert (m2.tags, m2.pictures, m2.vendor) == (m.tags, m.pictures, m.vendor)
    np.testing.assert_array_equal(pyflac_rs.decode(pyflac_rs.remux(data, "flac")).to_numpy(), pcm)


@given(PICTURES)
def test_picture_block_round_trip(pictures):
    for p in pictures:
        assert Picture.from_block(p.to_block().data) == p


@given(st.binary(max_size=200))
def test_picture_parser_rejects_garbage_cleanly(body):
    try:
        Picture.from_block(body)
    except FlacError:
        pass


def _corrupt_all_ways(data: bytes, positions, values):
    for pos, value in zip(positions, values, strict=True):
        pos %= len(data)
        if data[pos] == value:
            continue
        yield data[:pos] + bytes([value]) + data[pos + 1 :]
    yield data[: positions[0] % len(data)]


CORRUPTION = st.tuples(
    st.lists(st.integers(0, 2**31), min_size=1, max_size=4),
    st.lists(st.integers(0, 255), min_size=4, max_size=4),
)


@pytest.mark.parametrize(
    ("make", "readers"),
    [
        (
            pyflac_rs.encode,
            [pyflac_rs.decode, pyflac_rs.read_metadata, lambda d: pyflac_rs.remux(d, "ogg")],
        ),
        (
            pyflac_rs.encode_ogg,
            [pyflac_rs.decode_ogg, pyflac_rs.read_metadata, lambda d: pyflac_rs.remux(d, "flac")],
        ),
        (pyflac_rs.encode_frames, [pyflac_rs.decode_frames]),
    ],
    ids=["flac", "ogg", "frames"],
)
@given(corruption=CORRUPTION)
def test_corrupt_input_raises_flac_error_only(make, readers, corruption):
    positions, values = corruption
    pcm = np.arange(-2000, 2000, 3, dtype=np.int16).reshape(-1, 2)
    good = make(pcm, channels=2, bits_per_sample=16, sample_rate=44_100)
    for bad in _corrupt_all_ways(good, positions, values[: len(positions)]):
        for read in readers:
            # Damage the CRCs cannot see (padding, tag text) may still read;
            # anything else must be a FlacError, never another exception.
            try:
                read(bad)
            except FlacError:
                pass


@pytest.mark.parametrize(
    ("make", "read", "pos", "value"),
    [
        (pyflac_rs.encode, pyflac_rs.decode, 94, 0x55),
        (pyflac_rs.encode, pyflac_rs.decode, 95, 0x0F),
        (pyflac_rs.encode_frames, pyflac_rs.decode_frames, 8, 0x55),
        (pyflac_rs.encode_frames, pyflac_rs.decode_frames, 9, 0x0F),
    ],
)
def test_residual_underflow_is_a_flac_error(make, read, pos, value):
    # Found by test_corrupt_input_raises_flac_error_only: a partition order that
    # leaves fewer samples than the predictor order made libflac-rs 0.143.1
    # panic (slice index underflow) instead of rejecting the frame.
    pcm = np.arange(-2000, 2000, 3, dtype=np.int16).reshape(-1, 2)
    good = make(pcm, channels=2, bits_per_sample=16, sample_rate=44_100)
    with pytest.raises(FlacError):
        read(good[:pos] + bytes([value]) + good[pos + 1 :])


@pytest.mark.parametrize("serial", [0, 1, 2**31 - 1, 2**31, 2**32 - 1])
def test_ogg_serial_is_unsigned_32_bit(serial):
    pcm = np.zeros((100, 1), dtype=np.int16)
    fmt = {"channels": 1, "bits_per_sample": 16, "sample_rate": 8000}
    ogg = pyflac_rs.encode_ogg(pcm, serial=serial, **fmt)
    assert int.from_bytes(ogg[14:18], "little") == serial
    assert pyflac_rs.read_metadata(ogg).serial == serial
    flac = pyflac_rs.encode(pcm, **fmt)
    assert pyflac_rs.remux(flac, "ogg", serial=serial) == ogg
    for bad in (-1, 2**32):
        with pytest.raises(OverflowError):
            pyflac_rs.encode_ogg(pcm, serial=bad, **fmt)


@pytest.mark.full
@pytest.mark.parametrize("bits", DECODABLE_BITS)
@pytest.mark.parametrize("channels", range(1, 9))
@pytest.mark.parametrize("level", range(9))
def test_full_matrix(bits, channels, level):
    # Every depth, channel count and level, with lengths around block edges.
    rng = np.random.default_rng(bits * 100 + channels * 10 + level)
    lo, hi = -(1 << (bits - 1)), (1 << (bits - 1)) - 1
    fmt = {"channels": channels, "bits_per_sample": bits, "sample_rate": 48_000}
    for frames in (1, 15, 16, 1151, 1152, 1153, 4095, 4097, 20_000):
        t = np.arange(frames)[:, None] / 48_000
        tone = np.round(hi * 0.7 * np.sin(2 * np.pi * (300 + 50 * np.arange(channels)) * t))
        noise = rng.integers(lo // 8, hi // 8, (frames, channels), endpoint=True)
        pcm = np.clip(tone + noise, lo, hi).astype(np.int64)
        flac = pyflac_rs.encode(pcm, compression_level=level, **fmt)
        out = pyflac_rs.decode(flac)
        assert out.md5_ok
        np.testing.assert_array_equal(out.to_numpy(), pcm.astype(np.int32))
        ogg = pyflac_rs.remux(flac, "ogg")
        np.testing.assert_array_equal(pyflac_rs.decode_ogg(ogg).to_numpy(), pcm.astype(np.int32))
