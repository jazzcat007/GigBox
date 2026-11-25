"""``fm1-switch asset``: build and read shareable FM-1 assets on the host.

Nothing here touches a unit. Outputs are files: a sample slot image (``.fsmp``), a neutral
pattern (``.json``), a Standard MIDI File (``.mid``), or SysEx frames (``.syx``).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from . import drums as drm, fsmp, pattern as pat

NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
_PC = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}


def note_name(n: int) -> str:
    return f"{NAMES[n % 12]}{n // 12 - 1}"


def parse_note(text: str) -> int:
    """``60`` or ``C4`` / ``F#3`` / ``Bb2`` (C4 = 60, as the web editor) -> MIDI note."""
    text = text.strip()
    if re.fullmatch(r"\d+", text):
        n = int(text)
    else:
        m = re.fullmatch(r"([A-Ga-g])([#b]?)(-?\d+)", text)
        if not m:
            raise ValueError(f"not a note: {text!r}")
        pc = _PC[m.group(1).upper()] + {"#": 1, "b": -1, "": 0}[m.group(2)]
        n = (int(m.group(3)) + 1) * 12 + pc
    if not 0 <= n <= 127:
        raise ValueError(f"note {text!r} out of range 0..127")
    return n


def note_from_name(name: str, default: int = 60) -> int:
    """A note name inside a file name (``kick_C2.wav``), else the default."""
    m = re.search(r"(?<![A-Za-z])([A-G])([#b]?)(-?\d)(?!\d)", name)
    if not m:
        return default
    try:
        return parse_note(m.group(0))
    except ValueError:
        return default


def _zone_from_spec(spec: str) -> fsmp.Zone:
    """``FILE[:ROOT[:LO-HI]]`` -> a Zone. ROOT defaults to a note name in the file name."""
    parts = spec.split(":")
    path = Path(parts[0])
    root = parse_note(parts[1]) if len(parts) > 1 and parts[1] else note_from_name(path.stem)
    lo = hi = None
    if len(parts) > 2 and parts[2]:
        a, _, b = parts[2].partition("-")
        lo, hi = parse_note(a), parse_note(b)
        if lo > hi:
            raise ValueError(f"{spec}: key range {lo}-{hi} is backwards")
    src = fsmp.read_wav(path)
    samples, loop = fsmp.to_slot_samples(src)
    return fsmp.Zone(samples=samples, root=root, lo=lo, hi=hi, loop=loop)


def cmd_sample_build(args) -> int:
    zones = [_zone_from_spec(s) for s in args.files]
    image = fsmp.build_slot(args.name, zones)
    Path(args.out).write_bytes(image)
    data_len = len(image) - fsmp.DATA_OFF
    print(f"{args.out}: slot {args.name.upper()[:8]!r}, {len(zones)} zone(s), {len(image)} bytes "
          f"({data_len} bytes ADPCM, {data_len * 2 / fsmp.SLOT_RATE:.2f} s at 22.05 kHz)")
    for z in sorted(zones, key=lambda z: z.root):
        span = len(z.samples) / fsmp.SLOT_RATE
        loop = "looped" if z.loop else "one-shot"
        print(f"  root {note_name(z.root):<4} {span:6.2f} s  {loop}")
    if data_len > fsmp.SLICE_KEEP_LIMIT:
        print(f"warning: {data_len} bytes is over {fsmp.SLICE_KEEP_LIMIT}: SLICE's hand-set slices "
              "will not survive a save on the unit", file=sys.stderr)
    print("nothing was sent to a unit", file=sys.stderr)
    return 0


def cmd_sample_info(args) -> int:
    info = fsmp.parse_slot(Path(args.file).read_bytes())
    print(f"name {info.name!r}  zones {len(info.zones)}  data {len(info.data)} bytes  "
          f"CRC {'ok' if info.crc_ok else 'MISMATCH'}")
    for z in info.zones:
        loop = f"loop {z['ls']}-{z['le']}" if z["looped"] else "one-shot"
        print(f"  root {note_name(int(z['root'])):<4} keys {z['lo']}-{z['hi']}  "
              f"{z['n'] / fsmp.SLOT_RATE:6.2f} s  {loop}")
    return 0 if info.crc_ok else 1


def cmd_sample_export(args) -> int:
    rate, bits = args.rate, args.bits
    if args.drumboy:
        rate, bits = rate or 44100, 24
    image = Path(args.file).read_bytes()
    paths = fsmp.slot_to_wavs(image, args.outdir, rate=rate, bits=bits)
    for p in paths:
        src = fsmp.read_wav(p)
        secs = len(src.samples) / src.rate
        print(f"{p}  {secs:.2f} s")
        if args.drumboy and secs > drm.DRUMBOY_MAX_SECONDS:
            print(f"warning: {p.name} is over {drm.DRUMBOY_MAX_SECONDS} s, Drumboy Pro's sample length",
                  file=sys.stderr)
    return 0


def _drum_pattern_from_file(path: str) -> drm.DrumPattern:
    return drm.from_json(json.loads(Path(path).read_text()))


def cmd_drum_import(args) -> int:
    m = drm.load_map(args.map)
    p, warnings = drm.from_midi(Path(args.file).read_bytes(), m, steps_per_quarter=args.steps_per_quarter,
                                length=args.length, channel=args.channel)
    p.name = args.name or Path(args.file).stem
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    text = json.dumps(drm.to_json(p), indent=1) + "\n"
    if args.out:
        Path(args.out).write_text(text)
        hits = sum(bin(s.hit).count("1") for s in p.steps)
        print(f"{args.out}: {len(p.steps)} steps, {hits} hits")
    else:
        sys.stdout.write(text)
    return 0


def cmd_drum_export(args) -> int:
    p = _drum_pattern_from_file(args.file)
    m = drm.load_map(args.map)
    Path(args.out).write_bytes(drm.to_midi(p, m, ppq=args.ppq, channel=args.channel))
    print(f"{args.out}: {len(p.steps)} steps, map {args.map}, channel {args.channel + 1}")
    return 0


def cmd_drum_syx(args) -> int:
    p = _drum_pattern_from_file(args.file)
    Path(args.out).write_bytes(drm.track_drum_syx(p, args.track))
    print(f"{args.out}: DRUM TRACK_STEP frames for track {args.track + 1}, all {drm.NSTEP} steps")
    print("the track's engine must be DRUM and its LEN set separately; nothing was sent", file=sys.stderr)
    return 0


def cmd_drum_show(args) -> int:
    p = _drum_pattern_from_file(args.file)
    print(f"{p.name or args.file}: {len(p.steps)} steps, {p.steps_per_quarter} per quarter")
    for i, s in enumerate(p.steps):
        if not s.hit:
            continue
        lanes = [drm.LANES[l] for l in range(8) if s.hit >> l & 1]
        acc = [drm.LANES[l] for l in range(8) if s.acc >> l & 1]
        extra = f"  accent {','.join(acc)}" if acc else ""
        print(f"  {i + 1:>3} {' '.join(lanes)}{extra}")
    return 0


def _pattern_from_file(path: str) -> pat.Pattern:
    return pat.from_json(json.loads(Path(path).read_text()))


def cmd_pattern_import(args) -> int:
    data = Path(args.file).read_bytes()
    p, warnings = pat.from_midi(data, steps_per_quarter=args.steps_per_quarter,
                                length=args.length, channel=args.channel)
    p.name = args.name or Path(args.file).stem
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    text = json.dumps(pat.to_json(p), indent=1) + "\n"
    if args.out:
        Path(args.out).write_text(text)
        print(f"{args.out}: {len(p.steps)} steps, {sum(s.time == pat.NOTE for s in p.steps)} NOTE steps")
    else:
        sys.stdout.write(text)
    return 0


def cmd_pattern_export(args) -> int:
    p = _pattern_from_file(args.file)
    Path(args.out).write_bytes(pat.to_midi(p, ppq=args.ppq))
    print(f"{args.out}: {len(p.steps)} steps at {p.steps_per_quarter} per quarter, ppq {args.ppq}")
    return 0


def cmd_pattern_syx(args) -> int:
    p = _pattern_from_file(args.file)
    Path(args.out).write_bytes(pat.track_syx(p, args.track))
    print(f"{args.out}: TRACK_STEP frames for track {args.track + 1}, all {pat.NSTEP} steps")
    print("set the track's LEN to the pattern length separately; nothing was sent", file=sys.stderr)
    return 0


def cmd_pattern_show(args) -> int:
    p = _pattern_from_file(args.file)
    print(f"{p.name or args.file}: {len(p.steps)} steps, {p.steps_per_quarter} per quarter")
    for i, s in enumerate(p.steps):
        if s.time == pat.REST:
            continue
        notes = " ".join(note_name(n) for n in s.notes) if s.notes else ""
        flags = ("A" if s.accent else "") + ("S" if s.slide else "")
        print(f"  {i + 1:>3} {pat.TIME_LABEL[s.time]:<5} {notes:<12} vel {s.vel or pat.DEFAULT_VEL:<4} {flags}")
    return 0


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="fm1-switch asset", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="kind", required=True)

    s = sub.add_parser("sample", help="FSMP sample slot images")
    ss = s.add_subparsers(dest="op", required=True)
    b = ss.add_parser("build", help="WAV files -> one slot image (one zone per WAV)")
    b.add_argument("name", help="slot name, up to 8 ASCII characters")
    b.add_argument("out", help="output .fsmp file")
    b.add_argument("files", nargs="+", metavar="FILE[:ROOT[:LO-HI]]",
                   help="WAV file, optionally with its root note (C4 or 60) and key range")
    b.set_defaults(func=cmd_sample_build)
    i = ss.add_parser("info", help="read a slot image back: zones, key ranges, CRC")
    i.add_argument("file")
    i.set_defaults(func=cmd_sample_info)
    ex = ss.add_parser("export", help="slot image -> one WAV per zone (named by root note)")
    ex.add_argument("file")
    ex.add_argument("outdir")
    ex.add_argument("--rate", type=int, help="output sample rate (default: the slot's 22050)")
    ex.add_argument("--bits", type=int, choices=(16, 24), default=16)
    ex.add_argument("--drumboy", action="store_true",
                    help="Drumboy Pro's format: 44.1 kHz, 24 bit; warns past its 5.44 s")
    ex.set_defaults(func=cmd_sample_export)

    p = sub.add_parser("pattern", help="FM-1 step patterns")
    ps = p.add_subparsers(dest="op", required=True)
    imp = ps.add_parser("import", help="Standard MIDI File -> neutral pattern JSON")
    imp.add_argument("file")
    imp.add_argument("-o", "--out", help="output .json (default: stdout)")
    imp.add_argument("--steps-per-quarter", type=int, default=4, help="grid: 4 = 1/16 (default)")
    imp.add_argument("--length", type=int, help="steps, 1..64 (default: the notes' extent, whole bars)")
    imp.add_argument("--channel", type=int, help="MIDI channel 1..16 (default: all)")
    imp.add_argument("--name", help="pattern name (default: the file name)")
    imp.set_defaults(func=cmd_pattern_import)
    exp = ps.add_parser("export", help="neutral pattern JSON -> Standard MIDI File")
    exp.add_argument("file")
    exp.add_argument("-o", "--out", required=True)
    exp.add_argument("--ppq", type=int, default=480, help="ticks per quarter note (default 480)")
    exp.set_defaults(func=cmd_pattern_export)
    syx = ps.add_parser("syx", help="neutral pattern JSON -> TRACK_STEP SysEx frames (.syx)")
    syx.add_argument("file")
    syx.add_argument("--track", type=int, required=True, help="track 1..4")
    syx.add_argument("-o", "--out", required=True)
    syx.set_defaults(func=cmd_pattern_syx)
    show = ps.add_parser("show", help="print a pattern's sounding steps")
    show.add_argument("file")
    show.set_defaults(func=cmd_pattern_show)

    d = sub.add_parser("drum", help="FM-1 DRUM grids: 8 lanes, General MIDI notes")
    ds = d.add_subparsers(dest="op", required=True)
    dimp = ds.add_parser("import", help="drum MIDI file -> drum pattern JSON")
    dimp.add_argument("file")
    dimp.add_argument("-o", "--out", help="output .json (default: stdout)")
    dimp.add_argument("--map", default="fm1", help="fm1 (default), drumboy, or a map JSON file")
    dimp.add_argument("--steps-per-quarter", type=int, default=4)
    dimp.add_argument("--length", type=int)
    dimp.add_argument("--channel", type=int, help="MIDI channel 1..16 (default: all)")
    dimp.add_argument("--name")
    dimp.set_defaults(func=cmd_drum_import)
    dexp = ds.add_parser("export", help="drum pattern JSON -> drum MIDI file")
    dexp.add_argument("file")
    dexp.add_argument("-o", "--out", required=True)
    dexp.add_argument("--map", default="fm1", help="fm1 (default), drumboy, or a map JSON file")
    dexp.add_argument("--channel", type=int, default=10, help="MIDI channel 1..16 (default 10)")
    dexp.add_argument("--ppq", type=int, default=480)
    dexp.set_defaults(func=cmd_drum_export)
    dsyx = ds.add_parser("syx", help="drum pattern JSON -> DRUM TRACK_STEP frames (.syx)")
    dsyx.add_argument("file")
    dsyx.add_argument("--track", type=int, required=True, help="track 1..4")
    dsyx.add_argument("-o", "--out", required=True)
    dsyx.set_defaults(func=cmd_drum_syx)
    dshow = ds.add_parser("show", help="print a drum pattern's hits")
    dshow.add_argument("file")
    dshow.set_defaults(func=cmd_drum_show)
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = _parser()
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)
    if getattr(args, "track", None) is not None:
        if not 1 <= args.track <= pat.NTRK:
            print(f"fm1-switch asset: track must be 1..{pat.NTRK}", file=sys.stderr)
            return 2
        args.track -= 1
    if getattr(args, "channel", None) is not None:
        if not 1 <= args.channel <= 16:
            print("fm1-switch asset: channel must be 1..16", file=sys.stderr)
            return 2
        args.channel -= 1
    try:
        return args.func(args)
    except (OSError, ValueError) as e:
        print(f"fm1-switch asset: {e}", file=sys.stderr)
        return 1
