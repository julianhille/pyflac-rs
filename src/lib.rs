//! Python bindings for `libflac-rs`.
//!
//! Every entry point copies its input out of Python objects while attached to the
//! interpreter, then runs the codec inside `Python::detach`, so the GIL is free
//! for other threads (and asyncio executors) for the whole encode/decode.

#![forbid(unsafe_code)]

mod container;
mod ogg;

use libflac_rs::{
    DecodedStream, Encoder, EncoderConfig, LIBFLAC_VENDOR_STRING, MetadataBlock, spaced_seek_points,
};
use pyo3::buffer::PyBuffer;
use pyo3::create_exception;
use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::pybacked::PyBackedBytes;
use pyo3::types::PyBytes;

use container::{Block, Seek, Target};

create_exception!(
    _native,
    FlacError,
    PyValueError,
    "Malformed, truncated or corrupt FLAC."
);

const MIN_BITS: u32 = 4;
const MAX_BITS: u32 = 32;
const MIN_BLOCK_SIZE: u32 = 16;
const MAX_BLOCK_SIZE: u32 = 65535;
const MAX_CHANNELS: u32 = 8;
const MAX_SAMPLE_RATE: u32 = (1 << 20) - 1;
const MAX_LEVEL: u32 = 8;
/// Metadata block lengths are 24-bit.
const MAX_PADDING: u32 = (1 << 24) - 1;
/// 18 bytes per seek point must fit the 24-bit block length.
const MAX_SEEK_POINTS: u32 = MAX_PADDING / 18;

/// Validated stream format and encoder settings.
struct Format {
    channels: u32,
    bits_per_sample: u32,
    sample_rate: u32,
    compression_level: u32,
    block_size: u32,
}

impl Format {
    fn new(
        channels: u32,
        bits_per_sample: u32,
        sample_rate: u32,
        compression_level: u32,
        block_size: Option<u32>,
    ) -> PyResult<Self> {
        if !(1..=MAX_CHANNELS).contains(&channels) {
            return Err(PyValueError::new_err(format!(
                "channels must be 1..={MAX_CHANNELS}"
            )));
        }
        if !(MIN_BITS..=MAX_BITS).contains(&bits_per_sample) {
            return Err(PyValueError::new_err(format!(
                "bits_per_sample must be {MIN_BITS}..={MAX_BITS}"
            )));
        }
        if !(1..=MAX_SAMPLE_RATE).contains(&sample_rate) {
            return Err(PyValueError::new_err(format!(
                "sample_rate must be 1..={MAX_SAMPLE_RATE}"
            )));
        }
        if compression_level > MAX_LEVEL {
            return Err(PyValueError::new_err(format!(
                "compression_level must be 0..={MAX_LEVEL}"
            )));
        }
        // libFLAC's per-level default: 1152 for levels 0-2, 4096 above.
        let block_size = block_size.unwrap_or(if compression_level <= 2 { 1152 } else { 4096 });
        if !(MIN_BLOCK_SIZE..=MAX_BLOCK_SIZE).contains(&block_size) {
            return Err(PyValueError::new_err(format!(
                "block_size must be {MIN_BLOCK_SIZE}..={MAX_BLOCK_SIZE}"
            )));
        }
        Ok(Self {
            channels,
            bits_per_sample,
            sample_rate,
            compression_level,
            block_size,
        })
    }

    fn encoder(&self, md5: bool) -> Encoder {
        Encoder::new(
            EncoderConfig::new(self.channels, self.bits_per_sample, self.sample_rate)
                .with_compression_level(self.compression_level)
                .with_block_size(self.block_size)
                .with_md5(md5),
        )
    }

    /// Reject samples outside the signed range of `bits_per_sample`. Runs detached.
    fn check_range(&self, samples: &[i32]) -> Result<(), String> {
        let bits = self.bits_per_sample;
        let max = ((1i64 << (bits - 1)) - 1) as i32;
        let min = (-(1i64 << (bits - 1))) as i32;
        match samples.iter().position(|&s| s < min || s > max) {
            None => Ok(()),
            Some(i) => Err(format!(
                "sample {} at index {i} is outside the {bits}-bit range {min}..={max}",
                samples[i]
            )),
        }
    }
}

/// Copy interleaved PCM out of a buffer (int8/16/32/64) or a sequence of ints.
///
/// A 2-D buffer is read as `(frames, channels)` and must have `channels` columns.
fn extract_samples(obj: &Bound<'_, PyAny>, channels: u32) -> PyResult<Vec<i32>> {
    let py = obj.py();
    let check_shape = |shape: &[usize]| -> PyResult<()> {
        match shape {
            [_] => Ok(()),
            [_, cols] if *cols == channels as usize => Ok(()),
            [_, cols] => Err(PyValueError::new_err(format!(
                "2-D samples must have shape (frames, {channels}), got {cols} columns"
            ))),
            _ => Err(PyValueError::new_err(
                "samples must be 1-D interleaved or 2-D (frames, channels)",
            )),
        }
    };
    let samples = if let Ok(buf) = PyBuffer::<i32>::get(obj) {
        check_shape(buf.shape())?;
        buf.to_vec(py)?
    } else if let Ok(buf) = PyBuffer::<i16>::get(obj) {
        check_shape(buf.shape())?;
        buf.to_vec(py)?.into_iter().map(i32::from).collect()
    } else if let Ok(buf) = PyBuffer::<i8>::get(obj) {
        check_shape(buf.shape())?;
        buf.to_vec(py)?.into_iter().map(i32::from).collect()
    } else if let Ok(buf) = PyBuffer::<i64>::get(obj) {
        check_shape(buf.shape())?;
        buf.to_vec(py)?
            .into_iter()
            .map(i32::try_from)
            .collect::<Result<_, _>>()
            .map_err(|_| PyValueError::new_err("int64 sample does not fit in 32 bits"))?
    } else if PyBuffer::<u8>::get(obj).is_ok() {
        // Raw bytes have no sample width; guessing one would silently corrupt audio.
        return Err(PyTypeError::new_err(
            "samples must be a signed integer buffer (int8/16/32/64), not raw bytes; \
             use e.g. memoryview(data).cast('i') or numpy.frombuffer(data, numpy.int32)",
        ));
    } else {
        obj.extract::<Vec<i32>>().map_err(|_| {
            PyTypeError::new_err("samples must be a signed integer buffer or a sequence of ints")
        })?
    };
    if samples.len() % channels as usize != 0 {
        return Err(PyValueError::new_err(format!(
            "sample count {} is not a multiple of channels ({channels})",
            samples.len()
        )));
    }
    Ok(samples)
}

/// `(type, body)` pairs from Python as checked metadata blocks.
fn to_blocks(metadata: Vec<(u8, Vec<u8>)>) -> PyResult<Vec<Block>> {
    let blocks: Vec<Block> = metadata
        .into_iter()
        .map(|(kind, data)| Block { kind, data })
        .collect();
    container::check_blocks(&blocks).map_err(PyValueError::new_err)?;
    Ok(blocks)
}

/// Blocks for a fresh encode. A SEEKTABLE describes existing frames, so new audio
/// gets one only through `seek_points`.
fn encode_blocks(metadata: Option<Vec<(u8, Vec<u8>)>>) -> PyResult<Option<Vec<Block>>> {
    let Some(metadata) = metadata else {
        return Ok(None);
    };
    let blocks = to_blocks(metadata)?;
    if blocks.iter().any(|b| b.kind == container::SEEKTABLE) {
        return Err(PyValueError::new_err(
            "metadata for new audio cannot contain a SEEKTABLE; use seek_points",
        ));
    }
    Ok(Some(blocks))
}

/// Encoded bytes, or the reason a detached step failed.
fn into_bytes(py: Python<'_>, result: Result<Vec<u8>, String>) -> PyResult<Py<PyBytes>> {
    result
        .map(|out| PyBytes::new(py, &out).unbind())
        .map_err(PyValueError::new_err)
}

/// Encode interleaved PCM to a complete `.flac` file.
#[pyfunction]
#[pyo3(signature = (
    samples, *, channels, bits_per_sample, sample_rate,
    compression_level = 5, block_size = None, md5 = true, seek_points = 0, padding = 0,
    metadata = None,
))]
#[allow(clippy::too_many_arguments)]
fn encode(
    py: Python<'_>,
    samples: &Bound<'_, PyAny>,
    channels: u32,
    bits_per_sample: u32,
    sample_rate: u32,
    compression_level: u32,
    block_size: Option<u32>,
    md5: bool,
    seek_points: u32,
    padding: u32,
    metadata: Option<Vec<(u8, Vec<u8>)>>,
) -> PyResult<Py<PyBytes>> {
    let fmt = Format::new(
        channels,
        bits_per_sample,
        sample_rate,
        compression_level,
        block_size,
    )?;
    if seek_points > MAX_SEEK_POINTS {
        return Err(PyValueError::new_err(format!(
            "seek_points must be 0..={MAX_SEEK_POINTS}"
        )));
    }
    if padding > MAX_PADDING {
        return Err(PyValueError::new_err(format!(
            "padding must be 0..={MAX_PADDING}"
        )));
    }
    let blocks = encode_blocks(metadata)?;
    let pcm = extract_samples(samples, channels)?;
    let result = py.detach(move || {
        fmt.check_range(&pcm)?;
        if let Some(blocks) = blocks {
            let flac = fmt.encoder(md5).encode(&pcm);
            let stream = container::parse_flac(&flac, true)?;
            let padding = (padding > 0).then_some(padding);
            return container::rebuild(
                stream,
                blocks,
                Seek::Spaced(seek_points),
                padding,
                Target::Flac,
                0,
            );
        }
        let total = (pcm.len() / fmt.channels as usize) as u64;
        let points = spaced_seek_points(seek_points, total);
        let mut blocks = vec![MetadataBlock::VorbisComment(LIBFLAC_VENDOR_STRING)];
        if !points.is_empty() {
            blocks.push(MetadataBlock::Seektable(&points));
        }
        if padding > 0 {
            blocks.push(MetadataBlock::Padding(padding));
        }
        Ok(fmt.encoder(md5).encode_with_metadata(&pcm, &blocks))
    });
    into_bytes(py, result)
}

/// Encode interleaved PCM to raw FLAC frames (no `fLaC` marker or metadata).
#[pyfunction]
#[pyo3(signature = (
    samples, *, channels, bits_per_sample, sample_rate, compression_level = 5, block_size = None,
))]
fn encode_frames(
    py: Python<'_>,
    samples: &Bound<'_, PyAny>,
    channels: u32,
    bits_per_sample: u32,
    sample_rate: u32,
    compression_level: u32,
    block_size: Option<u32>,
) -> PyResult<Py<PyBytes>> {
    let fmt = Format::new(
        channels,
        bits_per_sample,
        sample_rate,
        compression_level,
        block_size,
    )?;
    let pcm = extract_samples(samples, channels)?;
    let result = py.detach(move || {
        fmt.check_range(&pcm)?;
        Ok(fmt.encoder(false).encode_frames(&pcm))
    });
    into_bytes(py, result)
}

/// Encode interleaved PCM to an Ogg FLAC stream.
#[pyfunction]
#[pyo3(signature = (
    samples, *, channels, bits_per_sample, sample_rate,
    compression_level = 5, block_size = None, md5 = true, serial = 0, metadata = None,
))]
#[allow(clippy::too_many_arguments)]
fn encode_ogg(
    py: Python<'_>,
    samples: &Bound<'_, PyAny>,
    channels: u32,
    bits_per_sample: u32,
    sample_rate: u32,
    compression_level: u32,
    block_size: Option<u32>,
    md5: bool,
    serial: u32,
    metadata: Option<Vec<(u8, Vec<u8>)>>,
) -> PyResult<Py<PyBytes>> {
    let fmt = Format::new(
        channels,
        bits_per_sample,
        sample_rate,
        compression_level,
        block_size,
    )?;
    let blocks = encode_blocks(metadata)?;
    let pcm = extract_samples(samples, channels)?;
    let result = py.detach(move || {
        fmt.check_range(&pcm)?;
        let Some(blocks) = blocks else {
            return Ok(fmt.encoder(md5).encode_ogg(&pcm, serial as i32));
        };
        let flac = fmt.encoder(md5).encode(&pcm);
        let stream = container::parse_flac(&flac, true)?;
        container::rebuild(stream, blocks, Seek::Keep, None, Target::Ogg, serial)
    });
    into_bytes(py, result)
}

/// Input bytes: `bytes`/`bytearray` via `PyBackedBytes` (zero-copy for `bytes`),
/// any other byte buffer (memoryview, mmap, numpy uint8) copied.
enum Input {
    Backed(PyBackedBytes),
    Owned(Vec<u8>),
}

impl std::ops::Deref for Input {
    type Target = [u8];
    fn deref(&self) -> &[u8] {
        match self {
            Input::Backed(b) => b,
            Input::Owned(v) => v,
        }
    }
}

fn extract_input(obj: &Bound<'_, PyAny>) -> PyResult<Input> {
    if let Ok(b) = obj.extract::<PyBackedBytes>() {
        return Ok(Input::Backed(b));
    }
    let buf =
        PyBuffer::<u8>::get(obj).map_err(|_| PyTypeError::new_err("data must be bytes-like"))?;
    Ok(Input::Owned(buf.to_vec(obj.py())?))
}

/// Native-endian `int32` bytes of `samples`, built while detached.
fn pcm_bytes(samples: &[i32]) -> Vec<u8> {
    let mut out = Vec::with_capacity(samples.len() * 4);
    for s in samples {
        out.extend_from_slice(&s.to_ne_bytes());
    }
    out
}

/// `(samples, channels, bits_per_sample, sample_rate, total_samples, md5, md5_ok,
/// first_sample)`; the Python layer wraps it in `DecodedAudio`.
type Decoded = (
    Vec<u8>,
    u32,
    u32,
    u32,
    u64,
    Option<[u8; 16]>,
    Option<bool>,
    u64,
);

fn from_stream(s: DecodedStream) -> Decoded {
    let md5 = (s.md5 != [0u8; 16]).then_some(s.md5);
    (
        pcm_bytes(&s.interleaved),
        s.channels,
        s.bits_per_sample,
        s.sample_rate,
        s.total_samples,
        md5,
        Some(s.md5_ok),
        0,
    )
}

fn into_py(py: Python<'_>, decoded: Option<Decoded>, what: &str) -> PyResult<Py<PyAny>> {
    let (pcm, channels, bits, rate, total, md5, md5_ok, first) =
        decoded.ok_or_else(|| FlacError::new_err(format!("invalid or corrupt {what}")))?;
    let md5 = md5.map(|m| PyBytes::new(py, &m));
    let tuple = (
        PyBytes::new(py, &pcm),
        channels,
        bits,
        rate,
        total,
        md5,
        md5_ok,
        first,
    );
    Ok(tuple.into_pyobject(py)?.into_any().unbind())
}

/// Run a libflac-rs decoder, turning a panic into `None` (a FlacError).
/// libflac-rs 0.143.1 can index out of bounds on some corrupt residuals
/// (`decoder.rs`, partition sample count underflow) instead of failing cleanly.
fn guarded<T>(f: impl FnOnce() -> Option<T>) -> Option<T> {
    std::panic::catch_unwind(std::panic::AssertUnwindSafe(f)).unwrap_or(None)
}

/// Decode a complete `.flac` file.
#[pyfunction]
fn decode(py: Python<'_>, data: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    let input = extract_input(data)?;
    let decoded = py.detach(move || guarded(|| libflac_rs::decode(&input).map(from_stream)));
    into_py(py, decoded, "FLAC stream")
}

/// Decode an Ogg FLAC stream.
#[pyfunction]
fn decode_ogg(py: Python<'_>, data: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    let input = extract_input(data)?;
    let decoded = py.detach(move || guarded(|| libflac_rs::decode_ogg(&input).map(from_stream)));
    into_py(py, decoded, "Ogg FLAC stream")
}

/// Decode raw FLAC frames (as produced by `encode_frames`).
#[pyfunction]
fn decode_frames(py: Python<'_>, data: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    let input = extract_input(data)?;
    let decoded = py.detach(move || {
        guarded(|| libflac_rs::decode_frames(&input)).map(|f| {
            let total = f
                .interleaved
                .len()
                .checked_div(f.channels as usize)
                .unwrap_or(0);
            (
                pcm_bytes(&f.interleaved),
                f.channels,
                f.bits_per_sample,
                f.sample_rate,
                total as u64,
                None,
                None,
                0,
            )
        })
    });
    into_py(py, decoded, "FLAC frame stream")
}

/// Decode a `.flac` file from `target_sample` to the end, using its SEEKTABLE.
#[pyfunction]
fn decode_seek(py: Python<'_>, data: &Bound<'_, PyAny>, target_sample: u64) -> PyResult<Py<PyAny>> {
    let input = extract_input(data)?;
    let decoded = py.detach(move || {
        guarded(|| libflac_rs::decode_seek(&input, target_sample)).map(|r| {
            let total = r
                .interleaved
                .len()
                .checked_div(r.channels as usize)
                .unwrap_or(0);
            (
                pcm_bytes(&r.interleaved),
                r.channels,
                r.bits_per_sample,
                r.sample_rate,
                total as u64,
                None,
                None,
                r.first_sample,
            )
        })
    });
    into_py(py, decoded, "FLAC stream or target_sample past the end")
}

/// `(streaminfo, [(type, body)], container, serial)` of a native or Ogg FLAC stream,
/// read without touching its audio frames.
#[pyfunction]
fn read_metadata(py: Python<'_>, data: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    let input = extract_input(data)?;
    let parsed = py.detach(move || {
        container::parse(&input, false).map(|s| {
            let blocks: Vec<(u8, Vec<u8>)> =
                s.blocks.into_iter().map(|b| (b.kind, b.data)).collect();
            (s.streaminfo, blocks, s.serial)
        })
    });
    let (si, blocks, serial) = parsed.map_err(FlacError::new_err)?;
    let blocks: Vec<_> = blocks
        .iter()
        .map(|(k, d)| (*k, PyBytes::new(py, d)))
        .collect();
    let kind = if serial.is_some() { "ogg" } else { "flac" };
    let tuple = (PyBytes::new(py, &si), blocks, kind, serial);
    Ok(tuple.into_pyobject(py)?.into_any().unbind())
}

/// Copy the audio frames of a native or Ogg FLAC stream into another container
/// byte for byte, keeping its metadata unless `metadata` replaces it.
#[pyfunction]
#[pyo3(signature = (data, *, to, metadata = None, serial = None, seek_points = None, padding = None))]
fn remux(
    py: Python<'_>,
    data: &Bound<'_, PyAny>,
    to: &str,
    metadata: Option<Vec<(u8, Vec<u8>)>>,
    serial: Option<u32>,
    seek_points: Option<u32>,
    padding: Option<u32>,
) -> PyResult<Py<PyBytes>> {
    let target = match to {
        "flac" => Target::Flac,
        "ogg" => Target::Ogg,
        "frames" => Target::Frames,
        _ => {
            return Err(PyValueError::new_err(
                "to must be 'flac', 'ogg' or 'frames'",
            ));
        }
    };
    if seek_points.is_some_and(|n| n > MAX_SEEK_POINTS) {
        return Err(PyValueError::new_err(format!(
            "seek_points must be 0..={MAX_SEEK_POINTS}"
        )));
    }
    if padding.is_some_and(|n| n > MAX_PADDING) {
        return Err(PyValueError::new_err(format!(
            "padding must be 0..={MAX_PADDING}"
        )));
    }
    let blocks = metadata.map(to_blocks).transpose()?;
    let input = extract_input(data)?;
    let result = py.detach(move || {
        let stream = container::parse(&input, true)?;
        let serial = serial.or(stream.serial).unwrap_or(0);
        let blocks = blocks.unwrap_or_else(|| stream.blocks.clone());
        let seek = seek_points.map_or(Seek::Keep, Seek::Spaced);
        container::rebuild(stream, blocks, seek, padding, target, serial)
    });
    let out = result.map_err(FlacError::new_err)?;
    Ok(PyBytes::new(py, &out).unbind())
}

#[pymodule(gil_used = false)]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("FlacError", m.py().get_type::<FlacError>())?;
    m.add("LIBFLAC_VENDOR_STRING", LIBFLAC_VENDOR_STRING)?;
    m.add_function(wrap_pyfunction!(encode, m)?)?;
    m.add_function(wrap_pyfunction!(encode_frames, m)?)?;
    m.add_function(wrap_pyfunction!(encode_ogg, m)?)?;
    m.add_function(wrap_pyfunction!(decode, m)?)?;
    m.add_function(wrap_pyfunction!(decode_ogg, m)?)?;
    m.add_function(wrap_pyfunction!(decode_frames, m)?)?;
    m.add_function(wrap_pyfunction!(decode_seek, m)?)?;
    m.add_function(wrap_pyfunction!(read_metadata, m)?)?;
    m.add_function(wrap_pyfunction!(remux, m)?)?;
    Ok(())
}
