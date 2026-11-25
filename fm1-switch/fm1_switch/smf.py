"""Minimal Standard MIDI File support: note events in, note events out.

Reads format 0 and 1 with running status. Meta and SysEx events are skipped. Writes a single
format 0 track with a 120 BPM tempo event, so a DAW opens it at a sensible tempo. Only note
on/off is kept; controllers, program changes and the rest are dropped.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NoteEvent:
    tick: int
    channel: int      # 0..15
    note: int         # 0..127
    vel: int          # 1..127 for note-on; 0 for note-off
    on: bool


def _read_vlq(buf: bytes, p: int) -> tuple[int, int]:
    value = 0
    for _ in range(4):
        b = buf[p]
        p += 1
        value = (value << 7) | (b & 0x7F)
        if not b & 0x80:
            return value, p
    raise ValueError("variable-length quantity longer than four bytes")


def _vlq(value: int) -> bytes:
    out = [value & 0x7F]
    value >>= 7
    while value:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    return bytes(reversed(out))


def read_smf(data: bytes) -> tuple[int, list[NoteEvent]]:
    """Return (ticks per quarter note, note events sorted by tick; offs before ons at a tick)."""
    if data[:4] != b"MThd":
        raise ValueError("not a Standard MIDI File")
    hlen = int.from_bytes(data[4:8], "big")
    fmt = int.from_bytes(data[8:10], "big")
    ntrk = int.from_bytes(data[10:12], "big")
    division = int.from_bytes(data[12:14], "big")
    if division & 0x8000:
        raise ValueError("SMPTE time division is not supported; export with ticks per quarter note")
    if fmt not in (0, 1):
        raise ValueError(f"MIDI file format {fmt} is not supported (0 or 1)")
    if division == 0:
        raise ValueError("ticks per quarter note is zero")
    events: list[NoteEvent] = []
    p = 8 + hlen
    for _ in range(ntrk):
        if data[p:p + 4] != b"MTrk":
            raise ValueError("track chunk missing")
        tlen = int.from_bytes(data[p + 4:p + 8], "big")
        p += 8
        end = p + tlen
        tick = 0
        status = 0
        while p < end:
            delta, p = _read_vlq(data, p)
            tick += delta
            b = data[p]
            if b & 0x80:
                status = b
                p += 1
            elif status == 0:
                raise ValueError("running status without a status byte")
            if status == 0xFF:                  # meta: type, length, text
                p += 1
                mlen, p = _read_vlq(data, p)
                p += mlen
                status = 0
                continue
            if status in (0xF0, 0xF7):          # SysEx: length, bytes
                slen, p = _read_vlq(data, p)
                p += slen
                status = 0
                continue
            hi = status & 0xF0
            ch = status & 0x0F
            if hi in (0xC0, 0xD0):              # one data byte
                p += 1
                continue
            note, vel = data[p], data[p + 1]
            p += 2
            if hi == 0x90 and vel:
                events.append(NoteEvent(tick, ch, note, vel, True))
            elif hi == 0x80 or hi == 0x90:      # note-off, or note-on with velocity 0
                events.append(NoteEvent(tick, ch, note, 0, False))
        p = end
    events.sort(key=lambda e: (e.tick, e.on))
    return division, events


def write_smf(events: list[NoteEvent], ppq: int = 480, bpm: float = 120.0) -> bytes:
    """Note events -> a format 0 Standard MIDI File."""
    if not 1 <= ppq <= 0x7FFF:
        raise ValueError(f"ticks per quarter note {ppq} out of range")
    trk = bytearray()
    us = int(round(60_000_000 / bpm))
    trk.extend(_vlq(0) + b"\xff\x51\x03" + us.to_bytes(3, "big"))
    last = 0
    for e in sorted(events, key=lambda e: (e.tick, e.on)):
        if e.tick < last:
            raise ValueError("events must not go back in time")
        trk.extend(_vlq(e.tick - last))
        trk.extend(bytes([(0x90 if e.on else 0x80) | (e.channel & 0x0F), e.note, e.vel]))
        last = e.tick
    trk.extend(_vlq(0) + b"\xff\x2f\x00")
    header = b"MThd" + (6).to_bytes(4, "big") + (0).to_bytes(2, "big") + (1).to_bytes(2, "big") \
        + ppq.to_bytes(2, "big")
    return header + b"MTrk" + len(trk).to_bytes(4, "big") + bytes(trk)
