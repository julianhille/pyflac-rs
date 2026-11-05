"""FLAC metadata blocks, Vorbis comments (tags) and pictures.

Blocks are kept as raw ``(type, body)`` pairs, so anything the stream carries,
including APPLICATION and CUESHEET blocks this module does not interpret, is
preserved unchanged. Tags and pictures are parsed views over those bytes.
"""

from __future__ import annotations

import struct
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import IntEnum
from typing import Literal

from ._native import LIBFLAC_VENDOR_STRING, FlacError

__all__ = [
    "BlockType",
    "FlacInfo",
    "Metadata",
    "MetadataBlock",
    "Picture",
    "StreamInfo",
    "Tags",
]

Tags = Mapping[str, str | Iterable[str]] | Iterable[tuple[str, str]]
"""Tags as a mapping (a value may be a list) or as ``(key, value)`` pairs."""


class BlockType(IntEnum):
    STREAMINFO = 0
    PADDING = 1
    APPLICATION = 2
    SEEKTABLE = 3
    VORBIS_COMMENT = 4
    CUESHEET = 5
    PICTURE = 6


@dataclass(frozen=True, slots=True)
class MetadataBlock:
    """One metadata block: its type (1-126) and body, without the 4-byte header."""

    type: int
    data: bytes


@dataclass(frozen=True, slots=True)
class StreamInfo:
    """The STREAMINFO block, which always comes from the audio itself."""

    min_block_size: int
    max_block_size: int
    min_frame_size: int
    """0 when unknown."""
    max_frame_size: int
    """0 when unknown."""
    sample_rate: int
    channels: int
    bits_per_sample: int
    total_samples: int
    """0 when unknown."""
    md5: bytes | None
    """``None`` when the encoder did not compute one."""

    @classmethod
    def from_bytes(cls, body: bytes) -> StreamInfo:
        min_bs, max_bs = struct.unpack_from(">HH", body)
        min_fs = int.from_bytes(body[4:7])
        max_fs = int.from_bytes(body[7:10])
        packed = int.from_bytes(body[10:18])
        md5 = body[18:34]
        return cls(
            min_bs,
            max_bs,
            min_fs,
            max_fs,
            sample_rate=packed >> 44,
            channels=((packed >> 41) & 7) + 1,
            bits_per_sample=((packed >> 36) & 31) + 1,
            total_samples=packed & ((1 << 36) - 1),
            md5=md5 if any(md5) else None,
        )

    @property
    def duration(self) -> float:
        return self.total_samples / self.sample_rate if self.sample_rate else 0.0


@dataclass(frozen=True, slots=True)
class Picture:
    """A PICTURE block. ``type`` follows ID3v2 APIC (3 = front cover)."""

    data: bytes
    mime: str = "image/jpeg"
    type: int = 3
    description: str = ""
    width: int = 0
    height: int = 0
    depth: int = 0
    colors: int = 0

    @classmethod
    def from_block(cls, body: bytes) -> Picture:
        try:
            kind, n = struct.unpack_from(">II", body)
            mime = body[8 : 8 + n].decode("ascii")
            pos = 8 + n
            (n,) = struct.unpack_from(">I", body, pos)
            description = body[pos + 4 : pos + 4 + n].decode("utf-8")
            pos += 4 + n
            width, height, depth, colors, n = struct.unpack_from(">5I", body, pos)
            data = body[pos + 20 : pos + 20 + n]
        except (struct.error, UnicodeDecodeError) as e:
            raise FlacError(f"malformed PICTURE block: {e}") from None
        if len(data) != n:
            raise FlacError("malformed PICTURE block: truncated picture data")
        return cls(data, mime, kind, description, width, height, depth, colors)

    def to_block(self) -> MetadataBlock:
        mime = self.mime.encode("ascii")
        description = self.description.encode("utf-8")
        body = b"".join(
            [
                struct.pack(">II", self.type, len(mime)),
                mime,
                struct.pack(">I", len(description)),
                description,
                struct.pack(
                    ">5I", self.width, self.height, self.depth, self.colors, len(self.data)
                ),
                self.data,
            ]
        )
        return MetadataBlock(BlockType.PICTURE, body)


def _parse_comment(body: bytes) -> tuple[str, list[tuple[str, str]]]:
    try:
        (n,) = struct.unpack_from("<I", body)
        vendor = body[4 : 4 + n].decode("utf-8")
        pos = 4 + n
        (count,) = struct.unpack_from("<I", body, pos)
        pos += 4
        tags = []
        for _ in range(count):
            (n,) = struct.unpack_from("<I", body, pos)
            entry = body[pos + 4 : pos + 4 + n].decode("utf-8")
            if len(entry.encode("utf-8")) != n:
                raise FlacError("malformed VORBIS_COMMENT block: truncated comment")
            pos += 4 + n
            key, sep, value = entry.partition("=")
            if not sep:
                raise FlacError(f"malformed VORBIS_COMMENT entry without '=': {key!r}")
            tags.append((key, value))
    except (struct.error, UnicodeDecodeError) as e:
        raise FlacError(f"malformed VORBIS_COMMENT block: {e}") from None
    return vendor, tags


def _pairs(tags: Tags) -> list[tuple[str, str]]:
    if isinstance(tags, Mapping):
        pairs = []
        for key, value in tags.items():
            values = [value] if isinstance(value, str) else value
            pairs.extend((key, v) for v in values)
    else:
        pairs = [(k, v) for k, v in tags]
    for key, _ in pairs:
        if not key or not all(0x20 <= ord(c) <= 0x7D and c != "=" for c in key):
            raise ValueError(f"invalid tag name {key!r}: use printable ASCII without '='")
    return pairs


def _comment_block(vendor: str, tags: Tags) -> MetadataBlock:
    pairs = _pairs(tags)
    v = vendor.encode("utf-8")
    parts = [struct.pack("<I", len(v)), v, struct.pack("<I", len(pairs))]
    for key, value in pairs:
        entry = f"{key}={value}".encode()
        parts += [struct.pack("<I", len(entry)), entry]
    return MetadataBlock(BlockType.VORBIS_COMMENT, b"".join(parts))


@dataclass(frozen=True, slots=True)
class Metadata:
    """The metadata blocks after STREAMINFO, in stream order.

    Immutable: the ``with_*``/``without`` methods return a new ``Metadata``.
    """

    blocks: tuple[MetadataBlock, ...] = ()

    @classmethod
    def build(
        cls,
        *,
        tags: Tags = (),
        pictures: Iterable[Picture] = (),
        vendor: str = LIBFLAC_VENDOR_STRING,
        blocks: Iterable[MetadataBlock] = (),
    ) -> Metadata:
        """A VORBIS_COMMENT with ``vendor`` and ``tags``, then ``pictures``, then
        any other ``blocks``."""
        return cls((_comment_block(vendor, tags), *(p.to_block() for p in pictures), *blocks))

    def _comment(self) -> tuple[str, list[tuple[str, str]]] | None:
        for block in self.blocks:
            if block.type == BlockType.VORBIS_COMMENT:
                return _parse_comment(block.data)
        return None

    @property
    def vendor(self) -> str | None:
        """The VORBIS_COMMENT vendor string, or ``None`` without a VORBIS_COMMENT."""
        comment = self._comment()
        return comment[0] if comment else None

    @property
    def tags(self) -> tuple[tuple[str, str], ...]:
        """``(key, value)`` pairs in stream order, keys as written."""
        comment = self._comment()
        return tuple(comment[1]) if comment else ()

    def get(self, key: str) -> list[str]:
        """All values of tag ``key``, matched case-insensitively."""
        key = key.upper()
        return [v for k, v in self.tags if k.upper() == key]

    @property
    def pictures(self) -> tuple[Picture, ...]:
        return tuple(Picture.from_block(b.data) for b in self.blocks if b.type == BlockType.PICTURE)

    def with_tags(self, tags: Tags, *, vendor: str | None = None) -> Metadata:
        """Replace all tags (in place of the existing VORBIS_COMMENT, else first).

        The vendor string is kept unless ``vendor`` is given.
        """
        block = _comment_block(vendor or self.vendor or LIBFLAC_VENDOR_STRING, tags)
        blocks = list(self.blocks)
        for i, b in enumerate(blocks):
            if b.type == BlockType.VORBIS_COMMENT:
                blocks[i] = block
                break
        else:
            blocks.insert(0, block)
        return Metadata(tuple(blocks))

    def with_pictures(self, pictures: Iterable[Picture]) -> Metadata:
        """Replace all pictures; the new ones follow the other blocks."""
        kept = (b for b in self.blocks if b.type != BlockType.PICTURE)
        return Metadata((*kept, *(p.to_block() for p in pictures)))

    def without(self, *types: int) -> Metadata:
        """Drop every block of the given types."""
        return Metadata(tuple(b for b in self.blocks if b.type not in types))


@dataclass(frozen=True, slots=True)
class FlacInfo:
    """What ``read_metadata`` finds in a stream."""

    stream_info: StreamInfo
    metadata: Metadata
    container: Literal["flac", "ogg"]
    serial: int | None
    """The Ogg logical-bitstream serial, for Ogg input."""
