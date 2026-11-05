<!-- SPDX-License-Identifier: 0BSD -->
# API reference

Everything is in the `pyflac_rs` package and is typed (`py.typed`).
`Buffer` means any object with the buffer protocol: `bytes`, `bytearray`,
`memoryview`, a numpy array, and so on. `fmt` stands for
`channels=…, bits_per_sample=…, sample_rate=…`.

## Encoding

| Function | Returns |
| --- | --- |
| `encode(samples, *, fmt, compression_level=5, block_size=None, md5=True, seek_points=0, padding=0, metadata=None)` | `.flac` bytes |
| `encode_ogg(samples, *, fmt, compression_level=5, block_size=None, md5=True, serial=0, metadata=None)` | Ogg FLAC bytes |
| `encode_frames(samples, *, fmt, compression_level=5, block_size=None)` | raw frames |

## Decoding

| Function | Returns |
| --- | --- |
| `decode(data)` | `DecodedAudio`, MD5-verified |
| `decode_ogg(data)` | `DecodedAudio` |
| `decode_frames(data)` | `DecodedAudio` |
| `decode_seek(data, target_sample)` | `DecodedAudio` from `target_sample` (or the frame before it) to the end |

## Remux and metadata

| Function | Returns |
| --- | --- |
| `remux(data, to, *, metadata=None, serial=None, seek_points=None, padding=None)` | bytes in container `to` (`"flac"`, `"ogg"` or `"frames"`) |
| `read_metadata(data)` | `FlacInfo` |

## Async

Every function above has an `*_async` coroutine (`encode_async`,
`remux_async`, …) with the same arguments plus a keyword-only
`executor: concurrent.futures.Executor | None`.

## Types

`DecodedAudio` (frozen dataclass)
: `samples: bytes` (native-endian int32, interleaved), `channels`,
  `bits_per_sample`, `sample_rate`, `total_samples`, `md5: bytes | None`,
  `md5_ok: bool | None`, `first_sample`; `duration` (seconds),
  `to_numpy()`, `to_memoryview()`.

`FlacInfo`
: `stream_info: StreamInfo`, `metadata: Metadata`,
  `container: "flac" | "ogg"`, `serial: int | None` (0 to 2³²−1).

`StreamInfo`
: `min_block_size`, `max_block_size`, `min_frame_size`, `max_frame_size`
  (0 = unknown), `sample_rate`, `channels`, `bits_per_sample`,
  `total_samples` (0 = unknown), `md5: bytes | None`; `duration`.

`Metadata`
: `blocks: tuple[MetadataBlock, ...]`; `Metadata.build(tags=, pictures=,
  vendor=, blocks=)`; `vendor`, `tags`, `get(key)`, `pictures`;
  `with_tags(tags, vendor=None)`, `with_pictures(pictures)`,
  `without(*types)`.

`MetadataBlock`
: `type: int`, `data: bytes` (the body without the 4-byte header).

`Picture`
: `data`, `mime="image/jpeg"`, `type=3` (ID3v2 APIC, 3 = front cover),
  `description`, `width`, `height`, `depth`, `colors`; `to_block()`,
  `Picture.from_block(body)`.

`BlockType`
: `STREAMINFO`, `PADDING`, `APPLICATION`, `SEEKTABLE`, `VORBIS_COMMENT`,
  `CUESHEET`, `PICTURE`.

`Tags`
: a `Mapping[str, str | Iterable[str]]`, or an iterable of `(key, value)`
  pairs.

`FlacError`
: a `ValueError` subclass for malformed or corrupt FLAC/Ogg data.

`LIBFLAC_VENDOR_STRING`
: the vendor string libFLAC 1.4.3 writes (`reference libFLAC 1.4.3 20230623`).
