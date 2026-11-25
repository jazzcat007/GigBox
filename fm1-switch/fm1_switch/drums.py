"""Drum grids: the FM-1 DRUM track's 8 lanes as a neutral pattern, Standard MIDI File drum
tracks, and TRACK_STEP frames that carry hits.

A DRUM step holds a lane mask (``hit``) and an accent mask (``acc``, only on lanes that hit).
Each lane plays one General MIDI note on the device (web/EDITOR_PROTOCOL.md, "Hits"). The
hit-only NOTE step is time 0 with no notes, and the frame's hits go in three bytes after
``vel``. Frames are for a track whose engine is DRUM; set the track's LEN separately.

Lane-to-note maps are data, not code. ``fm1`` is the FM-1's own GM notes. ``drumboy`` sends
and receives Drumboy Pro's layer i as note 36+i, which is the only fixed fact about its layers:
which instrument sits on which layer is set per kit. Pass a JSON map to match a kit.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .pattern import NSTEP, NTRK, HDR, CMD_TRACK_STEP, PatternError
from .smf import NoteEvent, read_smf, write_smf

LANES = ("kick", "snare", "clap", "hat_closed", "hat_open", "tom", "rim", "bell")
FM1_NOTES = {"kick": 36, "snare": 38, "clap": 39, "hat_closed": 42, "hat_open": 46,
             "tom": 45, "rim": 37, "bell": 56}
DRUMBOY_NOTES = {lane: 36 + i for i, lane in enumerate(LANES)}
ACCENT_VEL = 127            # the device plays an accented hit at velocity 127
DEFAULT_VEL = 96            # velocity 0 on a step means 96
DRUM_CHANNEL = 9            # MIDI channel 10, the GM drum channel
FORMAT = "fm1-drum-pattern"
VERSION = 1
DRUMBOY_MAX_SECONDS = 5.44  # one sample sector on Drumboy Pro (kSampleSize 240000 at 44.1 kHz)


class DrumError(PatternError):
    """A drum pattern, map or frame that is not valid."""


@dataclass
class DrumStep:
    hit: int = 0             # bit l: lane l strikes
    acc: int = 0             # bit l: that hit is accented (only where hit is set)
    vel: int = 0             # velocity of the unaccented hits; 0 = 96

    def check(self) -> None:
        if not 0 <= self.hit < 256 or not 0 <= self.acc < 256:
            raise DrumError("hit and acc are 8-bit lane masks")
        if self.acc & ~self.hit:
            raise DrumError("an accent needs its hit")
        if not 0 <= self.vel <= 127:
            raise DrumError(f"velocity {self.vel} out of range 0..127")


@dataclass
class DrumPattern:
    steps: list[DrumStep]
    steps_per_quarter: int = 4
    name: str = ""

    def check(self) -> None:
        if not 1 <= len(self.steps) <= NSTEP:
            raise DrumError(f"a pattern holds 1..{NSTEP} steps, has {len(self.steps)}")
        if not 1 <= self.steps_per_quarter <= 32:
            raise DrumError("steps per quarter note must be 1..32")
        for s in self.steps:
            s.check()


# ---- maps ------------------------------------------------------------------

def load_map(spec: str) -> dict[str, int]:
    """``fm1``, ``drumboy``, or a JSON file of {lane: note}. Lanes not named are not mapped."""
    if spec == "fm1":
        return dict(FM1_NOTES)
    if spec == "drumboy":
        return dict(DRUMBOY_NOTES)
    raw = json.loads(Path(spec).read_text())
    if not isinstance(raw, dict):
        raise DrumError("a map file is a JSON object of lane: note")
    m: dict[str, int] = {}
    for lane, note in raw.items():
        if lane not in LANES:
            raise DrumError(f"unknown lane {lane!r}; lanes are {', '.join(LANES)}")
        if not isinstance(note, int) or isinstance(note, bool) or not 0 <= note <= 127:
            raise DrumError(f"lane {lane}: note must be 0..127")
        m[lane] = note
    if len(set(m.values())) != len(m):
        raise DrumError("two lanes share a note")
    return m


def _inverse(m: dict[str, int]) -> dict[int, int]:
    return {note: LANES.index(lane) for lane, note in m.items()}


# ---- neutral JSON ----------------------------------------------------------

def to_json(p: DrumPattern) -> dict:
    p.check()
    return {
        "format": FORMAT,
        "version": VERSION,
        "name": p.name,
        "steps_per_quarter": p.steps_per_quarter,
        "length": len(p.steps),
        "lanes": list(LANES),
        "steps": [
            {"hits": [LANES[l] for l in range(8) if s.hit >> l & 1],
             "accents": [LANES[l] for l in range(8) if s.acc >> l & 1],
             "vel": s.vel}
            for s in p.steps
        ],
    }


def from_json(obj: dict) -> DrumPattern:
    if not isinstance(obj, dict) or obj.get("format") != FORMAT:
        raise DrumError(f"not an {FORMAT} file")
    if obj.get("version") != VERSION:
        raise DrumError(f"unsupported drum pattern version {obj.get('version')!r}")
    raw = obj.get("steps")
    if not isinstance(raw, list):
        raise DrumError("steps must be a list")
    steps = []
    for i, r in enumerate(raw):
        if not isinstance(r, dict):
            raise DrumError(f"step {i} is not an object")
        hit = acc = 0
        for name in r.get("hits", []):
            if name not in LANES:
                raise DrumError(f"step {i}: unknown lane {name!r}")
            hit |= 1 << LANES.index(name)
        for name in r.get("accents", []):
            if name not in LANES:
                raise DrumError(f"step {i}: unknown lane {name!r}")
            acc |= 1 << LANES.index(name)
        vel = r.get("vel", 0)
        if not isinstance(vel, int) or isinstance(vel, bool):
            raise DrumError(f"step {i}: vel must be an integer")
        steps.append(DrumStep(hit=hit, acc=acc, vel=vel))
    spq = obj.get("steps_per_quarter", 4)
    if not isinstance(spq, int) or isinstance(spq, bool):
        raise DrumError("steps_per_quarter must be an integer")
    p = DrumPattern(steps=steps, steps_per_quarter=spq, name=str(obj.get("name", "")))
    p.check()
    return p


# ---- Standard MIDI File ----------------------------------------------------

def from_midi(data: bytes, m: dict[str, int], *, steps_per_quarter: int = 4,
              length: int | None = None, channel: int | None = None) -> tuple[DrumPattern, list[str]]:
    """A drum track -> a DRUM pattern. Only note-ons on mapped notes count; note-offs and
    durations are ignored, since lanes are one-shots. A hit at velocity 127 is an accent."""
    ppq, events = read_smf(data)
    if channel is not None:
        events = [e for e in events if e.channel == channel]
    inv = _inverse(m)
    warnings: list[str] = []
    step_ticks = ppq / steps_per_quarter
    hits: list[tuple[int, int, int]] = []      # (step, lane, vel)
    ignored: dict[int, int] = {}
    for e in events:
        if not e.on:
            continue
        if e.note not in inv:
            ignored[e.note] = ignored.get(e.note, 0) + 1
            continue
        hits.append((round(e.tick / step_ticks), inv[e.note], e.vel))
    if ignored:
        names = ", ".join(f"{n} ×{c}" for n, c in sorted(ignored.items()))
        warnings.append(f"notes not in the map were ignored: {names}")
    bar = 4 * steps_per_quarter
    extent = max((s for s, _, _ in hits), default=0) + 1
    if length is None:
        length = max(bar, -(-extent // bar) * bar)
    if length > NSTEP:
        warnings.append(f"hits run past {NSTEP} steps; they are cut at step {NSTEP}")
        length = NSTEP
    if not 1 <= length <= NSTEP:
        raise DrumError(f"length must be 1..{NSTEP}")
    steps = [DrumStep() for _ in range(length)]
    past = 0
    for s, lane, vel in hits:
        if s >= length:
            past += 1
            continue
        st = steps[s]
        st.hit |= 1 << lane
        if vel >= ACCENT_VEL:
            st.acc |= 1 << lane
        else:
            st.vel = max(st.vel, vel)
    if past:
        warnings.append(f"{past} hit(s) past the pattern length were dropped")
    return DrumPattern(steps=steps, steps_per_quarter=steps_per_quarter), warnings


def to_midi(p: DrumPattern, m: dict[str, int], *, ppq: int = 480, channel: int = DRUM_CHANNEL) -> bytes:
    """A DRUM pattern -> a MIDI file, lanes as their mapped notes. Gates are half a step."""
    p.check()
    used = [l for l in LANES if any(s.hit >> LANES.index(l) & 1 for s in p.steps)]
    missing = [l for l in used if l not in m]
    if missing:
        raise DrumError(f"the map has no note for lane(s): {', '.join(missing)}")
    st = ppq / p.steps_per_quarter
    events: list[NoteEvent] = []
    for i, s in enumerate(p.steps):
        for lane, note in m.items():
            bit = 1 << LANES.index(lane)
            if not s.hit & bit:
                continue
            vel = ACCENT_VEL if s.acc & bit else (s.vel or DEFAULT_VEL)
            on = round(i * st)
            off = on + max(1, round(st / 2))
            events.append(NoteEvent(on, channel & 0x0F, note, vel, True))
            events.append(NoteEvent(off, channel & 0x0F, note, 0, False))
    return write_smf(events, ppq)


# ---- Felucca TRACK_STEP frames with hits -----------------------------------

def drum_step_frame(track: int, index: int, step: DrumStep) -> bytes:
    """One TRACK_STEP (cmd 30) set frame for a DRUM step. A step with hits is a NOTE step with
    no notes; an empty step is REST. Chance 100 %, ratchet x1, no nudge, no condition."""
    if not 0 <= track < NTRK:
        raise DrumError(f"track {track} out of range 0..{NTRK - 1}")
    if not 0 <= index < NSTEP:
        raise DrumError(f"step {index} out of range 0..{NSTEP - 1}")
    step.check()
    time = 0 if step.hit else 2
    body = [track, index, 0, 0, 0, 0, 0, time, 0, step.vel,
            step.hit & 127, step.acc & 127, (step.hit >> 7) | ((step.acc >> 7) << 1),
            100, 1, 8, 0]
    return HDR + bytes([CMD_TRACK_STEP]) + bytes(body) + b"\xF7"


def parse_drum_step_frame(frame: bytes) -> tuple[int, int, DrumStep]:
    """A DRUM TRACK_STEP frame -> (track, index, step). Notes in a drum frame are refused:
    a DRUM step is hits only."""
    if frame[:4] != HDR or frame[4] != CMD_TRACK_STEP or frame[-1] != 0xF7:
        raise DrumError("not a TRACK_STEP frame")
    body = frame[5:-1]
    if len(body) != 17 or any(b & 0x80 for b in body):
        raise DrumError("malformed TRACK_STEP body")
    track, index, n = body[0], body[1], body[2]
    if n:
        raise DrumError("a DRUM frame carries no notes")
    time = body[7]
    hit = (body[10] & 127) | ((body[12] & 1) << 7)
    acc = (body[11] & 127) | (((body[12] >> 1) & 1) << 7)
    if time == 2 and hit:
        raise DrumError("a REST step carries no hits")
    return track, index, DrumStep(hit=hit if time == 0 else 0, acc=acc if time == 0 else 0, vel=body[9])


def track_drum_syx(p: DrumPattern, track: int) -> bytes:
    """All 64 steps of a DRUM track as concatenated frames; steps past the pattern are REST."""
    p.check()
    steps = list(p.steps) + [DrumStep() for _ in range(NSTEP - len(p.steps))]
    return b"".join(drum_step_frame(track, i, s) for i, s in enumerate(steps))
