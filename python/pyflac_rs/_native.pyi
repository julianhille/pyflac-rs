from collections.abc import Buffer, Sequence
from typing import Literal, TypeAlias

LIBFLAC_VENDOR_STRING: str

class FlacError(ValueError): ...

_Blocks: TypeAlias = list[tuple[int, bytes]]
_Raw: TypeAlias = tuple[bytes, int, int, int, int, bytes | None, bool | None, int]

def encode(
    samples: Buffer | Sequence[int],
    *,
    channels: int,
    bits_per_sample: int,
    sample_rate: int,
    compression_level: int = 5,
    block_size: int | None = None,
    md5: bool = True,
    seek_points: int = 0,
    padding: int = 0,
    metadata: _Blocks | None = None,
) -> bytes: ...
def encode_frames(
    samples: Buffer | Sequence[int],
    *,
    channels: int,
    bits_per_sample: int,
    sample_rate: int,
    compression_level: int = 5,
    block_size: int | None = None,
) -> bytes: ...
def encode_ogg(
    samples: Buffer | Sequence[int],
    *,
    channels: int,
    bits_per_sample: int,
    sample_rate: int,
    compression_level: int = 5,
    block_size: int | None = None,
    md5: bool = True,
    serial: int = 0,
    metadata: _Blocks | None = None,
) -> bytes: ...
def decode(data: Buffer) -> _Raw: ...
def decode_ogg(data: Buffer) -> _Raw: ...
def decode_frames(data: Buffer) -> _Raw: ...
def decode_seek(data: Buffer, target_sample: int) -> _Raw: ...
def read_metadata(
    data: Buffer,
) -> tuple[bytes, _Blocks, Literal["flac", "ogg"], int | None]: ...
def remux(
    data: Buffer,
    *,
    to: Literal["flac", "ogg", "frames"],
    metadata: _Blocks | None = None,
    serial: int | None = None,
    seek_points: int | None = None,
    padding: int | None = None,
) -> bytes: ...
