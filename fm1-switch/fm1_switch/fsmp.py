"""Felucca user sample slots (``FSMP`` v1), built and read on the host.

A slot image is what sits at a slot's flash address: a 480-byte header, padded with 0xFF
to 512, then the IMA ADPCM data. Layout (web/EDITOR_PROTOCOL.md, "User sample slot"; and
firmware/src/eng_sample.c):

  header  u32 magic 'FSMP' (0x504D5346), u16 version 1, u8 zone count, u8 0,
          8-byte name (ASCII, zero padded), u32 data length, u32 CRC-32 of the data,
          8 zero bytes, then 16 zones of 28 bytes (unused zones are zero)
  zone    u32 offset, samples, loop start, loop end (inclusive); u32 rate
          (Hz / 44100 x 65536); i16 root x 16 (MIDI note in 1/16 semitones);
          i16 ADPCM predictor at the loop start; u8 step index at the loop start,
          low key, high key, looped (0/1)
  data    IMA ADPCM, 4 bit, low nibble first, starting from predictor 0 and index 0

The byte layout is taken from the documentation. The encoder and decoder are written here
from the IMA ADPCM definition. Nothing is sent to a unit: the output is a file.
"""

from __future__ import annotations

import io
import math
import re
import struct
import wave
import zlib
from dataclasses import dataclass, field
from pathlib import Path

MAGIC = 0x504D5346          # "FSMP"
VERSION = 1
SLOT_SIZE = 0x14000         # 80 KiB per slot
DATA_OFF = 512              # ADPCM data starts here; the header area is 480 bytes
HDR_LEN = 32 + 16 * 28      # 480
MAX_ZONES = 16
MAX_DATA = SLOT_SIZE - DATA_OFF          # 81408 bytes of ADPCM (about 7.4 s at 22.05 kHz)
SLICE_KEEP_LIMIT = 77312    # above this, SLICE's hand-set slices do not survive a save
SLOT_RATE = 22050
PEAK = 30000                # zones are normalised to this peak, as the web editor does

_INDEX = (-1, -1, -1, -1, 2, 4, 6, 8)
_STEP = (
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45,
    50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143, 157, 173, 190, 209, 230,
    253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658, 724, 796, 876, 963,
    1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024, 3327,
    3660, 4026, 4428, 4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442,
    11487, 12635, 13899, 15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794,
    32767,
)


class SampleError(ValueError):
    """A sample that cannot be put in a slot, or a slot image that is not valid."""


# ---- IMA ADPCM -------------------------------------------------------------

def ima_encode(samples: list[int], loop_start: int = 0) -> tuple[bytes, tuple[int, int]]:
    """int16 samples -> (ADPCM bytes, (predictor, index) the decoder holds at ``loop_start``).

    Encoding starts from predictor 0 and index 0. The returned state lets a looped zone
    resume decoding at its loop start.
    """
    pred, idx = 0, 0
    snap = (0, 0)
    out = bytearray()
    pending = None
    for n, s in enumerate(samples):
        if n == loop_start:
            snap = (pred, idx)
        step = _STEP[idx]
        diff = s - pred
        code = 0
        if diff < 0:
            code = 8
            diff = -diff
        vp = step >> 3
        if diff >= step:
            code |= 4
            diff -= step
            vp += step
        step >>= 1
        if diff >= step:
            code |= 2
            diff -= step
            vp += step
        step >>= 1
        if diff >= step:
            code |= 1
            vp += step
        pred = pred - vp if code & 8 else pred + vp
        pred = max(-32768, min(32767, pred))
        idx = max(0, min(88, idx + _INDEX[code & 7]))
        if pending is None:
            pending = code
        else:
            out.append(pending | (code << 4))
            pending = None
    if pending is not None:
        out.append(pending)
    return bytes(out), snap


def ima_decode(data: bytes, n: int, pred: int = 0, idx: int = 0) -> list[int]:
    """ADPCM bytes -> n int16 samples, starting from the given predictor and index."""
    out = []
    for i in range(n):
        b = data[i >> 1]
        code = (b >> 4) if i & 1 else (b & 15)
        step = _STEP[idx]
        vp = step >> 3
        if code & 4:
            vp += step
        if code & 2:
            vp += step >> 1
        if code & 1:
            vp += step >> 2
        pred = pred - vp if code & 8 else pred + vp
        pred = max(-32768, min(32767, pred))
        idx = max(0, min(88, idx + _INDEX[code & 7]))
        out.append(pred)
    return out


# ---- resampling ------------------------------------------------------------

def resample(x: list[float], sr_in: int, sr_out: int, half: int = 16) -> list[float]:
    """Windowed-sinc resampling (Hann window, ``half`` taps either side at the output rate).

    Downsampling lowers the cutoff to the new Nyquist, so content above it does not alias.
    The kernel is normalised to unity DC gain.
    """
    if sr_in == sr_out:
        return list(x)
    ratio = sr_out / sr_in
    fc = min(1.0, ratio)                    # cutoff, as a fraction of the input Nyquist
    width = half / fc                       # kernel half-width in input samples
    n_out = int(len(x) * ratio)
    out = []
    last = len(x) - 1
    for i in range(n_out):
        t = i / ratio
        lo = max(0, math.ceil(t - width))
        hi = min(last, math.floor(t + width))
        acc = 0.0
        norm = 0.0
        for k in range(lo, hi + 1):
            d = t - k
            if d == 0:
                h = fc
            else:
                arg = math.pi * fc * d
                h = fc * math.sin(arg) / arg
            h *= 0.5 + 0.5 * math.cos(math.pi * d / width)
            acc += x[k] * h
            norm += h
        out.append(acc / norm if norm else 0.0)
    return out


# ---- WAV input -------------------------------------------------------------

@dataclass
class Source:
    rate: int
    samples: list[float]                       # mono, nominally -1..1
    loop: tuple[int, int] | None = None        # (start, end inclusive) in source frames


def _smpl_loop(raw: bytes) -> tuple[int, int] | None:
    """The first loop of a RIFF 'smpl' chunk, or None. End is returned inclusive."""
    if raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        return None
    p = 12
    while p + 8 <= len(raw):
        cid = raw[p:p + 4]
        size = struct.unpack_from("<I", raw, p + 4)[0]
        body = p + 8
        if cid == b"smpl" and size >= 36 + 24 and struct.unpack_from("<I", raw, body + 28)[0]:
            start, end = struct.unpack_from("<II", raw, body + 36 + 8)
            return start, end
        p = body + size + (size & 1)
    return None


def read_wav(path: str | Path) -> Source:
    """Read a PCM WAV (8, 16, 24 or 32 bit, any channel count) as mono floats.

    Float WAVs are refused: export them as 16-bit PCM first. A 'smpl' loop is used when present.
    """
    raw = Path(path).read_bytes()
    try:
        w = wave.open(io.BytesIO(raw))
    except (wave.Error, EOFError) as e:
        raise SampleError(f"{path}: not a PCM WAV ({e})") from None
    with w:
        nch, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
        frames = w.readframes(w.getnframes())
    if width not in (1, 2, 3, 4):
        raise SampleError(f"{path}: unsupported sample width {width} bytes")
    if nch < 1:
        raise SampleError(f"{path}: no channels")
    n = len(frames) // (width * nch)
    scale = float(1 << (8 * width - 1))
    mono = []
    for i in range(n):
        acc = 0.0
        base = i * width * nch
        for c in range(nch):
            o = base + c * width
            if width == 1:
                acc += (frames[o] - 128) / 128.0
            else:
                acc += int.from_bytes(frames[o:o + width], "little", signed=True) / scale
        mono.append(acc / nch)
    return Source(rate=rate, samples=mono, loop=_smpl_loop(raw))


# ---- zones and slots -------------------------------------------------------

@dataclass
class Zone:
    samples: list[int]                         # int16 at SLOT_RATE
    root: int                                  # MIDI note
    lo: int | None = None                      # key range; None: split between roots
    hi: int | None = None
    loop: tuple[int, int] | None = None        # (start, end inclusive) in samples


def key_split(roots: list[int]) -> list[tuple[int, int]]:
    """Sorted, distinct roots -> key ranges. Each zone reaches halfway to its neighbours;
    the outer zones run to the ends of the keyboard."""
    n = len(roots)
    out = []
    for j, r in enumerate(roots):
        lo = 0 if j == 0 else (roots[j - 1] + r) // 2 + 1
        hi = 127 if j == n - 1 else (r + roots[j + 1]) // 2
        out.append((lo, hi))
    return out


def to_slot_samples(src: Source, *, loop_src: tuple[int, int] | None = None) -> tuple[list[int], tuple[int, int] | None]:
    """Source audio -> int16 at SLOT_RATE, peak-normalised to PEAK, plus the loop in those samples."""
    x = resample(src.samples, src.rate, SLOT_RATE)
    if not x:
        raise SampleError("the file has no audio")
    pk = max(abs(v) for v in x)
    if pk == 0:
        raise SampleError("the file is silent")
    ints = [max(-32768, min(32767, int(round(v / pk * PEAK)))) for v in x]
    loop = None
    lp = loop_src if loop_src is not None else src.loop
    if lp is not None:
        r = SLOT_RATE / src.rate
        ls = int(round(lp[0] * r))
        le = min(len(ints) - 1, int(round(lp[1] * r)))
        if 0 <= ls < le:
            loop = (ls, le)
    return ints, loop


def build_slot(name: str, zones: list[Zone]) -> bytes:
    """Zones -> a slot image (512-byte header area, then ADPCM data). Raises SampleError."""
    if not 1 <= len(zones) <= MAX_ZONES:
        raise SampleError(f"1..{MAX_ZONES} zones per slot, got {len(zones)}")
    zones = sorted(zones, key=lambda z: z.root)
    roots = [z.root for z in zones]
    if len(set(roots)) != len(roots):
        raise SampleError("two zones share a root note")
    for z in zones:
        if not 0 <= z.root <= 127:
            raise SampleError(f"root note {z.root} out of range 0..127")
        if not z.samples:
            raise SampleError("a zone has no samples")
    data = bytearray()
    recs = []
    for z in zones:
        n = len(z.samples)
        if z.loop is None:
            ls, le, looped = 0, n - 1, 0
            adp, _ = ima_encode(z.samples, 0)
            pred, idx = 0, 0                    # no loop: the state at the start
        else:
            ls, le = z.loop
            if not (0 <= ls < le < n):
                raise SampleError(f"loop {z.loop} does not fit a zone of {n} samples")
            looped = 1
            adp, (pred, idx) = ima_encode(z.samples, ls)
        off = len(data)
        data += adp
        recs.append([off, n, ls, le, z.root, pred, idx, looped, z.lo, z.hi])
    if len(data) > MAX_DATA:
        secs = MAX_DATA * 2 / SLOT_RATE
        raise SampleError(f"too long: {len(data)} bytes of ADPCM, a slot holds {MAX_DATA} "
                          f"(about {secs:.1f} s in all)")
    split = key_split(roots)
    zb = bytearray()
    rate = int(round(SLOT_RATE / 44100 * 65536))
    for rec, (slo, shi) in zip(recs, split):
        off, n, ls, le, root, pred, idx, looped, lo, hi = rec
        lo = slo if lo is None else lo
        hi = shi if hi is None else hi
        zb += struct.pack("<5I2h4B", off, n, ls, le, rate, root * 16, pred, idx, lo, hi, looped)
    nm = re.sub(r"[^\x20-\x7E]", "", name.upper())[:8].encode("ascii").ljust(8, b"\0")
    hdr = struct.pack("<IHBB8sII8x", MAGIC, VERSION, len(zones), 0, nm, len(data), zlib.crc32(bytes(data)))
    hdr = (hdr + bytes(zb)).ljust(HDR_LEN, b"\0")
    return hdr + b"\xff" * (DATA_OFF - HDR_LEN) + bytes(data)


@dataclass
class SlotInfo:
    name: str
    crc_ok: bool
    data: bytes
    zones: list[dict] = field(default_factory=list)

    def decode_zone(self, i: int) -> list[int]:
        """The zone's samples as the firmware plays them from its start (looped zones: one pass)."""
        z = self.zones[i]
        return ima_decode(self.data[z["off"]:], z["n"], 0, 0)


def parse_slot(image: bytes) -> SlotInfo:
    """Read a slot image back, checking what the firmware checks (magic, version, zone bounds, CRC)."""
    if len(image) < DATA_OFF:
        raise SampleError("slot image is shorter than its header area")
    magic, version, nz, _rsv, nm, dlen, crc = struct.unpack_from("<IHBB8sII", image, 0)
    if magic != MAGIC:
        raise SampleError("not a sample slot (no FSMP magic)")
    if version != VERSION:
        raise SampleError(f"unsupported slot version {version}")
    if not 1 <= nz <= MAX_ZONES:
        raise SampleError(f"bad zone count {nz}")
    if dlen > MAX_DATA or DATA_OFF + dlen > len(image):
        raise SampleError("data length does not fit the image")
    data = image[DATA_OFF:DATA_OFF + dlen]
    zones = []
    for k in range(nz):
        off, n, ls, le, rate, root16, pred, idx, lo, hi, looped = struct.unpack_from(
            "<5I2h4B", image, 32 + 28 * k)
        if off > dlen or (n + 1) // 2 + off > dlen or not 0 < n or ls > le or le >= n:
            raise SampleError(f"zone {k} points outside its data")
        zones.append(dict(off=off, n=n, ls=ls, le=le, rate=rate, root=root16 / 16, pred=pred,
                          idx=idx, lo=lo, hi=hi, looped=bool(looped)))
    return SlotInfo(
        name=nm.rstrip(b"\0").decode("ascii", "replace"),
        crc_ok=zlib.crc32(data) == crc,
        data=data,
        zones=zones,
    )


# ---- export: slot -> WAV ---------------------------------------------------

_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")


def note_name(n: int) -> str:
    """MIDI note -> name, C4 = 60 (as the web editor)."""
    return f"{_NAMES[n % 12]}{n // 12 - 1}"


def write_wav(path: str | Path, samples: list[int], rate: int, bits: int = 16,
              root: int | None = None, loop: tuple[int, int] | None = None) -> None:
    """Write int16 samples as PCM WAV (16 or 24 bit, mono). A loop goes in a 'smpl' chunk,
    with start and end (inclusive) in samples; the root note goes there too."""
    if bits not in (16, 24):
        raise SampleError("bits must be 16 or 24")
    if not 8000 <= rate <= 192000:
        raise SampleError(f"sample rate {rate} out of range")
    width = bits // 8
    if bits == 16:
        frames = struct.pack("<%dh" % len(samples), *samples)
    else:
        frames = b"".join((s << 8).to_bytes(3, "little", signed=True) for s in samples)
    body = bytearray(b"WAVE")
    body += b"fmt " + struct.pack("<I", 16) + struct.pack("<HHIIHH", 1, 1, rate, rate * width, width, bits)
    body += b"data" + struct.pack("<I", len(frames)) + frames
    if len(frames) & 1:
        body += b"\0"
    if loop is not None or root is not None:
        smpl = struct.pack("<9I", 0, 0, 1_000_000_000 // rate, root if root is not None else 60, 0, 0, 0,
                           1 if loop else 0, 0)
        if loop is not None:
            smpl += struct.pack("<6I", 0, 0, loop[0], loop[1], 0, 0)
        body += b"smpl" + struct.pack("<I", len(smpl)) + smpl
        if len(smpl) & 1:
            body += b"\0"
    Path(path).write_bytes(b"RIFF" + struct.pack("<I", len(body)) + bytes(body))


def _safe_name(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_")
    return s or "SAMPLE"


def slot_to_wavs(image: bytes, outdir: str | Path, *, rate: int | None = None, bits: int = 16) -> list[Path]:
    """Every zone of a slot image -> one WAV, named ``<SLOT>_<root note>.wav``.

    The file name carries the root note, so ``read_wav`` + ``build_slot`` read it back with the
    same root. Key ranges are not stored in a WAV and are recomputed on import. ``rate`` resamples
    (the slot's own rate is 22.05 kHz); without it the slot rate is kept.
    """
    info = parse_slot(image)
    if not info.crc_ok:
        raise SampleError("the slot's data CRC does not match; not exporting it")
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    used: set[str] = set()
    for i, z in enumerate(info.zones):
        src_rate = round(z["rate"] / 65536 * 44100)
        dst_rate = rate or src_rate
        pcm = info.decode_zone(i)
        loop = (z["ls"], z["le"]) if z["looped"] else None
        if dst_rate != src_rate:
            y = resample([v / 32768 for v in pcm], src_rate, dst_rate)
            pcm = [max(-32768, min(32767, int(round(v * 32768)))) for v in y]
            if loop is not None:
                r = dst_rate / src_rate
                ls = int(round(loop[0] * r))
                le = min(len(pcm) - 1, int(round(loop[1] * r)))
                loop = (ls, le) if 0 <= ls < le else None
        root = int(z["root"])
        base = f"{_safe_name(info.name)}_{note_name(root)}"
        name, k = base, 2
        while name in used:
            name, k = f"{base}_{k}", k + 1
        used.add(name)
        p = out / f"{name}.wav"
        write_wav(p, pcm, dst_rate, bits, root=root, loop=loop)
        paths.append(p)
    return paths
