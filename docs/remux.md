<!-- SPDX-License-Identifier: 0BSD -->
# Remux and metadata

## Remux

`remux(data, to)` moves the audio frames between `.flac` and Ogg FLAC **byte
for byte**, without decoding or re-encoding:

```python
ogg = pyflac_rs.remux(flac, "ogg", serial=7)
flac = pyflac_rs.remux(ogg, "flac", seek_points=50)
frames = pyflac_rs.remux(flac, "frames")   # just the frames
```

- The input container is detected from its first bytes. A leading ID3v2 tag
  on `.flac` input is skipped.
- Every frame's CRC (and every Ogg page CRC) is checked; corrupt input raises
  `FlacError`.
- STREAMINFO is copied. An unknown total sample count or frame size is filled
  in from the frames.
- `to` may be the input's own container, which changes only the metadata.

Options:

| Option | `None` (default) | Value |
| --- | --- | --- |
| `metadata` | keep the source blocks | a `Metadata` replaces them |
| `serial` | the source's Ogg serial, else 0 | the Ogg serial, 0 to 2³²−1 |
| `seek_points` | keep any SEEKTABLE | `0` removes it, `n` writes `n` points from the real frame offsets (`.flac` only) |
| `padding` | keep PADDING as is | one block of that size replaces it, `0` removes it |

Ogg FLAC has no SEEKTABLE and requires the VORBIS_COMMENT right after
STREAMINFO, so Ogg output drops the SEEKTABLE and reorders (or adds a
vendor-only) VORBIS_COMMENT. A `.flac` → Ogg → `.flac` round trip keeps
everything else: tags, pictures, cue sheets, application blocks and padding.

## Reading metadata

```python
info = pyflac_rs.read_metadata(data)      # .flac or Ogg FLAC; frames are not read
info.container                            # "flac" or "ogg"
info.serial                               # Ogg serial, or None
info.stream_info.sample_rate, info.stream_info.total_samples, info.stream_info.md5
m = info.metadata
m.vendor                                  # VORBIS_COMMENT vendor string
m.tags                                    # (("TITLE", "…"), ("ARTIST", "A"), …) in stream order
m.get("artist")                           # ["A", "B"], case-insensitive
m.pictures                                # (Picture(...), …)
m.blocks                                  # every block after STREAMINFO, raw
```

## Writing metadata

`Metadata` is immutable. Build one, or derive one from a file:

```python
from pyflac_rs import BlockType, Metadata, Picture

meta = Metadata.build(
    tags={"TITLE": "Song", "ARTIST": ["A", "B"]},   # dict, or (key, value) pairs
    pictures=[Picture(png_bytes, mime="image/png", description="cover")],
)
flac = pyflac_rs.encode(pcm, metadata=meta, padding=1024, **fmt)

# Change the tags of an existing file without touching the audio:
src = pyflac_rs.read_metadata(flac).metadata
new = src.with_tags({"TITLE": "New title"}).without(BlockType.PICTURE)
flac = pyflac_rs.remux(flac, "flac", metadata=new)
```

- `with_tags(tags, vendor=None)` replaces all tags and keeps the vendor string.
- `with_pictures(pictures)` replaces all pictures.
- `without(*types)` drops blocks of the given types.
- Tag names must be printable ASCII without `=`. Values are UTF-8.

The encoders take `metadata=` too. It replaces libFLAC's vendor-only
VORBIS_COMMENT. It may not hold a STREAMINFO or a SEEKTABLE (use
`seek_points`), and a non-zero `padding` replaces its PADDING.
