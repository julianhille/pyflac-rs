//! Container-level FLAC: metadata blocks and audio frames as opaque bytes.
//!
//! Remuxing never decodes audio. Native streams are split into frames by sync code,
//! header CRC-8, frame CRC-16 and frame/sample-number continuity; Ogg FLAC packets
//! already are frames. Frames are copied byte for byte between containers, which is
//! lossless, and the Ogg side is paged exactly as libFLAC + libogg page it.

use std::borrow::Cow;

use libflac_rs::LIBFLAC_VENDOR_STRING;

use crate::ogg::{OggStream, crc_update};

pub const STREAMINFO: u8 = 0;
pub const PADDING: u8 = 1;
pub const SEEKTABLE: u8 = 3;
pub const VORBIS_COMMENT: u8 = 4;
/// Metadata block lengths are 24-bit.
pub const MAX_BLOCK_LEN: usize = (1 << 24) - 1;
const STREAMINFO_LEN: usize = 34;
const SEEKPOINT_LEN: usize = 18;
const PLACEHOLDER: u64 = u64::MAX;

/// A metadata block other than STREAMINFO: its type and body (no 4-byte header).
#[derive(Clone)]
pub struct Block {
    pub kind: u8,
    pub data: Vec<u8>,
}

/// One audio frame and the number of inter-channel samples it holds.
pub struct Frame<'a> {
    pub bytes: Cow<'a, [u8]>,
    pub samples: u64,
}

/// A parsed stream. `frames` is empty when parsed with `frames = false`.
pub struct Stream<'a> {
    pub streaminfo: [u8; STREAMINFO_LEN],
    pub blocks: Vec<Block>,
    pub frames: Vec<Frame<'a>>,
    /// The Ogg logical-bitstream serial, for Ogg input.
    pub serial: Option<u32>,
}

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Target {
    Flac,
    Ogg,
    Frames,
}

/// What to do with the SEEKTABLE when rebuilding.
#[derive(Clone, Copy)]
pub enum Seek {
    /// Keep the given blocks' SEEKTABLE, if any.
    Keep,
    /// Replace it with `n` evenly spaced points (0 removes it).
    Spaced(u32),
}

pub type Result<T> = std::result::Result<T, String>;

const fn crc8_table() -> [u8; 256] {
    let mut t = [0u8; 256];
    let mut i = 0;
    while i < 256 {
        let mut c = i as u8;
        let mut j = 0;
        while j < 8 {
            c = if c & 0x80 != 0 {
                (c << 1) ^ 0x07
            } else {
                c << 1
            };
            j += 1;
        }
        t[i] = c;
        i += 1;
    }
    t
}

const fn crc16_table() -> [u16; 256] {
    let mut t = [0u16; 256];
    let mut i = 0;
    while i < 256 {
        let mut c = (i as u16) << 8;
        let mut j = 0;
        while j < 8 {
            c = if c & 0x8000 != 0 {
                (c << 1) ^ 0x8005
            } else {
                c << 1
            };
            j += 1;
        }
        t[i] = c;
        i += 1;
    }
    t
}

static CRC8: [u8; 256] = crc8_table();
static CRC16: [u16; 256] = crc16_table();

fn crc8(bytes: &[u8]) -> u8 {
    bytes.iter().fold(0, |c, &b| CRC8[(c ^ b) as usize])
}

fn crc16_step(c: u16, b: u8) -> u16 {
    (c << 8) ^ CRC16[((c >> 8) as u8 ^ b) as usize]
}

/// A frame header: its length, sample count, and coded frame/sample number.
struct Header {
    len: usize,
    samples: u64,
    variable: bool,
    number: u64,
}

/// Parse and CRC-8-check the frame header at the start of `d`.
fn frame_header(d: &[u8]) -> Option<Header> {
    if d.len() < 6 || d[0] != 0xFF || d[1] & 0xFE != 0xF8 {
        return None;
    }
    let bs_code = d[2] >> 4;
    let sr_code = d[2] & 0x0F;
    if bs_code == 0 || sr_code == 15 || d[3] >> 4 > 10 || d[3] & 1 != 0 || (d[3] >> 1) & 7 == 3 {
        return None;
    }
    let (mut number, extra) = match d[4] {
        b if b & 0x80 == 0 => (b as u64, 0),
        b if b & 0xE0 == 0xC0 => ((b & 0x1F) as u64, 1),
        b if b & 0xF0 == 0xE0 => ((b & 0x0F) as u64, 2),
        b if b & 0xF8 == 0xF0 => ((b & 0x07) as u64, 3),
        b if b & 0xFC == 0xF8 => ((b & 0x03) as u64, 4),
        b if b & 0xFE == 0xFC => ((b & 0x01) as u64, 5),
        0xFE => (0, 6),
        _ => return None,
    };
    let mut i = 5;
    for _ in 0..extra {
        let b = *d.get(i)?;
        if b & 0xC0 != 0x80 {
            return None;
        }
        number = (number << 6) | (b & 0x3F) as u64;
        i += 1;
    }
    let samples = match bs_code {
        1 => 192,
        2..=5 => 576 << (bs_code - 2),
        6 => {
            i += 1;
            *d.get(i - 1)? as u64 + 1
        }
        7 => {
            i += 2;
            u16::from_be_bytes([*d.get(i - 2)?, *d.get(i - 1)?]) as u64 + 1
        }
        _ => 256 << (bs_code - 8),
    };
    i += match sr_code {
        12 => 1,
        13 | 14 => 2,
        _ => 0,
    };
    if crc8(d.get(..i)?) != *d.get(i)? {
        return None;
    }
    Some(Header {
        len: i + 1,
        samples,
        variable: d[1] & 1 != 0,
        number,
    })
}

/// Check one complete frame (an Ogg packet): header and whole-frame CRC-16.
fn whole_frame(d: &[u8]) -> Option<u64> {
    let h = frame_header(d)?;
    (d.len() >= h.len + 2 && d.iter().fold(0, |c, &b| crc16_step(c, b)) == 0).then_some(h.samples)
}

/// Split concatenated native frames. A frame ends where the running CRC-16 is zero
/// and the next valid header carries the next frame/sample number, or at the end.
fn split_frames(d: &[u8], base: usize) -> Result<Vec<Frame<'_>>> {
    let mut frames = Vec::new();
    let mut pos = 0;
    while pos < d.len() {
        let corrupt = || format!("corrupt or truncated FLAC frame at byte {}", base + pos);
        let h = frame_header(&d[pos..]).ok_or_else(corrupt)?;
        let next = if h.variable {
            h.number + h.samples
        } else {
            h.number + 1
        };
        let mut crc = 0u16;
        let mut end = None;
        for (q, &b) in d.iter().enumerate().skip(pos) {
            if crc == 0 && q >= pos + h.len + 2 && d[q] == 0xFF {
                if let Some(n) = frame_header(&d[q..]) {
                    if n.variable == h.variable && n.number == next {
                        end = Some(q);
                        break;
                    }
                }
            }
            crc = crc16_step(crc, b);
        }
        let end = match end {
            Some(e) => e,
            None if crc == 0 && d.len() >= pos + h.len + 2 => d.len(),
            None => return Err(corrupt()),
        };
        frames.push(Frame {
            bytes: Cow::Borrowed(&d[pos..end]),
            samples: h.samples,
        });
        pos = end;
    }
    Ok(frames)
}

/// Parse a 4-byte metadata block header: (is_last, type, length).
fn block_header(h: &[u8]) -> (bool, u8, usize) {
    (
        h[0] & 0x80 != 0,
        h[0] & 0x7F,
        u32::from_be_bytes([0, h[1], h[2], h[3]]) as usize,
    )
}

fn streaminfo_body(kind: u8, body: &[u8]) -> Result<[u8; STREAMINFO_LEN]> {
    if kind != STREAMINFO || body.len() != STREAMINFO_LEN {
        return Err("the first metadata block is not a 34-byte STREAMINFO".into());
    }
    Ok(body.try_into().unwrap())
}

fn other_block(kind: u8, body: &[u8]) -> Result<Block> {
    match kind {
        STREAMINFO => Err("more than one STREAMINFO block".into()),
        127 => Err("invalid metadata block type 127".into()),
        _ => Ok(Block {
            kind,
            data: body.to_vec(),
        }),
    }
}

/// Parse a native `.flac` file. A leading ID3v2 tag is skipped (and not kept).
pub fn parse_flac(data: &[u8], frames: bool) -> Result<Stream<'_>> {
    let mut pos = 0;
    if data.len() >= 10 && &data[..3] == b"ID3" {
        let size = data[6..10]
            .iter()
            .fold(0usize, |n, &b| (n << 7) | (b & 0x7F) as usize);
        pos = 10 + size + if data[5] & 0x10 != 0 { 10 } else { 0 };
    }
    if data.get(pos..pos + 4) != Some(b"fLaC") {
        return Err("not a FLAC stream (no fLaC marker)".into());
    }
    pos += 4;
    let truncated = || "truncated metadata block".to_string();
    let mut streaminfo = None;
    let mut blocks = Vec::new();
    loop {
        let (last, kind, len) = block_header(data.get(pos..pos + 4).ok_or_else(truncated)?);
        let body = data.get(pos + 4..pos + 4 + len).ok_or_else(truncated)?;
        pos += 4 + len;
        match streaminfo {
            None => streaminfo = Some(streaminfo_body(kind, body)?),
            Some(_) => blocks.push(other_block(kind, body)?),
        }
        if last {
            break;
        }
    }
    let frames = if frames {
        split_frames(&data[pos..], pos)?
    } else {
        Vec::new()
    };
    Ok(Stream {
        streaminfo: streaminfo.unwrap(),
        blocks,
        frames,
        serial: None,
    })
}

/// Packets of the first logical bitstream, page CRCs and sequence checked.
fn ogg_packets(data: &[u8]) -> Result<(u32, Vec<Vec<u8>>)> {
    let mut serial = None;
    let mut seq = 0u32;
    let mut packets = Vec::new();
    let mut cur: Vec<u8> = Vec::new();
    let mut ended = false;
    let mut pos = 0;
    while pos < data.len() {
        let bad = |what: &str| format!("{what} Ogg page at byte {pos}");
        let head = data.get(pos..pos + 27).ok_or_else(|| bad("truncated"))?;
        if &head[..4] != b"OggS" || head[4] != 0 {
            return Err(bad("invalid"));
        }
        let flags = head[5];
        let page_serial = u32::from_le_bytes(head[14..18].try_into().unwrap());
        let page_seq = u32::from_le_bytes(head[18..22].try_into().unwrap());
        let stored = u32::from_le_bytes(head[22..26].try_into().unwrap());
        let nsegs = head[26] as usize;
        let lacing = data
            .get(pos + 27..pos + 27 + nsegs)
            .ok_or_else(|| bad("truncated"))?;
        let body_start = pos + 27 + nsegs;
        let body_end = body_start + lacing.iter().map(|&l| l as usize).sum::<usize>();
        let body = data
            .get(body_start..body_end)
            .ok_or_else(|| bad("truncated"))?;
        let mut zeroed = data[pos..body_start].to_vec();
        zeroed[22..26].fill(0);
        if crc_update(crc_update(0, &zeroed), body) != stored {
            return Err(bad("CRC mismatch in"));
        }
        let ours = match serial {
            None if flags & 2 != 0 => {
                serial = Some(page_serial);
                true
            }
            None => return Err(bad("first page is not a beginning-of-stream")),
            Some(s) if s == page_serial => {
                if ended {
                    return Err(bad("data after end-of-stream in"));
                }
                if page_seq != seq {
                    return Err(format!("Ogg page missing before byte {pos}"));
                }
                true
            }
            Some(_) if ended && flags & 2 != 0 => {
                return Err("chained Ogg streams are not supported".into());
            }
            Some(_) => false,
        };
        if ours {
            if (flags & 1 != 0) != !cur.is_empty() {
                return Err(bad("broken packet continuation in"));
            }
            seq = page_seq.wrapping_add(1);
            let mut off = 0;
            for &l in lacing {
                cur.extend_from_slice(&body[off..off + l as usize]);
                off += l as usize;
                if l < 255 {
                    packets.push(std::mem::take(&mut cur));
                }
            }
            ended = flags & 4 != 0;
        }
        pos = body_end;
    }
    if !cur.is_empty() {
        return Err("truncated Ogg packet at end of stream".into());
    }
    Ok((serial.ok_or("empty Ogg stream")?, packets))
}

/// Parse an Ogg FLAC stream (FLAC-to-Ogg mapping 1.x).
pub fn parse_ogg(data: &[u8], frames: bool) -> Result<Stream<'static>> {
    let (serial, packets) = ogg_packets(data)?;
    let mut packets = packets.into_iter();
    let bos = packets.next().unwrap_or_default();
    if bos.len() < 13 + 4 || &bos[..5] != b"\x7FFLAC" || &bos[9..13] != b"fLaC" {
        return Err("not an Ogg FLAC stream".into());
    }
    if bos[5] != 1 {
        return Err(format!("unsupported Ogg FLAC mapping version {}", bos[5]));
    }
    let (mut last, kind, len) = block_header(&bos[13..17]);
    if bos.len() != 17 + len {
        return Err("malformed Ogg FLAC header packet".into());
    }
    let streaminfo = streaminfo_body(kind, &bos[17..])?;
    let mut blocks = Vec::new();
    while !last {
        let p = packets
            .next()
            .ok_or("Ogg FLAC stream ends inside its metadata")?;
        if p.len() < 4 {
            return Err("malformed Ogg FLAC metadata packet".into());
        }
        let (l, kind, len) = block_header(&p[..4]);
        if p.len() != 4 + len {
            return Err("malformed Ogg FLAC metadata packet".into());
        }
        blocks.push(other_block(kind, &p[4..])?);
        last = l;
    }
    let mut out = Vec::new();
    if frames {
        for (i, p) in packets.enumerate() {
            let samples = whole_frame(&p)
                .ok_or_else(|| format!("corrupt FLAC frame in Ogg audio packet {i}"))?;
            out.push(Frame {
                bytes: Cow::Owned(p),
                samples,
            });
        }
    }
    Ok(Stream {
        streaminfo,
        blocks,
        frames: out,
        serial: Some(serial),
    })
}

/// Parse native or Ogg FLAC, chosen by the leading magic.
pub fn parse(data: &[u8], frames: bool) -> Result<Stream<'_>> {
    if data.starts_with(b"OggS") {
        parse_ogg(data, frames)
    } else {
        parse_flac(data, frames)
    }
}

fn total_samples(si: &[u8; STREAMINFO_LEN]) -> u64 {
    u64::from_be_bytes(si[10..18].try_into().unwrap()) & ((1 << 36) - 1)
}

/// Fill an unknown (zero) total sample count and min/max frame size from the
/// frames, and reject a STREAMINFO whose total disagrees with them.
fn complete_streaminfo(si: &mut [u8; STREAMINFO_LEN], frames: &[Frame<'_>]) -> Result<()> {
    let sum: u64 = frames.iter().map(|f| f.samples).sum();
    // The final frame may be shorter than its header's block size only by padding
    // samples, which FLAC does not have, so the sum must match exactly.
    match total_samples(si) {
        0 if sum >= 1 << 36 => return Err("stream too long for STREAMINFO".into()),
        0 => {
            let v = u64::from_be_bytes(si[10..18].try_into().unwrap()) | sum;
            si[10..18].copy_from_slice(&v.to_be_bytes());
        }
        t if t != sum => {
            return Err(format!(
                "STREAMINFO says {t} samples but the frames hold {sum}"
            ));
        }
        _ => {}
    }
    if si[4..10] == [0; 6] && !frames.is_empty() {
        let min = frames.iter().map(|f| f.bytes.len()).min().unwrap() as u32;
        let max = frames.iter().map(|f| f.bytes.len()).max().unwrap() as u32;
        if max < 1 << 24 {
            si[4..7].copy_from_slice(&min.to_be_bytes()[1..]);
            si[7..10].copy_from_slice(&max.to_be_bytes()[1..]);
        }
    }
    Ok(())
}

/// A SEEKTABLE of `n` spaced points filled from real frame offsets, exactly as
/// libFLAC's encoder fills and sorts one (duplicates become trailing placeholders).
fn seektable(n: u32, total: u64, frames: &[Frame<'_>]) -> Vec<u8> {
    if n == 0 || total == 0 {
        return Vec::new();
    }
    let mut points: Vec<(u64, u64, u64)> = (0..n as u64)
        .map(|i| (total * i / n as u64, 0, 0))
        .collect();
    let (mut first, mut sample, mut offset) = (0, 0u64, 0u64);
    for f in frames {
        while first < points.len() && points[first].0 < sample + f.samples {
            if points[first].0 >= sample {
                points[first] = (sample, offset, f.samples);
            }
            first += 1;
        }
        sample += f.samples;
        offset += f.bytes.len() as u64;
    }
    points.sort_by_key(|p| p.0);
    points.dedup_by_key(|p| p.0);
    points.resize(n as usize, (PLACEHOLDER, 0, 0));
    let mut out = Vec::with_capacity(n as usize * SEEKPOINT_LEN);
    for (s, o, k) in points {
        out.extend_from_slice(&s.to_be_bytes());
        out.extend_from_slice(&o.to_be_bytes());
        out.extend_from_slice(&(k as u16).to_be_bytes());
    }
    out
}

/// Check caller-supplied blocks (types 1..=126, at most one VORBIS_COMMENT and one
/// SEEKTABLE, each body within the 24-bit length).
pub fn check_blocks(blocks: &[Block]) -> Result<()> {
    for b in blocks {
        if b.kind == STREAMINFO || b.kind > 126 {
            return Err(format!(
                "metadata block type {} is not allowed (STREAMINFO comes from the audio)",
                b.kind
            ));
        }
        if b.data.len() > MAX_BLOCK_LEN {
            return Err(format!("metadata block of type {} is too large", b.kind));
        }
        if b.kind == SEEKTABLE && b.data.len() % SEEKPOINT_LEN != 0 {
            return Err("SEEKTABLE length is not a multiple of 18".into());
        }
    }
    for kind in [VORBIS_COMMENT, SEEKTABLE] {
        if blocks.iter().filter(|b| b.kind == kind).count() > 1 {
            return Err(format!("more than one metadata block of type {kind}"));
        }
    }
    Ok(())
}

fn vendor_comment() -> Block {
    let vendor = LIBFLAC_VENDOR_STRING.as_bytes();
    let mut data = Vec::with_capacity(8 + vendor.len());
    data.extend_from_slice(&(vendor.len() as u32).to_le_bytes());
    data.extend_from_slice(vendor);
    data.extend_from_slice(&0u32.to_le_bytes());
    Block {
        kind: VORBIS_COMMENT,
        data,
    }
}

fn push_block(out: &mut Vec<u8>, kind: u8, data: &[u8], last: bool) {
    out.push(kind | if last { 0x80 } else { 0 });
    out.extend_from_slice(&(data.len() as u32).to_be_bytes()[1..]);
    out.extend_from_slice(data);
}

/// Write `stream`'s frames into `target` with `blocks` as its metadata.
///
/// `seek` and `padding` (`Some(0)` removes all PADDING) adjust the blocks first.
/// Ogg output drops any SEEKTABLE and puts VORBIS_COMMENT first, adding a
/// vendor-only one if missing, as the Ogg FLAC mapping requires.
pub fn rebuild(
    stream: Stream<'_>,
    mut blocks: Vec<Block>,
    seek: Seek,
    padding: Option<u32>,
    target: Target,
    serial: u32,
) -> Result<Vec<u8>> {
    if target == Target::Frames {
        return Ok(stream
            .frames
            .iter()
            .flat_map(|f| f.bytes.iter().copied())
            .collect());
    }
    let mut si = stream.streaminfo;
    complete_streaminfo(&mut si, &stream.frames)?;
    if let Some(n) = padding {
        blocks.retain(|b| b.kind != PADDING);
        if n > 0 {
            blocks.push(Block {
                kind: PADDING,
                data: vec![0; n as usize],
            });
        }
    }
    if let (Seek::Spaced(n), Target::Flac) = (seek, target) {
        let at = blocks.iter().position(|b| b.kind == SEEKTABLE);
        blocks.retain(|b| b.kind != SEEKTABLE);
        let data = seektable(n, total_samples(&si), &stream.frames);
        if !data.is_empty() {
            let at = at.unwrap_or_else(|| {
                blocks
                    .iter()
                    .position(|b| b.kind == VORBIS_COMMENT)
                    .map_or(0, |i| i + 1)
            });
            blocks.insert(
                at,
                Block {
                    kind: SEEKTABLE,
                    data,
                },
            );
        }
    }
    if target == Target::Ogg {
        blocks.retain(|b| b.kind != SEEKTABLE);
        let vc = match blocks.iter().position(|b| b.kind == VORBIS_COMMENT) {
            Some(i) => blocks.remove(i),
            None => vendor_comment(),
        };
        blocks.insert(0, vc);
    }
    check_blocks(&blocks)?;

    let mut head = Vec::with_capacity(4 + STREAMINFO_LEN);
    push_block(&mut head, STREAMINFO, &si, blocks.is_empty());
    if target == Target::Flac {
        let size = 4
            + head.len()
            + blocks.iter().map(|b| 4 + b.data.len()).sum::<usize>()
            + stream.frames.iter().map(|f| f.bytes.len()).sum::<usize>();
        let mut out = Vec::with_capacity(size);
        out.extend_from_slice(b"fLaC");
        out.extend_from_slice(&head);
        for (i, b) in blocks.iter().enumerate() {
            push_block(&mut out, b.kind, &b.data, i + 1 == blocks.len());
        }
        for f in &stream.frames {
            out.extend_from_slice(&f.bytes);
        }
        return Ok(out);
    }

    // The packet layout and flush/pageout calls of libflac-rs's `encode_ogg`.
    let mut bos = Vec::with_capacity(13 + head.len());
    bos.extend_from_slice(b"\x7FFLAC\x01\x00\x00\x00fLaC");
    bos.extend_from_slice(&head);
    let mut og = OggStream::new(serial);
    og.packetin(&bos, false, 0);
    og.flush();
    for (i, b) in blocks.iter().enumerate() {
        let mut packet = Vec::with_capacity(4 + b.data.len());
        push_block(&mut packet, b.kind, &b.data, i + 1 == blocks.len());
        og.packetin(&packet, false, 0);
        og.flush();
    }
    let mut granule = 0u64;
    for (i, f) in stream.frames.iter().enumerate() {
        granule += f.samples;
        og.packetin(&f.bytes, i + 1 == stream.frames.len(), granule as i64);
        og.pageout();
    }
    Ok(og.into_bytes())
}
