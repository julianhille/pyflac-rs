"""Async, GIL-free FLAC encoding and decoding backed by libflac-rs.

Every sync function releases the GIL for the whole encode/decode, so it runs in
parallel with other Python threads. The ``*_async`` variants run the same call in
an executor (the loop's default thread pool unless ``executor`` is given), which
keeps the event loop responsive and lets several codec calls run concurrently.

Samples are interleaved signed integers (``L R L R ...``), sign-extended to the
stream's ``bits_per_sample``. Encoders accept any int8/16/32/64 buffer (numpy
arrays, ``array.array``, ``memoryview.cast``), a 2-D ``(frames, channels)`` array,
or a sequence of ints. Decoders return native-endian ``int32`` sample bytes.

``remux`` moves the audio frames between ``.flac`` and Ogg FLAC byte for byte (no
re-encoding) and keeps the metadata (tags, pictures, cue sheets, ...) unless told
otherwise; ``read_metadata`` reads it without touching the audio.
"""

from __future__ import annotations

import asyncio
import functools
import sys
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import Executor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, TypeVar

from . import _native
from ._native import LIBFLAC_VENDOR_STRING, FlacError
from .metadata import (
    BlockType,
    FlacInfo,
    Metadata,
    MetadataBlock,
    Picture,
    StreamInfo,
    Tags,
)

if sys.version_info >= (3, 12):
    from collections.abc import Buffer
else:
    Buffer = bytes | bytearray | memoryview

if TYPE_CHECKING:
    import numpy as np

__all__ = [
    "LIBFLAC_VENDOR_STRING",
    "BlockType",
    "DecodedAudio",
    "FlacError",
    "FlacInfo",
    "Metadata",
    "MetadataBlock",
    "Picture",
    "StreamInfo",
    "Tags",
    "decode",
    "decode_async",
    "decode_frames",
    "decode_frames_async",
    "decode_ogg",
    "decode_ogg_async",
    "decode_seek",
    "decode_seek_async",
    "encode",
    "encode_async",
    "encode_frames",
    "encode_frames_async",
    "encode_ogg",
    "encode_ogg_async",
    "read_metadata",
    "read_metadata_async",
    "remux",
    "remux_async",
]

Samples = Buffer | Sequence[int]
MetadataLike = Metadata | Iterable[MetadataBlock]
T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class DecodedAudio:
    """Decoded PCM plus the stream format."""

    samples: bytes
    """Interleaved native-endian ``int32`` samples."""
    channels: int
    bits_per_sample: int
    sample_rate: int
    total_samples: int
    """Samples per channel in ``samples`` (STREAMINFO's total for ``decode``)."""
    md5: bytes | None
    """STREAMINFO audio MD5, or ``None`` when absent (or not applicable)."""
    md5_ok: bool | None
    """Whether the decoded audio matches ``md5``; ``None`` when not checked."""
    first_sample: int = 0
    """Index of the first sample in ``samples`` (non-zero after ``decode_seek``)."""

    @property
    def duration(self) -> float:
        """Duration of ``samples`` in seconds."""
        return self.total_samples / self.sample_rate if self.sample_rate else 0.0

    def to_memoryview(self) -> memoryview:
        """A zero-copy ``int32`` view of the interleaved samples."""
        return memoryview(self.samples).cast("i")

    def to_numpy(self) -> np.ndarray:
        """A read-only ``(frames, channels)`` ``int32`` array sharing ``samples``."""
        import numpy as np

        pcm = np.frombuffer(self.samples, dtype=np.int32)
        return pcm.reshape(-1, self.channels) if self.channels else pcm


def _wrap(raw: tuple[Any, ...]) -> DecodedAudio:
    return DecodedAudio(*raw)


def _blocks(metadata: MetadataLike | None) -> list[tuple[int, bytes]] | None:
    if metadata is None:
        return None
    blocks = metadata.blocks if isinstance(metadata, Metadata) else metadata
    return [(int(b.type), bytes(b.data)) for b in blocks]


def encode(
    samples: Samples,
    *,
    channels: int,
    bits_per_sample: int,
    sample_rate: int,
    compression_level: int = 5,
    block_size: int | None = None,
    md5: bool = True,
    seek_points: int = 0,
    padding: int = 0,
    metadata: MetadataLike | None = None,
) -> bytes:
    """Encode PCM to a complete ``.flac`` file, byte-identical to libFLAC 1.4.3.

    ``block_size`` defaults to libFLAC's per-level choice (1152 for levels 0-2,
    4096 above). ``seek_points`` evenly spaced SEEKTABLE points and ``padding``
    bytes of PADDING are written after the VORBIS_COMMENT when non-zero.

    ``metadata`` replaces libFLAC's vendor-only VORBIS_COMMENT, e.g. with
    ``Metadata.build(tags=...)`` or another file's ``read_metadata(...).metadata``.
    It may not hold a SEEKTABLE (use ``seek_points``); a non-zero ``padding``
    replaces its PADDING.
    """
    return _native.encode(
        samples,
        channels=channels,
        bits_per_sample=bits_per_sample,
        sample_rate=sample_rate,
        compression_level=compression_level,
        block_size=block_size,
        md5=md5,
        seek_points=seek_points,
        padding=padding,
        metadata=_blocks(metadata),
    )


def encode_frames(
    samples: Samples,
    *,
    channels: int,
    bits_per_sample: int,
    sample_rate: int,
    compression_level: int = 5,
    block_size: int | None = None,
) -> bytes:
    """Encode PCM to raw FLAC frames, without the ``fLaC`` marker or metadata."""
    return _native.encode_frames(
        samples,
        channels=channels,
        bits_per_sample=bits_per_sample,
        sample_rate=sample_rate,
        compression_level=compression_level,
        block_size=block_size,
    )


def encode_ogg(
    samples: Samples,
    *,
    channels: int,
    bits_per_sample: int,
    sample_rate: int,
    compression_level: int = 5,
    block_size: int | None = None,
    md5: bool = True,
    serial: int = 0,
    metadata: MetadataLike | None = None,
) -> bytes:
    """Encode PCM to an Ogg FLAC stream with logical-bitstream ``serial``.

    ``metadata`` works as in ``encode``; any SEEKTABLE is dropped, as Ogg FLAC has
    none, and the VORBIS_COMMENT is moved first as the Ogg mapping requires.
    """
    return _native.encode_ogg(
        samples,
        channels=channels,
        bits_per_sample=bits_per_sample,
        sample_rate=sample_rate,
        compression_level=compression_level,
        block_size=block_size,
        md5=md5,
        serial=serial,
        metadata=_blocks(metadata),
    )


def decode(data: Buffer) -> DecodedAudio:
    """Decode a ``.flac`` file, verifying the audio MD5 when STREAMINFO has one.

    Raises ``FlacError`` on malformed input or any CRC mismatch.
    """
    return _wrap(_native.decode(data))


def decode_ogg(data: Buffer) -> DecodedAudio:
    """Decode an Ogg FLAC stream. Raises ``FlacError`` on malformed input."""
    return _wrap(_native.decode_ogg(data))


def decode_frames(data: Buffer) -> DecodedAudio:
    """Decode raw FLAC frames (``encode_frames`` output). Raises ``FlacError``."""
    return _wrap(_native.decode_frames(data))


def decode_seek(data: Buffer, target_sample: int) -> DecodedAudio:
    """Decode a ``.flac`` file from ``target_sample`` to the end.

    Uses the SEEKTABLE when present. Raises ``FlacError`` on malformed input or
    when ``target_sample`` is at or past the end of the stream.
    """
    return _wrap(_native.decode_seek(data, target_sample))


def read_metadata(data: Buffer) -> FlacInfo:
    """Read STREAMINFO and all metadata blocks of a ``.flac`` or Ogg FLAC stream.

    The container is detected from the leading bytes; audio frames are not read.
    Raises ``FlacError`` on malformed metadata.
    """
    si, blocks, container, serial = _native.read_metadata(data)
    return FlacInfo(
        StreamInfo.from_bytes(si),
        Metadata(tuple(MetadataBlock(t, d) for t, d in blocks)),
        container,
        serial,
    )


def remux(
    data: Buffer,
    to: Literal["flac", "ogg", "frames"],
    *,
    metadata: MetadataLike | None = None,
    serial: int | None = None,
    seek_points: int | None = None,
    padding: int | None = None,
) -> bytes:
    """Rewrap the audio frames of a ``.flac`` or Ogg FLAC stream without re-encoding.

    Frames are copied byte for byte; the input container is detected from its
    leading bytes, and every frame's CRC is checked. ``to`` may equal the input
    container, to change only the metadata.

    - ``metadata``: ``None`` keeps the source blocks, a ``Metadata`` replaces them.
    - ``serial``: the Ogg serial; defaults to the source's, or 0.
    - ``seek_points``: ``None`` keeps any SEEKTABLE, ``0`` removes it, ``n`` writes
      ``n`` spaced points from the real frame offsets (``.flac`` only).
    - ``padding``: ``None`` keeps PADDING as is, otherwise one block of that size
      replaces it (``0`` removes it).

    Ogg output drops the SEEKTABLE and puts the VORBIS_COMMENT first (adding a
    vendor-only one if missing), as the Ogg FLAC mapping requires. STREAMINFO is
    copied, with an unknown total sample count or frame size filled in from the
    frames. A leading ID3v2 tag on ``.flac`` input is dropped. Raises ``FlacError``
    on malformed or corrupt input.
    """
    return _native.remux(
        data,
        to=to,
        metadata=_blocks(metadata),
        serial=serial,
        seek_points=seek_points,
        padding=padding,
    )


async def _run(executor: Executor | None, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    # Cancelling the awaiting task does not stop a call that already started.
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, functools.partial(fn, *args, **kwargs))


async def encode_async(
    samples: Samples, *, executor: Executor | None = None, **options: Any
) -> bytes:
    """``encode`` in an executor; takes the same keyword options."""
    return await _run(executor, encode, samples, **options)


async def encode_frames_async(
    samples: Samples, *, executor: Executor | None = None, **options: Any
) -> bytes:
    """``encode_frames`` in an executor; takes the same keyword options."""
    return await _run(executor, encode_frames, samples, **options)


async def encode_ogg_async(
    samples: Samples, *, executor: Executor | None = None, **options: Any
) -> bytes:
    """``encode_ogg`` in an executor; takes the same keyword options."""
    return await _run(executor, encode_ogg, samples, **options)


async def decode_async(data: Buffer, *, executor: Executor | None = None) -> DecodedAudio:
    """``decode`` in an executor."""
    return await _run(executor, decode, data)


async def decode_ogg_async(data: Buffer, *, executor: Executor | None = None) -> DecodedAudio:
    """``decode_ogg`` in an executor."""
    return await _run(executor, decode_ogg, data)


async def decode_frames_async(data: Buffer, *, executor: Executor | None = None) -> DecodedAudio:
    """``decode_frames`` in an executor."""
    return await _run(executor, decode_frames, data)


async def decode_seek_async(
    data: Buffer, target_sample: int, *, executor: Executor | None = None
) -> DecodedAudio:
    """``decode_seek`` in an executor."""
    return await _run(executor, decode_seek, data, target_sample)


async def read_metadata_async(data: Buffer, *, executor: Executor | None = None) -> FlacInfo:
    """``read_metadata`` in an executor."""
    return await _run(executor, read_metadata, data)


async def remux_async(
    data: Buffer,
    to: Literal["flac", "ogg", "frames"],
    *,
    executor: Executor | None = None,
    **options: Any,
) -> bytes:
    """``remux`` in an executor; takes the same keyword options."""
    return await _run(executor, remux, data, to, **options)
