"""Test suite for midikit (standard library unittest, Python 3.8+).

Run:  python tests/test_midikit.py -v
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from midikit import (  # noqa: E402
    ChannelEvent,
    Division,
    EventSelector,
    MetaEvent,
    MidiFile,
    MidiTrack,
    NoteSelector,
    SysExEvent,
    SystemEvent,
    TempoMap,
    apply_plan,
    extract_notes,
    from_document,
    to_document,
    validate,
)
from midikit.bytestream import ByteReader, ByteWriter, bytes_from_hex, hex_bytes, vlq_bytes  # noqa: E402
from midikit.constants import note_name, note_number  # noqa: E402
from midikit.errors import ParseError, SchemaError  # noqa: E402
from midikit.events import DecodeOptions, event_from_dict  # noqa: E402
from midikit.plan import normalize_plan  # noqa: E402
from midikit.selectors import parse_selector_arg, resolve_track_indices  # noqa: E402

try:
    import mido  # type: ignore

    HAVE_MIDO = True
except Exception:  # pragma: no cover
    HAVE_MIDO = False


def build_demo(format: int = 1, ppq: int = 480, tempo_bpm: float = 120.0) -> MidiFile:
    midi = MidiFile(format=format, division=ppq)
    conductor = midi.new_track("Conductor")
    conductor.add(MetaEvent.make_tempo(0, bpm=tempo_bpm))
    conductor.add(MetaEvent.make_time_signature(0, 4, 4))
    piano = midi.new_track("Piano")
    piano.add(ChannelEvent("program_change", 0, 0, tick=0))
    for index, pitch in enumerate((60, 62, 64, 65)):
        piano.add(ChannelEvent("note_on", 0, pitch, 100, tick=index * ppq))
        piano.add(ChannelEvent("note_off", 0, pitch, 64, tick=index * ppq + ppq - 10))
    bass = midi.new_track("Bass")
    bass.add(ChannelEvent("program_change", 1, 33, tick=0))
    bass.add(ChannelEvent("note_on", 1, 36, 80, tick=0))
    bass.add(ChannelEvent("note_off", 1, 36, 64, tick=ppq * 2))
    if format == 0:
        merged = MidiTrack()
        for track in midi.tracks:
            merged.events.extend(ev.clone() for ev in track.events)
        merged.sort_events()
        midi.tracks = [merged]
    return midi


class TestVLQ(unittest.TestCase):
    def test_values(self):
        cases = {0: b"\x00", 1: b"\x01", 127: b"\x7f", 128: b"\x81\x00",
                 8192: b"\xc0\x00", 0x0FFFFFFF: b"\xff\xff\xff\x7f"}
        for value, encoded in cases.items():
            self.assertEqual(vlq_bytes(value), encoded)
            reader = ByteReader(encoded)
            self.assertEqual(reader.read_vlq(), value)
            self.assertTrue(reader.eof)

    def test_negative_and_overflow(self):
        with self.assertRaises(ValueError):
            vlq_bytes(-1)
        with self.assertRaises(ValueError):
            vlq_bytes(0x10000000)

    def test_truncated(self):
        with self.assertRaises(ParseError):
            ByteReader(b"\x81").read_vlq()

    def test_overlong_strict(self):
        with self.assertRaises(ParseError):
            ByteReader(b"\x81\x81\x81\x81\x00").read_vlq(strict=True)
        self.assertEqual(ByteReader(b"\x81\x81\x81\x81\x00").read_vlq(strict=False), 270549120)

    def test_writer_roundtrip(self):
        writer = ByteWriter()
        for value in (0, 5, 127, 128, 16383, 16384, 0x0FFFFFFF):
            writer.vlq(value)
        reader = ByteReader(writer.getvalue())
        for value in (0, 5, 127, 128, 16383, 16384, 0x0FFFFFFF):
            self.assertEqual(reader.read_vlq(), value)

    def test_hex_helpers(self):
        self.assertEqual(hex_bytes(b"\xf0\x41"), "f0 41")
        self.assertEqual(bytes_from_hex("f0 41"), b"\xf0\x41")
        self.assertEqual(bytes_from_hex("[240, 65]"), b"\xf0\x41")
        self.assertEqual(bytes_from_hex("f041"), b"\xf0\x41")
        self.assertEqual(bytes_from_hex("0xf0,0x41"), b"\xf0\x41")


class TestEvents(unittest.TestCase):
    def test_channel_status_bytes(self):
        cases = [
            (ChannelEvent("note_on", 0, 60, 100), 0x90, b"\x90\x3c\x64"),
            (ChannelEvent("note_off", 15, 60, 0), 0x8F, b"\x8f\x3c\x00"),
            (ChannelEvent("poly_aftertouch", 3, 60, 20), 0xA3, b"\xa3\x3c\x14"),
            (ChannelEvent("control_change", 9, 7, 100), 0xB9, b"\xb9\x07\x64"),
            (ChannelEvent("program_change", 2, 40), 0xC2, b"\xc2\x28"),
            (ChannelEvent("channel_aftertouch", 1, 55), 0xD1, b"\xd1\x37"),
        ]
        for event, status, payload in cases:
            self.assertEqual(event.status, status)
            self.assertEqual(event.to_bytes()[0], payload)

    def test_pitch_bend(self):
        event = ChannelEvent("pitch_bend", 0, 0, 0)
        event.value = 8192
        self.assertEqual(event.data_bytes, b"\x00\x40")
        self.assertEqual(event.bend, 0)
        event.bend = -8192
        self.assertEqual(event.value, 0)
        event.bend = 8191
        self.assertEqual(event.value, 16383)
        centred = ChannelEvent("pitch_bend", 0, 0, 0)
        centred.value = 8192
        self.assertAlmostEqual(centred.semitones(), 0.0)
        centred.bend = 8191
        self.assertAlmostEqual(centred.semitones(), 2.0, places=3)

    def test_running_status_bytes(self):
        payload, status = ChannelEvent("note_on", 0, 60, 100).to_bytes(None)
        self.assertEqual(payload, b"\x90\x3c\x64")
        payload, status = ChannelEvent("note_on", 0, 62, 100).to_bytes(0x90)
        self.assertEqual(payload, b"\x3e\x64")
        payload, status = ChannelEvent("note_on", 1, 62, 100).to_bytes(0x90)
        self.assertEqual(payload, b"\x91\x3e\x64")

    def test_meta_json_roundtrip(self):
        events = [
            MetaEvent.make_tempo(0, bpm=133.7),
            MetaEvent.make_time_signature(0, 6, 8),
            MetaEvent.make_key_signature(0, key="F#m"),
            MetaEvent.make_track_name(0, "Strings"),
            MetaEvent.make_lyric(0, "hello"),
            MetaEvent.make_end_of_track(0),
            MetaEvent(0x7F, b"\x01\x02\x03"),
            MetaEvent(0x45, b"\xaa\xbb"),
        ]
        for event in events:
            rebuilt = event_from_dict(event.to_dict())
            self.assertEqual(rebuilt.fingerprint(), event.fingerprint(), event.describe())

    def test_channel_json_roundtrip(self):
        events = [
            ChannelEvent("note_on", 3, 60, 100, 480),
            ChannelEvent("control_change", 0, 7, 90, 10),
            ChannelEvent("program_change", 5, 42, None, 7),
            ChannelEvent("channel_aftertouch", 2, 33, None, 9),
        ]
        for event in events:
            rebuilt = event_from_dict(event.to_dict())
            self.assertEqual(rebuilt.fingerprint(), event.fingerprint())

    def test_sysex_and_system(self):
        sysex = SysExEvent(bytes_from_hex("f0 41 10 42 12 40 00 7f 00 41 f7"), tick=12)
        self.assertEqual(sysex.status, 0xF0)
        self.assertEqual(event_from_dict(sysex.to_dict()).fingerprint(), sysex.fingerprint())
        escape = SysExEvent(b"\x01\x02", escape=True, tick=3)
        self.assertEqual(escape.status, 0xF7)
        self.assertEqual(event_from_dict(escape.to_dict()).fingerprint(), escape.fingerprint())
        system = SystemEvent(0xF8, b"", tick=1)
        self.assertEqual(event_from_dict(system.to_dict()).fingerprint(), system.fingerprint())

    def test_event_aliases(self):
        self.assertEqual(event_from_dict({"tick": 0, "type": "note", "pitch": "C4", "vel": 90}).type, "note_on")
        self.assertEqual(event_from_dict({"tick": 0, "type": "cc", "control_name": "pan", "value": 64}).value1, 10)
        self.assertEqual(event_from_dict({"tick": 0, "type": "program", "program_name": "Flute"}).value1, 73)
        self.assertEqual(event_from_dict({"tick": 0, "type": "meta", "meta_type": "tempo", "bpm": 60}).tempo, 1000000)

    def test_validation_errors(self):
        with self.assertRaises(SchemaError):
            ChannelEvent("note_on", 16, 60, 100)
        with self.assertRaises(SchemaError):
            ChannelEvent("note_on", 0, 200, 100)
        with self.assertRaises(SchemaError):
            event_from_dict({"type": "bogus"})

    def test_note_names(self):
        self.assertEqual(note_number("C4"), 60)
        self.assertEqual(note_number("c#4"), 61)
        self.assertEqual(note_number("Db4"), 61)
        self.assertEqual(note_number("C"), 0)
        self.assertEqual(note_number(60), 60)
        self.assertEqual(note_name(60), "C4")
        self.assertEqual(note_name(0), "C-1")


class TestDivision(unittest.TestCase):
    def test_ppq(self):
        division = Division(ppq=480)
        self.assertFalse(division.is_smpte)
        self.assertEqual(division.to_int(), 480)
        self.assertEqual(Division.from_int(480).ppq, 480)

    def test_smpte(self):
        for fps, code in ((24.0, -24), (25.0, -25), (29.97, -29), (30.0, -30)):
            division = Division(smpte_fps=fps, smpte_ticks_per_frame=80)
            raw = division.to_int()
            self.assertEqual((raw >> 8) - 256, code)
            back = Division.from_int(raw)
            self.assertAlmostEqual(back.smpte_fps, fps, places=2)
            self.assertEqual(back.smpte_ticks_per_frame, 80)

    def test_invalid(self):
        with self.assertRaises(ParseError):
            Division.from_int(0x8000)
        with self.assertRaises(SchemaError):
            Division(ppq=0)
        with self.assertRaises(SchemaError):
            Division(smpte_fps=27.0)


class TestReadWrite(unittest.TestCase):
    def test_roundtrip_all_formats(self):
        for format_ in (0, 1, 2):
            midi = build_demo(format=format_)
            data = midi.to_bytes()
            back = MidiFile.read_bytes(data)
            self.assertEqual(back.format, format_)
            self.assertEqual(
                [e.fingerprint() for _t, _i, e in back.iter_events()],
                [e.fingerprint() for _t, _i, e in midi.iter_events()],
            )
            self.assertEqual(back.to_bytes(), data)
            self.assertEqual(back.warnings, [])

    def test_running_status_roundtrip(self):
        midi = build_demo()
        plain = midi.to_bytes()
        with_running = MidiFile.read_bytes(midi.to_bytes(running_status=True))
        self.assertEqual(with_running.to_bytes(), plain)
        self.assertLessEqual(len(midi.to_bytes(running_status=True)), len(plain))

    def test_note_off_as_note_on_zero(self):
        midi = build_demo()
        data = midi.to_bytes(note_off_as_note_on_0=True)
        back = MidiFile.read_bytes(data)
        self.assertTrue(all(e.type != "note_off" for _t, _i, e in back.iter_events() if e.is_channel))
        self.assertEqual(len(extract_notes(back).notes), len(extract_notes(midi).notes))

    def test_smpte_file_roundtrip(self):
        midi = MidiFile(format=0, division=Division(smpte_fps=25, smpte_ticks_per_frame=40))
        track = midi.new_track("S")
        track.add(ChannelEvent("note_on", 0, 60, 100, tick=0))
        track.add(ChannelEvent("note_off", 0, 60, 64, tick=400))
        back = MidiFile.read_bytes(midi.to_bytes())
        self.assertTrue(back.division.is_smpte)
        self.assertEqual(back.division.ticks_per_second, 1000.0)

    def test_unknown_chunk_preserved(self):
        midi = build_demo()
        midi.extra_chunks.append((b"XFIH", b"hello"))
        data = midi.to_bytes()
        back = MidiFile.read_bytes(data)
        self.assertEqual(back.extra_chunks, [(b"XFIH", b"hello")])
        self.assertEqual(back.to_bytes(), data)
        self.assertTrue(any(w["code"] == "W_UNKNOWN_CHUNK" for w in back.warnings))

    def test_track_count_mismatch_warning(self):
        midi = build_demo()
        data = bytearray(midi.to_bytes())
        data[10] = 9  # pretend the header declares 9 tracks
        back = MidiFile.read_bytes(bytes(data))
        self.assertTrue(any(w["code"] == "W_TRACK_COUNT" for w in back.warnings))
        self.assertFalse(validate(back)["ok"])

    def test_missing_end_of_track_is_repaired_on_write(self):
        midi = MidiFile(format=0, division=480)
        track = midi.new_track()
        track.add(ChannelEvent("note_on", 0, 60, 100, tick=0))
        data = midi.to_bytes()
        back = MidiFile.read_bytes(data)
        self.assertEqual(back.tracks[0].events[-1].type_byte, 0x2F)
        self.assertEqual(back.tracks[0].events[-1].tick, 0)

    def test_events_after_eot_are_preserved_and_flagged(self):
        midi = MidiFile(format=0, division=480)
        track = midi.new_track()
        track.add(MetaEvent.make_end_of_track(0))
        track.add(ChannelEvent("note_on", 0, 60, 100, tick=480))
        raw = midi.to_bytes(normalize_eot=False)
        # to_bytes normalises by default; rebuild the malformed stream by hand
        payload = track.to_bytes()
        header = b"MThd" + bytes([0, 0, 0, 6]) + bytes([0, 0, 0, 1, 1, 0xE0])
        raw = header + b"MTrk" + len(payload).to_bytes(4, "big") + payload
        back = MidiFile.read_bytes(raw)
        self.assertTrue(any(w["code"] == "W_EVENTS_AFTER_EOT" for w in back.warnings))

    def test_parse_errors(self):
        with self.assertRaises(ParseError):
            MidiFile.read_bytes(b"short")
        with self.assertRaises(ParseError):
            MidiFile.read_bytes(b"XXXX" + bytes(20))
        with self.assertRaises(ParseError):
            MidiFile.read_bytes(b"MThd" + bytes([0, 0, 0, 2]) + bytes(10))

    def test_riff_wrapper(self):
        midi = build_demo()
        inner = midi.to_bytes()
        riff = b"RIFF" + (4 + len(inner)).to_bytes(4, "little") + b"RMID" + b"data" + len(inner).to_bytes(4, "little") + inner
        back = MidiFile.read_bytes(riff)
        self.assertEqual(len(back.tracks), len(midi.tracks))
        self.assertTrue(any(w["code"] == "W_RIFF_WRAPPER" for w in back.warnings))

    def test_unknown_meta_preserved(self):
        midi = MidiFile(format=0, division=480)
        track = midi.new_track()
        track.add(MetaEvent(0x45, b"\x01\x02", tick=0))
        back = MidiFile.read_bytes(midi.to_bytes())
        unknown = [e for e in back.tracks[0].events if e.is_meta and e.type_byte == 0x45]
        self.assertEqual(len(unknown), 1)
        self.assertEqual(unknown[0].data, b"\x01\x02")

    def test_file_write_is_atomic_and_creates_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "nested", "out.mid")
            build_demo().write(target)
            self.assertTrue(os.path.isfile(target))
            self.assertEqual(len(MidiFile.read(target).tracks), 3)
            self.assertEqual([f for f in os.listdir(os.path.dirname(target))], ["out.mid"])


class TestNotes(unittest.TestCase):
    def test_extraction_and_pairing(self):
        midi = build_demo()
        extraction = extract_notes(midi)
        self.assertEqual(len(extraction.notes), 5)
        self.assertEqual(extraction.unterminated_note_ons, [])
        self.assertEqual(extraction.orphan_note_offs, [])
        self.assertEqual([n.pitch for n in extraction.sorted()][:4], [60, 62, 64, 65])

    def test_note_on_zero_velocity_is_note_off(self):
        midi = MidiFile(format=0, division=480)
        track = midi.new_track()
        track.add(ChannelEvent("note_on", 0, 60, 100, tick=0))
        track.add(ChannelEvent("note_on", 0, 60, 0, tick=240))
        notes = extract_notes(midi).notes
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].end, 240)

    def test_overlapping_same_pitch_lifo_fifo(self):
        midi = MidiFile(format=0, division=480)
        track = midi.new_track()
        for tick in (0, 100, 200, 300):
            track.add(ChannelEvent("note_on", 0, 60, 100, tick=tick))
        for tick in (400, 500, 600, 700):
            track.add(ChannelEvent("note_off", 0, 60, 64, tick=tick))
        lifo = sorted((n.start, n.end) for n in extract_notes(midi, pairing="lifo").notes)
        fifo = sorted((n.start, n.end) for n in extract_notes(midi, pairing="fifo").notes)
        self.assertEqual(lifo, [(0, 700), (100, 600), (200, 500), (300, 400)])
        self.assertEqual(fifo, [(0, 400), (100, 500), (200, 600), (300, 700)])

    def test_unterminated_and_orphan(self):
        midi = MidiFile(format=0, division=480)
        track = midi.new_track()
        track.add(ChannelEvent("note_off", 0, 64, 64, tick=0))
        track.add(ChannelEvent("note_on", 0, 60, 100, tick=10))
        track.add(ChannelEvent("note_on", 0, 60, 100, tick=480))
        extraction = extract_notes(midi)
        self.assertEqual(len(extraction.orphan_note_offs), 1)
        self.assertEqual(len(extraction.unterminated_note_ons), 2)
        self.assertEqual(len(extraction.notes), 2)
        self.assertTrue(all(n.unterminated for n in extraction.notes))
        open_only = extract_notes(midi, close_unterminated=False)
        self.assertEqual(len(open_only.notes), 0)

    def test_max_polyphony(self):
        midi = build_demo()
        notes = extract_notes(midi).notes
        from midikit.notes import max_polyphony

        self.assertGreaterEqual(max_polyphony(notes), 1)

    def test_note_selector(self):
        midi = build_demo()
        notes = extract_notes(midi).notes
        selector = NoteSelector({"pitch": "C4"})
        self.assertEqual(len([n for n in notes if selector.matches(n)]), 1)
        selector = NoteSelector({"note_min": 60, "note_max": 64})
        self.assertEqual(len([n for n in notes if selector.matches(n)]), 3)
        with self.assertRaises(SchemaError):
            NoteSelector({"bogus": 1})


class TestTempoMap(unittest.TestCase):
    def test_constant_tempo(self):
        midi = build_demo(tempo_bpm=120.0)
        tm = TempoMap.from_midi(midi)
        self.assertAlmostEqual(tm.tick_to_seconds(480), 0.5, places=6)
        self.assertAlmostEqual(tm.tick_to_seconds(960), 1.0, places=6)
        self.assertEqual(tm.seconds_to_tick(1.0), 960)
        self.assertAlmostEqual(tm.initial_bpm, 120.0, places=6)

    def test_tempo_change(self):
        midi = build_demo(tempo_bpm=120.0)
        midi.tracks[0].add(MetaEvent.make_tempo(960, bpm=60.0))
        tm = TempoMap.from_midi(midi)
        self.assertAlmostEqual(tm.tick_to_seconds(960), 1.0, places=6)
        self.assertAlmostEqual(tm.tick_to_seconds(1920), 3.0, places=6)
        self.assertEqual(tm.seconds_to_tick(3.0), 1920)
        self.assertAlmostEqual(tm.bpm_at(1000), 60.0, places=6)
        self.assertAlmostEqual(tm.bpm_at(500), 120.0, places=6)

    def test_bars_and_beats(self):
        midi = build_demo()
        tm = TempoMap.from_midi(midi)
        self.assertEqual(tm.bar_beat_tick(0)["bar"], 1)
        self.assertEqual(tm.bar_beat_tick(0)["beat"], 1)
        self.assertEqual(tm.bar_beat_tick(480)["beat"], 2)
        self.assertEqual(tm.bar_beat_tick(1920)["bar"], 2)
        self.assertEqual(tm.bar_start_tick(3), 3840)

    def test_time_signature_change(self):
        midi = build_demo()
        midi.tracks[0].add(MetaEvent.make_time_signature(1920, 3, 4))
        tm = TempoMap.from_midi(midi)
        self.assertEqual(tm.bar_beat_tick(1920)["bar"], 2)
        self.assertEqual(tm.time_signature_at(2000), (3, 4))
        self.assertEqual(tm.bar_beat_tick(1920 + 1440)["bar"], 3)

    def test_smpte_seconds(self):
        midi = MidiFile(format=0, division=Division(smpte_fps=25, smpte_ticks_per_frame=40))
        midi.new_track("S")
        tm = TempoMap.from_midi(midi)
        self.assertAlmostEqual(tm.tick_to_seconds(1000), 1.0, places=6)
        self.assertEqual(tm.seconds_to_tick(1.0), 1000)

    def test_parse_time_units(self):
        from midikit.tempo import parse_time

        tm = TempoMap.from_midi(build_demo())
        self.assertEqual(parse_time(480, tm), 480)
        self.assertEqual(parse_time("2b", tm), 960)
        self.assertEqual(parse_time("1bar", tm), 1920)
        self.assertEqual(parse_time("500ms", tm), 480)
        self.assertEqual(parse_time("1:2:0", tm), 480)
        with self.assertRaises(ValueError):
            parse_time("2bar", None)


class TestSelectors(unittest.TestCase):
    def setUp(self):
        self.midi = build_demo()

    def test_track_filtering(self):
        matches = EventSelector({"type": "note_on"}).filter(self.midi)
        self.assertEqual(len(matches), 5)
        matches = EventSelector({"type": "note_on", "track": 1}).filter(self.midi)
        self.assertEqual(len(matches), 4)

    def test_field_filters(self):
        self.assertEqual(len(EventSelector({"channel": 1}).filter(self.midi)), 3)
        self.assertEqual(len(EventSelector({"tick_min": 480, "tick_max": 960}).filter(self.midi)), 4)
        self.assertEqual(len(EventSelector({"meta_type": "set_tempo"}).filter(self.midi)), 1)
        self.assertEqual(len(EventSelector({"type": "cc"}).filter(self.midi)), 0)
        # the note group covers both note_on and note_off
        self.assertEqual(len(EventSelector({"type": "note", "note_min": 64}).filter(self.midi)), 4)
        self.assertEqual(len(EventSelector({"type": "note_on", "note_min": 64}).filter(self.midi)), 2)

    def test_limits_and_which(self):
        selector = EventSelector({"type": "note_on", "limit": 2})
        self.assertEqual(len(selector.filter(self.midi)), 2)
        selector = EventSelector({"type": "note_on", "which": "first"})
        self.assertEqual(len(selector.filter(self.midi)), 1)

    def test_unknown_field(self):
        with self.assertRaises(SchemaError):
            EventSelector({"nope": 1})

    def test_selector_shorthand(self):
        self.assertEqual(parse_selector_arg("track=1,pitch=C4"), {"track": 1, "pitch": "C4"})
        self.assertEqual(parse_selector_arg("{track:1}"), {"track": 1})
        self.assertEqual(parse_selector_arg('{"track": 2}'), {"track": 2})
        self.assertEqual(parse_selector_arg("note_on"), {"type": "note_on"})

    def test_resolve_track_indices(self):
        self.assertEqual(resolve_track_indices(self.midi, None), [0, 1, 2])
        self.assertEqual(resolve_track_indices(self.midi, [-1]), [2])
        with self.assertRaises(SchemaError):
            resolve_track_indices(self.midi, [7])


class TestOps(unittest.TestCase):
    def test_insert_and_delete_notes(self):
        midi = MidiFile(format=1, division=480)
        midi.new_track("T")
        apply_plan(midi, [{"op": "insert_notes", "track": 0, "unit": "beats",
                           "notes": [{"pitch": "C4", "start": 0, "duration": 1},
                                     {"pitch": "D4", "start": 1, "duration": 1},
                                     {"pitch": "E4", "start": 2, "duration": 1}]}])
        self.assertEqual(len(extract_notes(midi).notes), 3)
        apply_plan(midi, [{"op": "delete_notes", "match": {"pitch": "D4"}}])
        self.assertEqual([n.pitch for n in extract_notes(midi).sorted()], [60, 64])

    def test_transpose_chromatic_and_diatonic(self):
        midi = build_demo()
        apply_plan(midi, [{"op": "transpose", "semitones": 12, "match": {"track": 1}}])
        self.assertEqual([n.pitch for n in extract_notes(midi).notes if n.track == 1], [72, 74, 76, 77])
        midi2 = build_demo()
        apply_plan(midi2, [{"op": "transpose", "degrees": 2, "scale": "major", "root": "C",
                            "match": {"track": 1}}])
        self.assertEqual([n.pitch for n in extract_notes(midi2).notes if n.track == 1], [64, 65, 67, 69])

    def test_transpose_drop_mode(self):
        midi = build_demo()
        result = apply_plan(midi, [{"op": "transpose", "semitones": 60, "out_of_range": "drop"}])
        self.assertTrue(result["ok"])
        self.assertTrue(all(n.pitch <= 127 for n in extract_notes(midi).notes))

    def test_quantize_strength_and_swing(self):
        midi = MidiFile(format=1, division=480)
        midi.new_track("T")
        apply_plan(midi, [{"op": "insert_notes", "track": 0,
                           "notes": [{"pitch": 60, "start": 111, "duration": 100}]}])
        apply_plan(midi, [{"op": "quantize", "grid": "1/16", "strength": 1.0}])
        self.assertEqual(extract_notes(midi).notes[0].start, 120)
        # a note exactly between slots rounds forward
        apply_plan(midi, [{"op": "quantize", "grid": "1/8", "strength": 1.0}])
        self.assertEqual(extract_notes(midi).notes[0].start, 240)
        midi2 = MidiFile(format=1, division=480)
        midi2.new_track("T")
        apply_plan(midi2, [{"op": "insert_notes", "track": 0,
                            "notes": [{"pitch": 60, "start": 111, "duration": 100}]}])
        apply_plan(midi2, [{"op": "quantize", "grid": "1/8", "strength": 0.5}])
        self.assertEqual(extract_notes(midi2).notes[0].start, 56)

    def test_humanize_is_deterministic_with_seed(self):
        def humanized():
            midi = build_demo()
            apply_plan(midi, [{"op": "humanize", "timing": 12, "velocity": 9, "seed": 5}])
            return [(n.start, n.velocity) for n in extract_notes(midi).sorted()]

        self.assertEqual(humanized(), humanized())

    def test_set_velocity_modes(self):
        midi = build_demo()
        apply_plan(midi, [{"op": "set_velocity", "mode": "set", "value": 64}])
        self.assertTrue(all(n.velocity == 64 for n in extract_notes(midi).notes))
        apply_plan(midi, [{"op": "set_velocity", "mode": "scale", "value": 0.5}])
        self.assertTrue(all(n.velocity == 32 for n in extract_notes(midi).notes))
        apply_plan(midi, [{"op": "set_velocity", "mode": "offset", "value": 100}])
        self.assertTrue(all(n.velocity == 127 for n in extract_notes(midi).notes))

    def test_set_note_length(self):
        midi = MidiFile(format=1, division=480)
        midi.new_track("T")
        apply_plan(midi, [{"op": "insert_notes", "track": 0,
                           "notes": [{"pitch": 60, "start": 0, "duration": 100},
                                     {"pitch": 62, "start": 200, "duration": 100}]}])
        apply_plan(midi, [{"op": "set_note_length", "mode": "legato"}])
        notes = extract_notes(midi).sorted()
        self.assertEqual(notes[0].end, 200)
        apply_plan(midi, [{"op": "set_note_length", "mode": "set", "value": 50}])
        self.assertTrue(all(n.duration == 50 for n in extract_notes(midi).notes))

    def test_crop(self):
        midi = build_demo()
        apply_plan(midi, [{"op": "crop", "start": 480, "end": 1440, "shift": True}])
        notes = extract_notes(midi).sorted()
        self.assertTrue(all(0 <= n.start < 960 for n in notes))
        self.assertEqual(min(n.start for n in notes), 0)

    def test_copy_region_and_strum(self):
        midi = build_demo()
        result = apply_plan(midi, [{"op": "copy_region", "start": 0, "end": 1920, "to": 1920,
                                    "tracks": [1], "repeat": 2}])
        self.assertTrue(result["ok"])
        self.assertEqual(len([n for n in extract_notes(midi).notes if n.track == 1]), 12)

    def test_tempo_ops(self):
        midi = build_demo()
        apply_plan(midi, [{"op": "set_tempo", "bpm": 90, "replace_all": True}])
        tm = TempoMap.from_midi(midi)
        self.assertAlmostEqual(tm.initial_bpm, 90.0, places=3)
        apply_plan(midi, [{"op": "tempo_ramp", "bpm_start": 100, "bpm_end": 140, "start": 0,
                           "end": 1920, "steps": 5}])
        tempos = [e.tempo for _t, _i, e in midi.iter_events() if e.is_meta and e.type_byte == 0x51]
        self.assertEqual(len(tempos), 5)
        self.assertAlmostEqual(60000000.0 / tempos[0], 100.0, places=3)
        self.assertAlmostEqual(60000000.0 / tempos[-1], 140.0, places=3)

    def test_meta_ops(self):
        midi = build_demo()
        apply_plan(midi, [
            {"op": "set_time_signature", "numerator": 7, "denominator": 8, "at": 960},
            {"op": "set_key_signature", "key": "Bb"},
            {"op": "add_text", "meta_type": "lyric", "text": "la", "at": 0},
            {"op": "add_text", "meta_type": "marker", "text": "Chorus", "at": "1bar"},
        ])
        report = validate(midi, normalize=True)
        self.assertTrue(report["ok"], report["issues"][:3])
        markers = [e.text for _t, _i, e in midi.iter_events() if e.is_meta and e.type_byte == 6]
        self.assertEqual(markers, ["Chorus"])
        apply_plan(midi, [{"op": "strip_meta", "meta_types": ["lyric", "marker"]}])
        remaining = [e.name for _t, _i, e in midi.iter_events() if e.is_meta]
        self.assertNotIn("lyric", remaining)
        self.assertIn("set_tempo", remaining)

    def test_channel_and_instrument(self):
        midi = build_demo()
        result = apply_plan(midi, [{"op": "set_channel", "from_channel": 0, "channel": 5}])
        self.assertEqual(result["ops"][0]["changed"], 9)
        result = apply_plan(midi, [{"op": "set_instrument", "program": "Cello", "channel": 5, "track": 1}])
        self.assertEqual(result["ops"][0]["program"], 42)

    def test_track_structure_ops(self):
        midi = build_demo()
        apply_plan(midi, [{"op": "clone_track", "track": 1, "name": "Piano 2", "tick_offset": 1920}])
        self.assertEqual(len(midi.tracks), 4)
        self.assertEqual(midi.tracks[3].name, "Piano 2")
        apply_plan(midi, [{"op": "merge_tracks", "tracks": [1, 3]}])
        self.assertEqual(len(midi.tracks), 3)
        apply_plan(midi, [{"op": "delete_track", "track": [2]}])
        self.assertEqual(len(midi.tracks), 2)
        apply_plan(midi, [{"op": "move_track", "track": 1, "to": 0}])
        self.assertEqual(midi.tracks[0].name, "Merged")

    def test_split_by_channel(self):
        midi = MidiFile(format=0, division=480)
        track = midi.new_track("All")
        for channel in (0, 1):
            track.add(ChannelEvent("note_on", channel, 60 + channel, 100, tick=0))
            track.add(ChannelEvent("note_off", channel, 60 + channel, 64, tick=480))
        result = apply_plan(midi, [{"op": "split_track_by_channel", "track": 0}])
        self.assertEqual(result["ops"][0]["splits"], 2)
        self.assertEqual(len(midi.tracks), 2)

    def test_format_and_division(self):
        midi = build_demo()
        apply_plan(midi, [{"op": "set_format", "format": 0}])
        self.assertEqual(midi.format, 0)
        self.assertEqual(len(midi.tracks), 1)
        midi2 = build_demo()
        apply_plan(midi2, [{"op": "set_division", "ppq": 960, "rescale": True}])
        self.assertEqual(midi2.division.ppq, 960)
        self.assertEqual(midi2.tracks[1].events[1].tick, 0)
        self.assertEqual(max(e.tick for e in midi2.tracks[1].events), 1910 * 2)

    def test_repair(self):
        midi = MidiFile(format=0, division=480)
        track = midi.new_track()
        track.add(ChannelEvent("note_on", 0, 60, 100, tick=480))
        track.add(ChannelEvent("note_off", 0, 62, 64, tick=0))
        track.add(ChannelEvent("note_on", 0, 64, 100, tick=100))
        track.add(ChannelEvent("note_on", 0, 64, 100, tick=100))
        result = apply_plan(midi, [{"op": "repair", "dedupe": True}])
        self.assertTrue(result["ok"])
        report = result["ops"][0]
        self.assertEqual(report["orphan_note_off_removed"], 1)
        self.assertEqual(report["deduped_events"], 1)
        self.assertGreaterEqual(report["note_off_added"], 1)
        self.assertTrue(validate(midi, normalize=True)["ok"])

    def test_op_error_reporting(self):
        midi = build_demo()
        result = apply_plan(midi, [{"op": "transpose", "semitones": 1},
                                   {"op": "delete_notes", "match": {"pitch": "G9"}}])
        self.assertTrue(result["ok"])
        result = apply_plan(midi, [{"op": "delete_notes", "match": {"pitch": "G9"}, "require_match": True}])
        self.assertFalse(result["ok"])
        self.assertEqual(result["failed"]["error"]["code"], "E_OP")
        result = apply_plan(midi, [{"op": "no_such_op"}])
        self.assertFalse(result["ok"])
        self.assertEqual(result["failed"]["error"]["code"], "E_SCHEMA")
        # unknown params and unknown ops are reported, not raised, so a plan never half-applies silently
        result = apply_plan(midi, [{"op": "transpose", "bogus": 1}])
        self.assertFalse(result["ok"])
        self.assertEqual(result["failed"]["error"]["code"], "E_SCHEMA")
        from midikit.plan import describe_plan

        self.assertEqual(describe_plan([{"op": "transpose", "bogus": 1}])[0]["unknown_params"], ["bogus"])

    def test_plan_normalisation(self):
        self.assertEqual(normalize_plan({"ops": [{"op": "sort_events"}]}), [{"op": "sort_events"}])
        self.assertEqual(normalize_plan([{"op": "sort_events"}]), [{"op": "sort_events"}])
        self.assertEqual(normalize_plan([{"op": "transpose", "params": {"semitones": 2}}]),
                         [{"op": "transpose", "semitones": 2}])
        with self.assertRaises(SchemaError):
            normalize_plan({"nope": 1})

    def test_selector_precision(self):
        midi = build_demo()
        apply_plan(midi, [{"op": "delete_events",
                           "match": {"track": 1, "type": "note_on", "note_min": 64}}])
        self.assertEqual(len([e for _t, _i, e in midi.iter_events() if e.is_channel and e.is_note_on and e.tick > 1000]), 0)


class TestJsonIO(unittest.TestCase):
    def test_event_mode_roundtrip_is_byte_exact(self):
        midi = build_demo()
        doc = to_document(midi, mode="events")
        rebuilt = from_document(json.loads(json.dumps(doc)))
        self.assertEqual(rebuilt.to_bytes(), midi.to_bytes())

    def test_notes_mode(self):
        doc = {
            "format": 1,
            "division": {"type": "ppq", "ticks_per_beat": 480},
            "tracks": [{"name": "Lead", "notes": [
                {"pitch": "C4", "start": 0, "duration": 480, "velocity": 90},
                {"pitch": "E4", "start": 480, "duration": 480},
            ]}],
        }
        midi = from_document(doc)
        notes = extract_notes(midi).sorted()
        self.assertEqual([n.pitch for n in notes], [60, 64])
        self.assertEqual(midi.tracks[0].name, "Lead")

    def test_mode_both_does_not_duplicate(self):
        midi = build_demo()
        doc = to_document(midi, mode="both")
        self.assertIn("events", doc["tracks"][0])
        self.assertIn("notes_view", doc["tracks"][0])
        rebuilt = from_document(json.loads(json.dumps(doc)))
        self.assertEqual(rebuilt.to_bytes(), midi.to_bytes())

    def test_summary_included(self):
        midi = build_demo()
        doc = to_document(midi, include_summary=True)
        self.assertEqual(doc["summary"]["track_count"], 3)
        self.assertEqual(doc["summary"]["notes"]["count"], 5)


class TestValidate(unittest.TestCase):
    def test_clean_file(self):
        report = validate(build_demo(), normalize=True)
        self.assertTrue(report["ok"])
        self.assertEqual(report["counts"]["error"], 0)

    def test_format0_with_two_tracks(self):
        midi = build_demo()
        midi.format = 0
        report = validate(midi, normalize=True)
        self.assertFalse(report["ok"])
        self.assertTrue(any(i["code"] == "V_FORMAT0_TRACK_COUNT" for i in report["issues"]))

    def test_bad_meta_length(self):
        midi = MidiFile(format=0, division=480)
        track = midi.new_track()
        track.add(MetaEvent(0x51, b"\x01\x02", tick=0))
        report = validate(midi)
        self.assertTrue(any(i["code"] == "V_META_LENGTH" for i in report["issues"]))

    def test_note_warnings(self):
        midi = MidiFile(format=0, division=480)
        track = midi.new_track()
        track.add(ChannelEvent("note_on", 0, 60, 100, tick=0))
        track.add(ChannelEvent("note_off", 0, 62, 64, tick=10))
        report = validate(midi)
        codes = [i["code"] for i in report["issues"]]
        self.assertIn("V_UNTERMINATED_NOTE", codes)
        self.assertIn("V_ORPHAN_NOTE_OFF", codes)

    def test_key_signature_range(self):
        midi = MidiFile(format=0, division=480)
        track = midi.new_track()
        track.add(MetaEvent(0x59, bytes([20, 0]), tick=0))
        report = validate(midi)
        self.assertTrue(any(i["code"] == "V_KEYSIG_RANGE" for i in report["issues"]))


class TestAnalysis(unittest.TestCase):
    def test_summary(self):
        from midikit.analysis import summarize

        data = summarize(build_demo(ppq=480, tempo_bpm=120.0))
        self.assertEqual(data["track_count"], 3)
        self.assertEqual(data["notes"]["count"], 5)
        self.assertAlmostEqual(data["duration_seconds"], 1910 / 480.0 * 0.5, places=3)
        self.assertEqual(data["time_signatures"][0]["numerator"], 4)
        self.assertEqual(data["tracks"][1]["name"], "Piano")

    def test_piano_roll(self):
        from midikit.analysis import piano_roll

        text = piano_roll(build_demo(), width=40, height=8)
        self.assertIn("C60", text)          # a row label for the C4 in the demo
        self.assertIn("#", text)
        self.assertIn("pitches", text)
        self.assertEqual(len(text.splitlines()), 8 + 2)
        # a narrow height still shows the busiest pitches, not an empty window
        narrow = piano_roll(build_demo(), width=40, height=4)
        self.assertIn("#", narrow)
        full = piano_roll(build_demo(), width=40, height=40)
        self.assertNotIn("use --height", full)

    def test_diff(self):
        from midikit.analysis import diff_files

        a = build_demo()
        b = build_demo()
        self.assertTrue(diff_files(a, b)["identical_bytes"])
        apply_plan(b, [{"op": "transpose", "semitones": 1}])
        data = diff_files(a, b)
        self.assertFalse(data["identical_bytes"])
        self.assertEqual(data["notes"]["removed_count"], 5)
        self.assertEqual(data["notes"]["added_count"], 5)

    def test_event_rows(self):
        from midikit.analysis import event_rows

        rows = event_rows(build_demo(), limit=3)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["id"], "0/0")
        self.assertIn("bar", rows[0])


class TestCli(unittest.TestCase):
    """The CLI is exercised in-process so no subprocess is needed."""

    def run_cli(self, argv):
        from midikit.cli import main

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(argv)
        text = buffer.getvalue()
        try:
            payload = json.loads(text)
        except ValueError:
            payload = {"raw": text}
        return code, payload

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "demo.mid")
        build_demo().write(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_info_and_tracks(self):
        code, payload = self.run_cli(["info", self.path])
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["track_count"], 3)
        code, payload = self.run_cli(["tracks", self.path, "--compact"])
        self.assertEqual(len(payload["data"]["tracks"]), 3)

    def test_validate_exit_code(self):
        code, payload = self.run_cli(["validate", self.path])
        self.assertEqual(code, 0)
        self.assertTrue(payload["data"]["ok"])

    def test_dump(self):
        code, payload = self.run_cli(["dump", self.path, "--type", "note_on", "--compact"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["data"]["returned"], 5)
        self.assertEqual(payload["data"]["events"][0]["type"], "note_on")

    def test_create_and_apply(self):
        out = os.path.join(self.tmp.name, "created.mid")
        code, payload = self.run_cli([
            "create", out, "--tempo", "100", "--time-signature", "3/4", "--note", "C4 0 1 90",
            "--note", "E4 1 1 90", "--unit", "beats", "--track", "Keys:0:4", "--compact",
        ])
        self.assertEqual(code, 0, payload)
        created = MidiFile.read(out)
        self.assertEqual(len(extract_notes(created).notes), 2)
        self.assertAlmostEqual(TempoMap.from_midi(created).initial_bpm, 100.0, places=3)

        edited = os.path.join(self.tmp.name, "edited.mid")
        plan = {"ops": [{"op": "transpose", "semitones": 12},
                        {"op": "add_text", "meta_type": "marker", "text": "A", "at": "1bar"}]}
        code, payload = self.run_cli(["apply", out, "--ops", json.dumps(plan), "-o", edited, "--compact"])
        self.assertEqual(code, 0, payload)
        self.assertEqual([n.pitch for n in extract_notes(MidiFile.read(edited)).sorted()], [72, 76])

    def test_apply_failure_reports_and_writes_nothing(self):
        out = os.path.join(self.tmp.name, "never.mid")
        # unknown op -> plan schema error (exit 1)
        plan = {"ops": [{"op": "transpose", "semitones": 1}, {"op": "bogus_op"}]}
        code, payload = self.run_cli(["apply", self.path, "--ops", json.dumps(plan), "-o", out])
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertFalse(os.path.exists(out))
        # valid plan that fails at runtime -> operation error (exit 3), still nothing written
        plan = {"ops": [{"op": "transpose", "semitones": 1},
                        {"op": "delete_notes", "match": {"pitch": "G9"}, "require_match": True}]}
        code, payload = self.run_cli(["apply", self.path, "--ops", json.dumps(plan), "-o", out])
        self.assertEqual(code, 3)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "E_OP")
        self.assertFalse(os.path.exists(out))

    def test_op_command(self):
        out = os.path.join(self.tmp.name, "op.mid")
        code, payload = self.run_cli(["op", "transpose", self.path, "--semitones", "2",
                                      "--match", "track=1", "-o", out, "--compact"])
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["data"]["result"]["transposed"], 4)

    def test_dry_run_writes_nothing(self):
        code, payload = self.run_cli(["op", "transpose", self.path, "--semitones", "2", "--dry-run", "--compact"])
        self.assertEqual(code, 0)
        self.assertTrue(payload["data"]["written"]["dry_run"])
        self.assertEqual([n.pitch for n in extract_notes(MidiFile.read(self.path)).sorted()][:2], [60, 62])

    def test_in_place_with_backup(self):
        code, payload = self.run_cli(["op", "transpose", self.path, "--semitones", "2",
                                      "--in-place", "--backup", "--compact"])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(self.path + ".bak.mid"))
        self.assertEqual(MidiFile.read(self.path + ".bak.mid").to_bytes(), build_demo().to_bytes())
        self.assertEqual([n.pitch for n in extract_notes(MidiFile.read(self.path)).sorted()][:2], [62, 64])

    def test_missing_output_is_usage_error(self):
        code, payload = self.run_cli(["op", "transpose", self.path, "--semitones", "2"])
        self.assertEqual(code, 1)
        self.assertEqual(payload["error"]["code"], "E_USAGE")

    def test_unknown_selector_field(self):
        code, payload = self.run_cli(["op", "delete_notes", self.path, "--match", '{"nope": 1}',
                                      "-o", os.path.join(self.tmp.name, "x.mid")])
        self.assertEqual(code, 1)
        self.assertEqual(payload["error"]["code"], "E_SCHEMA")

    def test_parse_error_exit_code(self):
        bad = os.path.join(self.tmp.name, "bad.mid")
        with open(bad, "wb") as handle:
            handle.write(b"not a midi file at all")
        code, payload = self.run_cli(["info", bad])
        self.assertEqual(code, 4)
        self.assertEqual(payload["error"]["code"], "E_PARSE")

    def test_ops_schema_selftest_version(self):
        code, payload = self.run_cli(["ops", "--compact"])
        self.assertGreater(payload["data"]["count"], 30)
        code, payload = self.run_cli(["schema", "all", "--compact"])
        self.assertIn("plan", payload["data"])
        code, payload = self.run_cli(["selftest", "--compact"])
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["data"]["ok"])
        code, payload = self.run_cli(["version", "--compact"])
        self.assertTrue(payload["data"]["version"])

    def test_to_json_from_json_roundtrip(self):
        doc_path = os.path.join(self.tmp.name, "doc.json")
        back_path = os.path.join(self.tmp.name, "back.mid")
        code, _ = self.run_cli(["to-json", self.path, "-o", doc_path, "--mode", "both"])
        self.assertEqual(code, 0)
        code, _ = self.run_cli(["from-json", doc_path, "-o", back_path])
        self.assertEqual(code, 0)
        self.assertEqual(open(self.path, "rb").read(), open(back_path, "rb").read())

    def test_pianoroll_and_notes_and_stats(self):
        for argv in (["pianoroll", self.path], ["notes", self.path, "--unit", "beats"], ["stats", self.path]):
            code, payload = self.run_cli(argv)
            self.assertEqual(code, 0, argv)
            self.assertTrue(payload["ok"])


@unittest.skipUnless(HAVE_MIDO, "mido is not installed")
class TestMidoCrossCheck(unittest.TestCase):
    """Parse our output with mido and vice versa."""

    def test_mido_reads_our_file(self):
        midi = build_demo()
        data = midi.to_bytes()
        parsed = mido.MidiFile(file=io.BytesIO(data))
        self.assertEqual(parsed.type, 1)
        self.assertEqual(parsed.ticks_per_beat, 480)
        self.assertEqual(len(parsed.tracks), 3)
        note_ons = [msg for track in parsed.tracks for msg in track if msg.type == "note_on" and msg.velocity > 0]
        self.assertEqual(len(note_ons), 5)
        self.assertAlmostEqual(parsed.length, 1910 / 480.0 * 0.5, places=3)

    def test_mido_running_status_read_by_us(self):
        midi = mido.MidiFile(ticks_per_beat=480)
        track = mido.MidiTrack()
        midi.tracks.append(track)
        track.append(mido.MetaMessage("set_tempo", tempo=500000, time=0))
        for pitch in (60, 62, 64):
            track.append(mido.Message("note_on", note=pitch, velocity=100, time=0))
            track.append(mido.Message("note_off", note=pitch, velocity=64, time=240))
        buffer = io.BytesIO()
        midi.save(file=buffer)
        ours = MidiFile.read_bytes(buffer.getvalue())
        notes = extract_notes(ours).sorted()
        self.assertEqual([n.pitch for n in notes], [60, 62, 64])
        self.assertEqual(notes[0].start, 0)
        self.assertEqual(notes[0].end, 240)
        self.assertEqual(notes[1].start, 240)

    def test_sysex_and_meta_survive_mido(self):
        midi = MidiFile(format=0, division=480)
        track = midi.new_track("S")
        track.add(SysExEvent(bytes_from_hex("f0 7e 7f 09 01 f7"), tick=0))
        track.add(MetaEvent(0x7F, b"\x00\x21\x09", tick=0))
        track.add(ChannelEvent("note_on", 0, 60, 100, tick=0))
        track.add(ChannelEvent("note_off", 0, 60, 64, tick=120))
        parsed = mido.MidiFile(file=io.BytesIO(midi.to_bytes()))
        kinds = [msg.type for track in parsed.tracks for msg in track]
        self.assertIn("sysex", kinds)

    def test_our_edit_is_musically_visible_to_mido(self):
        midi = build_demo()
        apply_plan(midi, [{"op": "transpose", "semitones": 12, "match": {"track": 1}}])
        parsed = mido.MidiFile(file=io.BytesIO(midi.to_bytes()))
        pitches = [msg.note for track in parsed.tracks for msg in track
                   if msg.type == "note_on" and msg.velocity > 0 and msg.channel == 0]
        self.assertEqual(pitches, [72, 74, 76, 77])


if __name__ == "__main__":
    unittest.main(verbosity=2)
