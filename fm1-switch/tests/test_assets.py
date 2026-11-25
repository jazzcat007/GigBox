"""``fm1-switch asset``: sample slots (FSMP), MIDI and pattern conversion, TRACK_STEP frames.

Everything runs on the host. No unit is touched.
"""

from __future__ import annotations

import json
import math
import struct
import wave

import pytest

from fm1_switch import assets_cli, drums as drm, fsmp, pattern as pat, smf


def _sine(freq: float, rate: int, seconds: float, amp: float) -> list[int]:
    return [int(round(amp * math.sin(2 * math.pi * freq * i / rate))) for i in range(int(rate * seconds))]


def _write_wav(path, samples: list[int], rate: int, width: int = 2, channels: int = 1) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        frames = bytearray()
        for s in samples:
            for _ in range(channels):
                if width == 1:
                    frames.append((s >> 8) + 128 & 0xFF)
                else:
                    frames += int(s << (8 * (width - 2))).to_bytes(width, "little", signed=True)
        w.writeframes(bytes(frames))


# ---- IMA ADPCM --------------------------------------------------------------

def test_step_table_is_the_standard_89_entries():
    assert len(fsmp._STEP) == 89
    assert fsmp._STEP[0] == 7 and fsmp._STEP[-1] == 32767
    assert all(a < b for a, b in zip(fsmp._STEP, fsmp._STEP[1:]))


def test_adpcm_roundtrip_error_is_small_on_a_sine():
    x = _sine(440, fsmp.SLOT_RATE, 0.2, 20000)
    data, _ = fsmp.ima_encode(x, 0)
    assert len(data) == (len(x) + 1) // 2
    y = fsmp.ima_decode(data, len(x))
    errs = [abs(a - b) for a, b in zip(x, y)]
    rms = (sum(e * e for e in errs) / len(errs)) ** 0.5
    assert rms < 800, f"rms error {rms:.0f} on a 20000-peak sine (about 33 dB SNR)"
    # ADPCM starts from predictor 0 and step 7, so the first few samples ramp up: skip them
    assert max(errs[64:]) < 3000, f"max error {max(errs[64:])} after the start-up ramp"


def test_loop_state_lets_the_decoder_resume_at_the_loop_start():
    x = _sine(220, fsmp.SLOT_RATE, 0.3, 15000)
    ls = 1234
    data, (pred, idx) = fsmp.ima_encode(x, ls)            # ls is even: a whole byte
    full = fsmp.ima_decode(data, len(x))
    resumed = fsmp.ima_decode(data[ls // 2:], len(x) - ls, pred, idx)
    assert resumed == full[ls:]


def test_encoder_clamps_and_handles_odd_lengths():
    x = [32767, -32768, 0] * 5
    data, _ = fsmp.ima_encode(x, 0)
    assert len(data) == (len(x) + 1) // 2
    y = fsmp.ima_decode(data, len(x))
    assert all(-32768 <= v <= 32767 for v in y)


# ---- resampling and WAV input ----------------------------------------------

def test_resample_keeps_length_and_dc_level():
    x = [0.5] * 2000
    y = fsmp.resample(x, 44100, 22050)
    assert abs(len(y) - 1000) <= 1
    assert all(abs(v - 0.5) < 1e-6 for v in y[20:-20])


def test_read_wav_reads_24_bit_and_averages_channels(tmp_path):
    mono = tmp_path / "mono24.wav"
    _write_wav(mono, [1000] * 50, 22050, width=3, channels=1)
    src = fsmp.read_wav(mono)
    assert src.rate == 22050 and len(src.samples) == 50
    assert abs(src.samples[0] - 1000 / 32768) < 1e-9          # 16-bit value in 24-bit space

    stereo = tmp_path / "stereo16.wav"
    _write_wav(stereo, [1000] * 50, 22050, width=2, channels=2)   # the same value on both channels
    src = fsmp.read_wav(stereo)
    assert len(src.samples) == 50
    assert abs(src.samples[0] - 1000 / 32768) < 1e-9


def test_float_wav_is_refused_with_a_clear_message(tmp_path):
    p = tmp_path / "float.wav"
    data = struct.pack("<HHIIHH", 3, 1, 44100, 44100 * 4, 4, 32) + b""
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(data)) + data + b"data" + struct.pack("<I", 0)
    p.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
    with pytest.raises(fsmp.SampleError):
        fsmp.read_wav(p)


def test_smpl_loop_is_read_and_scaled(tmp_path):
    frames = struct.pack("<" + "h" * 4410, *([1000] * 4410))
    fmt = struct.pack("<HHIIHH", 1, 1, 44100, 88200, 2, 16)
    smpl = bytearray(36 + 24)
    struct.pack_into("<I", smpl, 28, 1)                  # one loop
    struct.pack_into("<II", smpl, 36 + 8, 1000, 3000)    # start, end (inclusive)
    body = (b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
            + b"data" + struct.pack("<I", len(frames)) + frames
            + b"smpl" + struct.pack("<I", len(smpl)) + bytes(smpl))
    p = tmp_path / "loop.wav"
    p.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
    src = fsmp.read_wav(p)
    assert src.loop == (1000, 3000)
    _, loop = fsmp.to_slot_samples(src)
    assert loop == (500, 1500)


# ---- slots -----------------------------------------------------------------

def test_key_split_halfway_between_roots():
    assert fsmp.key_split([48, 60, 72]) == [(0, 54), (55, 66), (67, 127)]


def test_build_slot_and_parse_back(tmp_path):
    a = fsmp.Zone(samples=_sine(261, fsmp.SLOT_RATE, 0.1, 12000), root=60)
    b = fsmp.Zone(samples=_sine(523, fsmp.SLOT_RATE, 0.1, 12000), root=72,
                  loop=(500, 1500))
    image = fsmp.build_slot("hello sample", [b, a])
    assert len(image) > fsmp.DATA_OFF
    info = fsmp.parse_slot(image)
    assert info.name == "HELLO SA"
    assert info.crc_ok
    assert [z["root"] for z in info.zones] == [60, 72]
    assert info.zones[0]["lo"] == 0 and info.zones[0]["hi"] == 66
    assert info.zones[1]["lo"] == 67 and info.zones[1]["hi"] == 127
    assert info.zones[1]["looped"] and (info.zones[1]["ls"], info.zones[1]["le"]) == (500, 1500)
    assert not info.zones[0]["looped"]
    assert info.zones[0]["rate"] == 32768
    got = info.decode_zone(0)
    assert len(got) == len(a.samples)
    assert max(abs(x - y) for x, y in zip(got[64:], a.samples[64:])) < 3000


def test_parse_rejects_bad_magic_and_tampered_data():
    image = fsmp.build_slot("X", [fsmp.Zone(samples=_sine(440, fsmp.SLOT_RATE, 0.05, 9000), root=69)])
    with pytest.raises(fsmp.SampleError):
        fsmp.parse_slot(b"\0" * 600)
    tampered = bytearray(image)
    tampered[-1] ^= 0xFF
    assert not fsmp.parse_slot(bytes(tampered)).crc_ok


def test_slot_too_long_is_refused():
    long = fsmp.Zone(samples=[0] * (fsmp.SLOT_RATE * 8), root=60)     # 8 s: 88,200 bytes of ADPCM
    with pytest.raises(fsmp.SampleError, match="too long"):
        fsmp.build_slot("LONG", [long])


def test_zone_rules_are_enforced():
    z = fsmp.Zone(samples=[1, 2, 3, 4], root=60)
    with pytest.raises(fsmp.SampleError, match="share a root"):
        fsmp.build_slot("D", [z, fsmp.Zone(samples=[1, 2], root=60)])
    with pytest.raises(fsmp.SampleError, match="loop"):
        fsmp.build_slot("L", [fsmp.Zone(samples=[1, 2, 3, 4], root=60, loop=(2, 9))])
    with pytest.raises(fsmp.SampleError):
        fsmp.build_slot("N", [fsmp.Zone(samples=[1], root=60)] * 17)


# ---- Standard MIDI File ----------------------------------------------------

def test_read_smf_handles_running_status_and_meta():
    track = (
        b"\x00\x90\x3c\x64"          # note on 60, vel 100
        + b"\x60\x3c\x00"            # running status: note on 60 vel 0 = note off, at tick 96
        + b"\x00\xff\x51\x03\x07\xa1\x20"   # tempo meta, skipped
        + b"\x00\x80\x40\x40"        # note off 64 at tick 96
        + b"\x00\xff\x2f\x00"
    )
    data = b"MThd" + (6).to_bytes(4, "big") + (0).to_bytes(2, "big") + (1).to_bytes(2, "big") \
        + (96).to_bytes(2, "big") + b"MTrk" + len(track).to_bytes(4, "big") + track
    ppq, events = smf.read_smf(data)
    assert ppq == 96
    assert [(e.tick, e.note, e.on) for e in events] == [(0, 60, True), (96, 60, False), (96, 64, False)]


def test_smf_write_then_read_is_identity():
    ev = [smf.NoteEvent(0, 0, 60, 100, True), smf.NoteEvent(480, 0, 60, 0, False),
          smf.NoteEvent(480, 0, 62, 90, True), smf.NoteEvent(960, 0, 62, 0, False)]
    ppq, back = smf.read_smf(smf.write_smf(ev, 480))
    assert ppq == 480
    assert back == ev


def test_smpte_division_is_refused():
    data = b"MThd" + (6).to_bytes(4, "big") + (0).to_bytes(2, "big") + (1).to_bytes(2, "big") \
        + (0xE728).to_bytes(2, "big")
    with pytest.raises(ValueError, match="SMPTE"):
        smf.read_smf(data)


# ---- patterns --------------------------------------------------------------

def _sample_pattern() -> pat.Pattern:
    steps = [pat.Step(time=pat.NOTE, notes=[60], vel=100), pat.Step(time=pat.TIE),
             pat.Step(time=pat.TIE), pat.Step(time=pat.REST),
             pat.Step(time=pat.NOTE, notes=[62, 65], vel=90, accent=True),
             pat.Step(time=pat.REST), pat.Step(time=pat.NOTE, notes=[67], vel=110),
             pat.Step(time=pat.REST)] + [pat.Step() for _ in range(8)]
    return pat.Pattern(steps=steps, steps_per_quarter=4, name="demo")


def test_pattern_midi_roundtrip_keeps_steps_and_ties():
    p = _sample_pattern()
    q, warnings = pat.from_midi(pat.to_midi(p, ppq=480), steps_per_quarter=4, length=16)
    assert warnings == []
    assert [(s.time, s.notes, s.vel) for s in q.steps] == [(s.time, s.notes, s.vel) for s in p.steps]


def test_from_midi_quantises_to_nearest_step():
    # ppq 480, 4 steps per quarter: one step is 120 ticks. Note at 100 ticks -> step 1.
    ev = [smf.NoteEvent(100, 0, 60, 100, True), smf.NoteEvent(220, 0, 60, 0, False)]
    p, _ = pat.from_midi(smf.write_smf(ev, 480), steps_per_quarter=4)
    assert p.steps[1].time == pat.NOTE and p.steps[1].notes == [60]
    assert p.steps[0].time == pat.REST and len(p.steps) == 16


def test_from_midi_chord_limit_and_warning():
    ev = [smf.NoteEvent(0, 0, n, 100, True) for n in (60, 62, 64, 65, 67)]
    ev += [smf.NoteEvent(240, 0, n, 0, False) for n in (60, 62, 64, 65, 67)]
    p, warnings = pat.from_midi(smf.write_smf(ev, 480), steps_per_quarter=4)
    assert p.steps[0].notes == [60, 62, 64, 65]
    assert any("5 notes" in w for w in warnings)


def test_from_midi_default_length_rounds_up_to_bars_and_caps_at_64():
    ev = [smf.NoteEvent(40 * 120, 0, 60, 100, True), smf.NoteEvent(41 * 120, 0, 60, 0, False)]
    p, _ = pat.from_midi(smf.write_smf(ev, 480), steps_per_quarter=4)
    assert len(p.steps) == 48
    far = [smf.NoteEvent(200 * 120, 0, 60, 100, True), smf.NoteEvent(201 * 120, 0, 60, 0, False)]
    q, warnings = pat.from_midi(smf.write_smf(far, 480), steps_per_quarter=4)
    assert len(q.steps) == 64 and any("past" in w for w in warnings)


def test_from_midi_channel_filter():
    ev = [smf.NoteEvent(0, 0, 60, 100, True), smf.NoteEvent(120, 0, 60, 0, False),
          smf.NoteEvent(0, 9, 36, 100, True), smf.NoteEvent(120, 9, 36, 0, False)]
    p, _ = pat.from_midi(smf.write_smf(ev, 480), steps_per_quarter=4, channel=9)
    assert p.steps[0].notes == [36]


def test_json_roundtrip_and_validation():
    p = _sample_pattern()
    obj = json.loads(json.dumps(pat.to_json(p)))
    assert pat.from_json(obj).steps[4].notes == [62, 65]
    bad = pat.to_json(p)
    bad["steps"][0]["time"] = "hold"
    with pytest.raises(pat.PatternError):
        pat.from_json(bad)
    too_many = pat.to_json(p)
    too_many["steps"][4]["notes"] = [60, 61, 62, 63, 64]
    with pytest.raises(pat.PatternError):
        pat.from_json(too_many)
    with pytest.raises(pat.PatternError):
        pat.from_json({"format": "fm1-pattern", "version": 1, "steps": [{}] * 65})


def test_track_step_frame_layout_and_roundtrip():
    s = pat.Step(time=pat.NOTE, notes=[64, 60], vel=100, accent=True)
    frame = pat.track_step_frame(2, 9, s)
    assert frame[:5] == bytes([0xF0, 0x7D, 0x46, 0x4C, 30])
    assert frame[-1] == 0xF7
    assert len(frame) == 23
    assert all(b < 0x80 for b in frame[1:-1])
    track, index, back = pat.parse_track_step_frame(frame)
    assert (track, index) == (2, 9)
    assert back.time == pat.NOTE and back.notes == [60, 64] and back.vel == 100 and back.accent


def test_track_syx_covers_all_steps_and_pads_with_rest():
    p = _sample_pattern()
    syx = pat.track_syx(p, 0)
    assert len(syx) == 64 * 23
    frames = [syx[i * 23:(i + 1) * 23] for i in range(64)]
    assert pat.parse_track_step_frame(frames[63])[2].time == pat.REST
    assert pat.parse_track_step_frame(frames[4])[2].notes == [62, 65]


def test_frame_bounds():
    with pytest.raises(pat.PatternError):
        pat.track_step_frame(4, 0, pat.Step())
    with pytest.raises(pat.PatternError):
        pat.track_step_frame(0, 64, pat.Step())


# ---- command line ----------------------------------------------------------

def test_cli_sample_build_info_and_bad_input(tmp_path, capsys):
    wav = tmp_path / "kick_C2.wav"
    _write_wav(wav, _sine(55, 44100, 0.15, 12000), 44100)
    out = tmp_path / "kick.fsmp"
    assert assets_cli.main(["sample", "build", "KICK", str(out), f"{wav}"]) == 0
    assert assets_cli.main(["sample", "info", str(out)]) == 0
    text = capsys.readouterr().out
    assert "root C2" in text and "CRC ok" in text
    assert assets_cli.main(["sample", "info", str(tmp_path / "missing.fsmp")]) == 1


def test_cli_explicit_root_and_key_range(tmp_path, capsys):
    wav = tmp_path / "pad.wav"
    _write_wav(wav, _sine(220, 22050, 0.1, 9000), 22050)
    out = tmp_path / "pad.fsmp"
    assert assets_cli.main(["sample", "build", "PAD", str(out), f"{wav}:A3:45-69"]) == 0
    info = fsmp.parse_slot(out.read_bytes())
    assert info.zones[0]["root"] == 57 and (info.zones[0]["lo"], info.zones[0]["hi"]) == (45, 69)


def test_cli_pattern_pipeline(tmp_path, capsys):
    j = tmp_path / "p.json"
    mid = tmp_path / "p.mid"
    syx = tmp_path / "p.syx"
    back = tmp_path / "back.json"
    j.write_text(json.dumps(pat.to_json(_sample_pattern())))
    assert assets_cli.main(["pattern", "export", str(j), "-o", str(mid)]) == 0
    assert assets_cli.main(["pattern", "import", str(mid), "-o", str(back), "--length", "16"]) == 0
    assert assets_cli.main(["pattern", "syx", str(j), "--track", "1", "-o", str(syx)]) == 0
    assert len(syx.read_bytes()) == 64 * 23
    assert assets_cli.main(["pattern", "show", str(back)]) == 0
    shown = capsys.readouterr().out
    assert "C4" in shown and "D4" in shown


def test_cli_track_range_is_checked(tmp_path):
    j = tmp_path / "p.json"
    j.write_text(json.dumps(pat.to_json(_sample_pattern())))
    assert assets_cli.main(["pattern", "syx", str(j), "--track", "5", "-o", str(tmp_path / "x.syx")]) == 2


def test_note_parsing():
    assert assets_cli.parse_note("C4") == 60
    assert assets_cli.parse_note("A3") == 57
    assert assets_cli.parse_note("Bb2") == 46
    assert assets_cli.parse_note("48") == 48
    with pytest.raises(ValueError):
        assets_cli.parse_note("H9")
    assert assets_cli.note_from_name("kick_F#1.wav") == 30


def test_asset_is_dispatched_from_the_main_cli(tmp_path, capsys):
    from fm1_switch import __main__ as cli

    j = tmp_path / "p.json"
    j.write_text(json.dumps(pat.to_json(_sample_pattern())))
    assert cli.main(["asset", "pattern", "show", str(j)]) == 0
    assert "C4" in capsys.readouterr().out


# ---- slot -> WAV export ----------------------------------------------------

def test_slot_export_wav_roundtrip_keeps_root_and_loop(tmp_path):
    x = _sine(55, fsmp.SLOT_RATE, 0.2, 12000)
    img = fsmp.build_slot("KICK", [fsmp.Zone(samples=x, root=36, loop=(100, 3000))])
    paths = fsmp.slot_to_wavs(img, tmp_path)
    assert [p.name for p in paths] == ["KICK_C2.wav"]
    src = fsmp.read_wav(paths[0])
    assert src.rate == fsmp.SLOT_RATE and src.loop == (100, 3000)
    assert len(src.samples) == len(x)
    # the exported WAV keeps the root: importing it again gives the same zone root
    samples, loop = fsmp.to_slot_samples(src)
    back = fsmp.parse_slot(fsmp.build_slot("KICK", [fsmp.Zone(samples=samples, root=36, loop=loop)]))
    assert back.zones[0]["root"] == 36 and back.zones[0]["looped"]


def test_slot_export_drumboy_format_is_44k1_24bit_and_loop_scales(tmp_path):
    x = _sine(55, fsmp.SLOT_RATE, 0.1, 12000)
    img = fsmp.build_slot("KICK", [fsmp.Zone(samples=x, root=36, loop=(100, 1000))])
    (p,) = fsmp.slot_to_wavs(img, tmp_path, rate=44100, bits=24)
    with wave.open(str(p)) as w:
        assert w.getframerate() == 44100 and w.getsampwidth() == 3 and w.getnchannels() == 1
    src = fsmp.read_wav(p)
    assert src.loop == (200, 2000)
    assert abs(len(src.samples) - 2 * len(x)) <= 2


def test_slot_export_refuses_a_bad_crc(tmp_path):
    img = bytearray(fsmp.build_slot("X", [fsmp.Zone(samples=_sine(440, fsmp.SLOT_RATE, 0.05, 9000), root=69)]))
    img[-1] ^= 0xFF
    with pytest.raises(fsmp.SampleError, match="CRC"):
        fsmp.slot_to_wavs(bytes(img), tmp_path)


def test_write_wav_refuses_odd_depths(tmp_path):
    with pytest.raises(fsmp.SampleError):
        fsmp.write_wav(tmp_path / "a.wav", [0, 1], 22050, bits=8)


# ---- drum grids ------------------------------------------------------------

def _drum_pattern() -> drm.DrumPattern:
    steps = [drm.DrumStep(hit=0b1, acc=0b1), drm.DrumStep(), drm.DrumStep(hit=0b100, vel=90),
             drm.DrumStep(hit=0b10000000 | 0b1, acc=0b10000000)] + [drm.DrumStep() for _ in range(12)]
    return drm.DrumPattern(steps=steps, steps_per_quarter=4, name="beat")


def test_drum_lane_notes_are_the_device_gm_notes():
    assert drm.FM1_NOTES == {"kick": 36, "snare": 38, "clap": 39, "hat_closed": 42,
                             "hat_open": 46, "tom": 45, "rim": 37, "bell": 56}
    assert drm.DRUMBOY_NOTES["kick"] == 36 and drm.DRUMBOY_NOTES["bell"] == 43


def test_drum_midi_roundtrip_and_accents():
    p = _drum_pattern()
    m = drm.load_map("fm1")
    q, warnings = drm.from_midi(drm.to_midi(p, m), m, length=16)
    assert warnings == []
    assert [(s.hit, s.acc) for s in q.steps] == [(s.hit, s.acc) for s in p.steps]
    assert q.steps[2].vel == 90


def test_drum_import_warns_about_unmapped_notes_and_drops_them():
    ev = [smf.NoteEvent(0, 9, 36, 100, True), smf.NoteEvent(0, 9, 40, 100, True),
          smf.NoteEvent(60, 9, 40, 0, False)]
    p, warnings = drm.from_midi(smf.write_smf(ev, 480), drm.load_map("fm1"), length=16)
    assert p.steps[0].hit == 0b1
    assert any("40" in w for w in warnings)


def test_drum_map_file_and_validation(tmp_path):
    f = tmp_path / "map.json"
    f.write_text(json.dumps({"kick": 36, "snare": 37}))
    assert drm.load_map(str(f)) == {"kick": 36, "snare": 37}
    f.write_text(json.dumps({"kick": 36, "snare": 36}))
    with pytest.raises(drm.DrumError, match="share"):
        drm.load_map(str(f))
    f.write_text(json.dumps({"cowbell": 56}))
    with pytest.raises(drm.DrumError, match="unknown lane"):
        drm.load_map(str(f))


def test_drum_export_needs_a_note_for_every_used_lane():
    p = _drum_pattern()
    with pytest.raises(drm.DrumError, match="no note"):
        drm.to_midi(p, {"kick": 36})


def test_drum_json_roundtrip_and_validation():
    p = _drum_pattern()
    obj = json.loads(json.dumps(drm.to_json(p)))
    back = drm.from_json(obj)
    assert [(s.hit, s.acc, s.vel) for s in back.steps] == [(s.hit, s.acc, s.vel) for s in p.steps]
    bad = drm.to_json(p)
    bad["steps"][0]["hits"] = ["cowbell"]
    with pytest.raises(drm.DrumError):
        drm.from_json(bad)
    bad = drm.to_json(p)
    bad["steps"][0]["accents"] = ["snare"]
    with pytest.raises(drm.DrumError, match="needs its hit"):
        drm.from_json(bad)


def test_drum_frame_carries_hits_with_their_high_bits():
    step = drm.DrumStep(hit=0b10000001, acc=0b10000000, vel=100)
    frame = drm.drum_step_frame(1, 5, step)
    assert len(frame) == 23 and all(b < 0x80 for b in frame[1:-1])
    track, index, back = drm.parse_drum_step_frame(frame)
    assert (track, index) == (1, 5)
    assert (back.hit, back.acc, back.vel) == (0b10000001, 0b10000000, 100)


def test_drum_rest_frame_and_syx_length():
    frame = drm.drum_step_frame(0, 0, drm.DrumStep())
    assert frame[5 + 7] == 2                       # time REST
    _, _, back = drm.parse_drum_step_frame(frame)
    assert back.hit == 0
    syx = drm.track_drum_syx(_drum_pattern(), 3)
    assert len(syx) == 64 * 23


def test_drum_cli_pipeline_and_sample_export(tmp_path, capsys):
    j = tmp_path / "beat.json"
    mid = tmp_path / "beat.mid"
    syx = tmp_path / "beat.syx"
    back = tmp_path / "back.json"
    j.write_text(json.dumps(drm.to_json(_drum_pattern())))
    assert assets_cli.main(["drum", "export", str(j), "--map", "drumboy", "-o", str(mid)]) == 0
    assert assets_cli.main(["drum", "import", str(mid), "--map", "drumboy", "-o", str(back)]) == 0
    assert assets_cli.main(["drum", "syx", str(j), "--track", "2", "-o", str(syx)]) == 0
    assert len(syx.read_bytes()) == 64 * 23
    assert assets_cli.main(["drum", "show", str(back)]) == 0
    assert "kick" in capsys.readouterr().out
    wav = tmp_path / "k.wav"
    _write_wav(wav, _sine(55, 44100, 0.1, 12000), 44100)
    img = tmp_path / "k.fsmp"
    assert assets_cli.main(["sample", "build", "KICK", str(img), f"{wav}:C2"]) == 0
    out = tmp_path / "out"
    assert assets_cli.main(["sample", "export", str(img), str(out), "--drumboy"]) == 0
    assert (out / "KICK_C2.wav").exists()
