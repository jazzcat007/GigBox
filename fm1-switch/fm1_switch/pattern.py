"""FM-1 sequencer patterns: a neutral JSON form, Standard MIDI File conversion, and Felucca
TRACK_STEP SysEx frames.

A track has 64 steps. Each step is NOTE (up to four notes), TIE (hold the step before) or
REST. The grid here is ``steps_per_quarter`` per quarter note: 4 is 1/16 notes, the device's
default. Timing is quantised to that grid, so swing and micro-timing from a MIDI file are lost.

SysEx frames use Felucca's editor protocol (web/EDITOR_PROTOCOL.md, cmd 30 TRACK_STEP). SLOOP
numbers its commands differently and is not covered. Frames only write step content. Set the
track's LEN separately. Nothing here sends anything to a unit.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .smf import NoteEvent, read_smf, write_smf

NSTEP = 64
NTRK = 4
NOTE, TIE, REST = 0, 1, 2
TIME_LABEL = {NOTE: "note", TIE: "tie", REST: "rest"}
TIME_CODE = {v: k for k, v in TIME_LABEL.items()}
DEFAULT_VEL = 96            # the firmware's meaning of velocity 0
MAX_NOTES = 4
FLAG_ACCENT = 1
FLAG_SLIDE = 2
FORMAT = "fm1-pattern"
VERSION = 1

HDR = bytes([0xF0, 0x7D, 0x46, 0x4C])
CMD_TRACK_STEP = 30


class PatternError(ValueError):
    """A pattern or frame that is not valid."""


@dataclass
class Step:
    time: int = REST
    notes: list[int] = field(default_factory=list)
    vel: int = 0                    # 0 = firmware default (96)
    accent: bool = False
    slide: bool = False

    def check(self) -> None:
        if self.time not in (NOTE, TIE, REST):
            raise PatternError(f"bad step time {self.time}")
        if self.time == NOTE and not 1 <= len(self.notes) <= MAX_NOTES:
            raise PatternError(f"a NOTE step needs 1..{MAX_NOTES} notes, has {len(self.notes)}")
        if self.time != NOTE and self.notes:
            raise PatternError("only NOTE steps carry notes")
        for n in self.notes:
            if not 0 <= n <= 127:
                raise PatternError(f"note {n} out of range 0..127")
        if not 0 <= self.vel <= 127:
            raise PatternError(f"velocity {self.vel} out of range 0..127")


@dataclass
class Pattern:
    steps: list[Step]
    steps_per_quarter: int = 4
    name: str = ""

    def check(self) -> None:
        if not 1 <= len(self.steps) <= NSTEP:
            raise PatternError(f"a pattern holds 1..{NSTEP} steps, has {len(self.steps)}")
        if not 1 <= self.steps_per_quarter <= 32:
            raise PatternError("steps per quarter note must be 1..32")
        for s in self.steps:
            s.check()
        if self.steps and self.steps[0].time == TIE:
            raise PatternError("the first step cannot be a TIE")


# ---- neutral JSON ----------------------------------------------------------

def to_json(p: Pattern) -> dict:
    p.check()
    return {
        "format": FORMAT,
        "version": VERSION,
        "name": p.name,
        "steps_per_quarter": p.steps_per_quarter,
        "length": len(p.steps),
        "steps": [
            {"time": TIME_LABEL[s.time], "notes": list(s.notes), "vel": s.vel,
             "accent": s.accent, "slide": s.slide}
            for s in p.steps
        ],
    }


def from_json(obj: dict) -> Pattern:
    if not isinstance(obj, dict) or obj.get("format") != FORMAT:
        raise PatternError(f"not an {FORMAT} file")
    if obj.get("version") != VERSION:
        raise PatternError(f"unsupported pattern version {obj.get('version')!r}")
    raw = obj.get("steps")
    if not isinstance(raw, list):
        raise PatternError("steps must be a list")
    steps = []
    for i, r in enumerate(raw):
        if not isinstance(r, dict):
            raise PatternError(f"step {i} is not an object")
        label = r.get("time", "rest")
        if label not in TIME_CODE:
            raise PatternError(f"step {i}: time must be note, tie or rest")
        notes = r.get("notes", [])
        if not isinstance(notes, list) or not all(isinstance(n, int) and not isinstance(n, bool) for n in notes):
            raise PatternError(f"step {i}: notes must be integers")
        vel = r.get("vel", 0)
        if not isinstance(vel, int) or isinstance(vel, bool):
            raise PatternError(f"step {i}: vel must be an integer")
        steps.append(Step(time=TIME_CODE[label], notes=sorted(notes), vel=vel,
                          accent=bool(r.get("accent", False)), slide=bool(r.get("slide", False))))
    spq = obj.get("steps_per_quarter", 4)
    if not isinstance(spq, int) or isinstance(spq, bool):
        raise PatternError("steps_per_quarter must be an integer")
    p = Pattern(steps=steps, steps_per_quarter=spq, name=str(obj.get("name", "")))
    p.check()
    return p


# ---- Standard MIDI File ----------------------------------------------------

def from_midi(data: bytes, *, steps_per_quarter: int = 4, length: int | None = None,
              channel: int | None = None) -> tuple[Pattern, list[str]]:
    """A MIDI file -> a pattern on the step grid, plus warnings about what did not fit.

    Notes that start on the same step form one NOTE step, up to four of them, lowest first.
    A note that sounds past its step's end turns the following empty steps into TIE steps.
    ``length`` defaults to the notes' extent, rounded up to whole bars (four quarters) and
    capped at 64 steps.
    """
    ppq, events = read_smf(data)
    if channel is not None:
        events = [e for e in events if e.channel == channel]
    warnings: list[str] = []
    step_ticks = ppq / steps_per_quarter
    last_tick = max((e.tick for e in events), default=0)

    open_: dict[tuple[int, int], list[tuple[int, int]]] = {}
    spans: list[tuple[int, int, int, int]] = []        # (start step, end step, note, vel)
    for e in events:
        key = (e.channel, e.note)
        if e.on:
            open_.setdefault(key, []).append((e.tick, e.vel))
        else:
            q = open_.get(key)
            if q:
                t0, v0 = q.pop(0)
                spans.append(_span(t0, e.tick, step_ticks, e.note, v0))
    unterminated = 0
    for (ch, note), q in open_.items():
        for t0, v0 in q:
            unterminated += 1
            spans.append(_span(t0, last_tick, step_ticks, note, v0))
    if unterminated:
        warnings.append(f"{unterminated} note(s) had no note-off; they end at the last event")

    bar = 4 * steps_per_quarter
    extent = max((s[1] for s in spans), default=0)
    if length is None:
        length = max(bar, -(-extent // bar) * bar)
    if length > NSTEP:
        warnings.append(f"notes run past {NSTEP} steps; they are cut at step {NSTEP}")
        length = NSTEP
    if not 1 <= length <= NSTEP:
        raise PatternError(f"length must be 1..{NSTEP}")

    steps = [Step() for _ in range(length)]
    starts: dict[int, list[tuple[int, int]]] = {}
    for s0, s1, note, vel in spans:
        if s0 >= length:
            warnings.append(f"a note at step {s0} is past the pattern length and is dropped")
            continue
        starts.setdefault(s0, []).append((note, vel))

    overridden = 0
    for k, pairs in starts.items():
        pairs = sorted(set(pairs))
        if len(pairs) > MAX_NOTES:
            warnings.append(f"step {k}: {len(pairs)} notes, the device holds {MAX_NOTES}; kept the lowest {MAX_NOTES}")
            pairs = pairs[:MAX_NOTES]
        steps[k] = Step(time=NOTE, notes=sorted({n for n, _ in pairs}),
                        vel=max(v for _, v in pairs))
    for s0, s1, note, _ in spans:
        for k in range(s0 + 1, min(s1, length)):
            if k in starts:
                overridden += 1
                break
    if overridden:
        warnings.append(f"{overridden} overlapping note(s) are cut where the next note starts "
                        "(the device retriggers a NOTE step)")

    # A TIE holds the NOTE step before it, so it is only used while that step's notes
    # (the longest of them) still sound. Steps with no note and nothing held are REST.
    hold_end = 0
    for k in range(length):
        if steps[k].time == NOTE:
            hold_end = max((s1 for s0, s1, _, _ in spans if s0 == k), default=k + 1)
        elif k < hold_end:
            steps[k] = Step(time=TIE)
        else:
            steps[k] = Step(time=REST)
    return Pattern(steps=steps, steps_per_quarter=steps_per_quarter), warnings


def _span(t0: int, t1: int, step_ticks: float, note: int, vel: int) -> tuple[int, int, int, int]:
    s0 = round(t0 / step_ticks)
    s1 = max(s0 + 1, round(t1 / step_ticks))
    return s0, s1, note, vel


def to_midi(p: Pattern, ppq: int = 480) -> bytes:
    """A pattern -> a format 0 MIDI file. Each NOTE step sounds until the next non-TIE step."""
    p.check()
    st = ppq / p.steps_per_quarter
    events: list[NoteEvent] = []
    n = len(p.steps)
    i = 0
    while i < n:
        s = p.steps[i]
        if s.time != NOTE:
            i += 1
            continue
        j = i + 1
        while j < n and p.steps[j].time == TIE:
            j += 1
        vel = s.vel or DEFAULT_VEL
        for note in s.notes:
            events.append(NoteEvent(round(i * st), 0, note, vel, True))
            events.append(NoteEvent(round(j * st), 0, note, 0, False))
        i = j
    return write_smf(events, ppq)


# ---- Felucca TRACK_STEP SysEx ----------------------------------------------

def track_step_frame(track: int, index: int, step: Step) -> bytes:
    """One TRACK_STEP (cmd 30) set frame. Hits, chance, ratchet, nudge and condition are sent
    at their neutral values: no drum hits, 100 % chance, x1 ratchet, no nudge, no condition."""
    if not 0 <= track < NTRK:
        raise PatternError(f"track {track} out of range 0..{NTRK - 1}")
    if not 0 <= index < NSTEP:
        raise PatternError(f"step {index} out of range 0..{NSTEP - 1}")
    step.check()
    notes = sorted(step.notes)
    n = len(notes)
    flags = (FLAG_ACCENT if step.accent else 0) | (FLAG_SLIDE if step.slide else 0)
    body = [track, index, n, *notes, *([0] * (MAX_NOTES - n)), step.time, flags, step.vel,
            0, 0, 0,        # drum hits: none (hit & 127, accent & 127, the two high bits)
            100,            # chance, percent
            1,              # ratchet x1
            8,              # nudge 0 (the byte is nudge + 8)
            0]              # condition: none
    return HDR + bytes([CMD_TRACK_STEP]) + bytes(body) + b"\xF7"


def parse_track_step_frame(frame: bytes) -> tuple[int, int, Step]:
    """A TRACK_STEP set frame -> (track, index, step). Used to check what we write."""
    if frame[:4] != HDR or frame[4] != CMD_TRACK_STEP or frame[-1] != 0xF7:
        raise PatternError("not a TRACK_STEP frame")
    body = frame[5:-1]
    if len(body) < 10 or any(b & 0x80 for b in body):
        raise PatternError("malformed TRACK_STEP body")
    track, index, n = body[0], body[1], body[2]
    notes = [b for b in body[3:7][:n]]
    time, flags, vel = body[7], body[8], body[9]
    step = Step(time=time, notes=sorted(notes) if time == NOTE else [], vel=vel,
                accent=bool(flags & FLAG_ACCENT), slide=bool(flags & FLAG_SLIDE))
    step.check()
    return track, index, step


def track_syx(p: Pattern, track: int) -> bytes:
    """All 64 steps of a track as concatenated frames (steps past the pattern are REST)."""
    p.check()
    steps = list(p.steps) + [Step() for _ in range(NSTEP - len(p.steps))]
    return b"".join(track_step_frame(track, i, s) for i, s in enumerate(steps))
