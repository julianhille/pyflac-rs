<!-- SPDX-License-Identifier: 0BSD -->
# Encoding and decoding

## Bit depths

Every depth from 4 to 32 bits **encodes** correctly: the reference `flac -t`
verifies the files, and `read_metadata` and `remux` handle them.

**Decoding** works for 8, 12, 16, 20, 24 and 32 bits. Other depths (4–7, 9–11,
13–15, 17–19, 21–23, 25–31) raise `FlacError` in `decode`, `decode_ogg`,
`decode_seek` and `decode_frames`. This is a limitation of libflac-rs 0.143.1:
a frame header can only name those six depths, and for any other depth it
points to STREAMINFO, which the libflac-rs decoder does not consult. Raw
frames (`decode_frames`) carry no STREAMINFO, so they can only ever decode the
six named depths.

## Samples

Samples are **interleaved signed integers** (`L R L R …`) in the range of
`bits_per_sample` (4–32 bits, see [Bit depths](#bit-depths)). The encoders accept:

- any int8, int16, int32 or int64 buffer: numpy arrays, `array.array`,
  `memoryview.cast("h")`, …
- a 2-D `(frames, channels)` numpy array, C- or Fortran-ordered;
- a sequence of Python `int`s.

Plain `bytes` are rejected: their sample width is ambiguous. Wrap them, e.g.
`memoryview(raw).cast("h")` for 16-bit little-endian PCM on a little-endian
machine, or `np.frombuffer(raw, "<i2")`.

The format is always given explicitly:

```python
flac = pyflac_rs.encode(
    pcm,
    channels=2,
    bits_per_sample=16,
    sample_rate=48_000,
    compression_level=8,   # 0-8, default 5
    block_size=None,       # libFLAC's per-level default (1152 or 4096)
    md5=True,              # STREAMINFO audio MD5
    seek_points=100,       # SEEKTABLE with 100 evenly spaced points
    padding=4096,          # PADDING block for later tag edits
)
```

With `compression_level=5` and no seek table or padding the output is byte for
byte what `flac -5 --no-seektable --no-padding` writes: libflac-rs is a
bit-exact port of libFLAC 1.4.3.

## Containers

| Function | Output |
| --- | --- |
| `encode` | a native `.flac` file (`fLaC`, metadata, frames) |
| `encode_ogg` | an Ogg FLAC stream (`.oga`), with `serial` for the logical bitstream |
| `encode_frames` | raw FLAC frames without the `fLaC` marker or any metadata |

## Decoding

`decode`, `decode_ogg` and `decode_frames` return a `DecodedAudio`:

```python
audio = pyflac_rs.decode(flac)
audio.channels, audio.bits_per_sample, audio.sample_rate
audio.total_samples          # per channel
audio.md5_ok                 # True, False, or None when there was no MD5
audio.to_numpy()             # read-only (frames, channels) int32, no copy
audio.to_memoryview()        # flat int32 view, no copy
audio.samples                # the raw native-endian int32 bytes
```

Every frame's CRC is checked, and a STREAMINFO MD5 is verified. A corrupt or
truncated stream raises `pyflac_rs.FlacError` (a `ValueError`).

`decode_seek(flac, n)` decodes from sample `n` to the end, using the SEEKTABLE
when the file has one. `audio.first_sample` says where the returned samples
start; it can be before `n` when seeking lands on a frame boundary.

## Memory

The codec is one-shot: the whole input and output are in memory. The input is
copied out of the Python object before the GIL is released, so changing a
buffer during the call cannot race with the encoder. `bytes` passed to the
decoders are read in place.
