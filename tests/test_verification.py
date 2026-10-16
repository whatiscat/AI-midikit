"""Hardening / regression suite: exercises every op, fuzzes the parser and
walks the whole CLI surface.  Run with:

    python tests/test_verification.py -v
"""

from __future__ import annotations

import io
import json
import os
import random
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from midikit import (  # noqa: E402
    ChannelEvent,
    Division,
    MetaEvent,
    MidiFile,
    MidiTrack,
    SysExEvent,
    SystemEvent,
    apply_plan,
    extract_notes,
    split_by_channel,
    split_by_track,
    split_midi,
    to_document,
    from_document,
    validate,
    write_parts,
)
from midikit.bytestream import bytes_from_hex  # noqa: E402
from midikit.errors import MidiKitError, ParseError, SchemaError  # noqa: E402
from midikit.ops import OPS  # noqa: E402
from midikit.split import render_name, sanitize_filename  # noqa: E402

try:
    import mido  # type: ignore

    HAVE_MIDO = True
except Exception:  # pragma: no cover
    HAVE_MIDO = False


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def rich_file(ppq: int = 480) -> MidiFile:
    """A file that touches most of the format: 4 tracks, sysex, unknown meta."""
    midi = MidiFile(format=1, division=ppq)
    conductor = midi.new_track("Conductor")
    conductor.add(MetaEvent.make_tempo(0, bpm=96))
    conductor.add(MetaEvent.make_time_signature(0, 4, 4))
    conductor.add(MetaEvent.make_key_signature(0, key="Am"))
    conductor.add(MetaEvent.make_marker(0, "Intro"))
    conductor.add(MetaEvent.make_marker(ppq * 4, "Verse"))
    conductor.add(MetaEvent.make_lyric(ppq, "la"))
    conductor.add(MetaEvent(0x7F, b"\x00\x21\x09", tick=0))

    piano = midi.new_track("Piano")
    piano.add(ChannelEvent("program_change", 0, 0, tick=0))
    piano.add(ChannelEvent("control_change", 0, 7, 100, tick=0))
    piano.add(SysExEvent(bytes_from_hex("f0 7e 7f 09 01 f7"), tick=0))
    for index, pitch in enumerate((60, 62, 64, 67)):
        piano.add(ChannelEvent("note_on", 0, pitch, 90 + index, tick=index * ppq))
        piano.add(ChannelEvent("note_off", 0, pitch, 64, tick=index * ppq + ppq - 20))
    piano.add(ChannelEvent("pitch_bend", 0, 0, 0, tick=ppq * 2))
    piano.add(ChannelEvent("channel_aftertouch", 0, 55, None, tick=ppq * 3))

    strings = midi.new_track("Strings")
    strings.add(ChannelEvent("program_change", 1, 48, tick=0))
    for start in range(0, ppq * 4, ppq):
        for pitch in (48, 55, 60):
            strings.add(ChannelEvent("note_on", 1, pitch, 70, tick=start))
            strings.add(ChannelEvent("note_off", 1, pitch, 64, tick=start + ppq))

    drums = midi.new_track("Drums")
    for start in range(0, ppq * 4, ppq // 2):
        drums.add(ChannelEvent("note_on", 9, 42, 70, tick=start))
        drums.add(ChannelEvent("note_off", 9, 42, 64, tick=start + 40))
    drums.add(ChannelEvent("note_on", 9, 36, 110, tick=0))
    drums.add(ChannelEvent("note_off", 9, 36, 64, tick=100))
    return midi


#: one valid argument set per op, used to prove every op runs and round trips
OP_ARGS = {
    "add_text": {"meta_type": "marker", "text": "X", "at": 0},
    "clone_track": {"track": 1, "name": "Copy"},
    "copy_events": {"match": {"type": "note_on", "channel": 0, "limit": 2}, "to_track": 1, "tick_offset": 960},
    "copy_region": {"start": 0, "end": 960, "to": 1920, "repeat": 1, "tracks": [1]},
    "crop": {"start": 0, "end": 1920, "tracks": [1]},
    "dedupe_events": {"tracks": [1]},
    "delete_events": {"match": {"type": "control_change", "control": 7}},
    "delete_notes": {"match": {"track": 1, "pitch": "C4"}},
    "delete_track": {"track": [3]},
    "humanize": {"timing": 4, "velocity": 3, "seed": 1, "match": {"track": 1}},
    "insert_events": {"track": 1, "events": [{"tick": 0, "type": "control_change", "channel": 0, "control": 10, "value": 64}]},
    "insert_notes": {"track": 1, "notes": [{"pitch": "C4", "start": 0, "duration": 240}]},
    "insert_track": {"index": -1, "name": "Extra"},
    "limit_pitch_range": {"min_pitch": "C2", "max_pitch": "C7", "mode": "fold", "match": {"track": 1}},
    "merge_tracks": {"tracks": [1, 2]},
    "move_events": {"match": {"type": "meta", "meta_type": "marker"}, "delta": 10},
    "move_track": {"track": 2, "to": 0},
    "quantize": {"grid": "1/16", "match": {"track": 1}},
    "remove_duplicate_notes": {"tolerance": 5, "match": {"track": 1}},
    "repair": {"dedupe": True},
    "scale_tempo": {"factor": 1.05},
    "set_channel": {"from_channel": 1, "channel": 2},
    "set_division": {"ppq": 960, "rescale": True},
    "set_format": {"format": 1},
    "set_instrument": {"program": "Cello", "channel": 0, "track": 1},
    "set_key_signature": {"key": "Dm"},
    "set_note_length": {"mode": "scale", "value": 0.8, "match": {"track": 1}},
    "set_tempo": {"bpm": 100},
    "set_time_signature": {"numerator": 3, "denominator": 4, "at": 0},
    "set_track_name": {"track": 1, "name": "Renamed"},
    "set_velocity": {"mode": "scale", "value": 0.9, "match": {"track": 1}},
    "sort_events": {"tracks": [1]},
    "split_track_by_channel": {"track": 1},
    "strip_meta": {"meta_types": ["lyric"]},
    "strum": {"window": 20, "tracks": [1]},
    "tempo_ramp": {"bpm_start": 100, "bpm_end": 120, "start": 0, "end": 1920, "steps": 4},
    "transpose": {"semitones": 2, "match": {"track": 1}},
    "update_events": {"match": {"type": "note_on", "channel": 0}, "set": {"velocity": 70}},
    "update_notes": {"match": {"track": 1}, "set": {"velocity": 80}},
}


class TestOpMatrix(unittest.TestCase):
    """Every registered op must be exercised with real arguments."""

    def test_every_op_has_test_arguments(self):
        missing = sorted(set(OPS) - set(OP_ARGS))
        self.assertEqual(missing, [], "ops without test arguments: %s" % missing)
        extra = sorted(set(OP_ARGS) - set(OPS))
        self.assertEqual(extra, [], "test arguments for unknown ops: %s" % extra)

    def test_each_op_runs_and_stays_valid(self):
        broken = []
        for name in sorted(OPS):
            midi = rich_file()
            before = [(e.track, e.pitch, e.start, e.end, e.velocity) for e in extract_notes(midi).sorted()]
            result = apply_plan(midi, [dict(OP_ARGS[name], op=name)])
            if not result["ok"]:
                broken.append("%s: %s" % (name, result["failed"]["error"]["message"]))
                continue
            try:
                data = midi.to_bytes()
            except Exception as exc:  # pragma: no cover - defensive
                broken.append("%s: serialise raised %s" % (name, exc))
                continue
            back = MidiFile.read_bytes(data)
            if back.to_bytes() != data:
                broken.append("%s: re-serialisation not idempotent" % name)
            if [e.fingerprint() for _t, _i, e in back.iter_events()] != [
                e.fingerprint() for _t, _i, e in midi.iter_events()
            ]:
                broken.append("%s: event stream changed on reparse" % name)
            report = validate(midi, normalize=True)
            if report["counts"]["error"]:
                broken.append("%s: introduced %d validation error(s): %s"
                              % (name, report["counts"]["error"], report["issues"][0]["code"]))
            after = extract_notes(midi)
            if len(after.unterminated_note_ons) > 0:
                broken.append("%s: left %d unterminated note(s)" % (name, len(after.unterminated_note_ons)))
            if len(after.orphan_note_offs) > 0:
                broken.append("%s: left %d orphan note_off(s)" % (name, len(after.orphan_note_offs)))
            # the document form must survive the edit too
            doc = to_document(midi, mode="events")
            rebuilt = from_document(json.loads(json.dumps(doc)))
            if rebuilt.to_bytes() != data:
                broken.append("%s: JSON round trip changed the file" % name)
            del before
        self.assertEqual(broken, [], "\n".join(broken))

    def test_ops_are_reported_with_metadata(self):
        for name, op in OPS.items():
            info = op.to_dict()
            self.assertTrue(info["summary"], name)
            self.assertIn(info["category"], ("note", "event", "track", "meta", "file"), name)
            for param in info["params"]:
                self.assertIn(param["type"], (
                    "int", "float", "str", "bool", "json", "int_list", "str_list",
                    "time", "grid", "selector", "note_list", "pitch", "choice", "program",
                ), "%s.%s" % (name, param["name"]))


class TestOpInvariants(unittest.TestCase):
    def test_transpose_is_reversible(self):
        midi = rich_file()
        original = [(n.track, n.channel, n.pitch, n.start, n.end, n.velocity) for n in extract_notes(midi).sorted()]
        apply_plan(midi, [{"op": "transpose", "semitones": 3, "match": {"channel": 0}},
                          {"op": "transpose", "semitones": -3, "match": {"channel": 0}}])
        after = [(n.track, n.channel, n.pitch, n.start, n.end, n.velocity) for n in extract_notes(midi).sorted()]
        self.assertEqual(after, original)

    def test_move_is_reversible(self):
        midi = rich_file()
        apply_plan(midi, [{"op": "move_events", "delta": 120}, {"op": "move_events", "delta": -120}])
        self.assertEqual(extract_notes(midi).sorted()[0].start, extract_notes(rich_file()).sorted()[0].start)

    def test_insert_then_delete_restores_note_set(self):
        midi = rich_file()
        before = [(n.track, n.pitch, n.start, n.end) for n in extract_notes(midi).sorted()]
        apply_plan(midi, [
            {"op": "insert_notes", "track": 1, "notes": [{"pitch": "A5", "start": 10, "duration": 100}]},
            {"op": "delete_notes", "match": {"pitch": "A5"}},
        ])
        after = [(n.track, n.pitch, n.start, n.end) for n in extract_notes(midi).sorted()]
        self.assertEqual(after, before)

    def test_humanize_is_deterministic_and_seeded(self):
        def run(seed):
            midi = rich_file()
            apply_plan(midi, [{"op": "humanize", "timing": 10, "velocity": 8, "seed": seed}])
            return midi.to_bytes()

        self.assertEqual(run(3), run(3))
        self.assertNotEqual(run(3), run(4))

    def test_quantize_is_idempotent(self):
        midi = rich_file()
        plan = [{"op": "quantize", "grid": "1/8", "strength": 1.0}]
        apply_plan(midi, plan)
        once = midi.to_bytes()
        apply_plan(midi, plan)
        self.assertEqual(midi.to_bytes(), once)

    def test_set_velocity_scale_twice_equals_squared(self):
        midi = rich_file()
        apply_plan(midi, [{"op": "set_velocity", "mode": "scale", "value": 0.5}])
        apply_plan(midi, [{"op": "set_velocity", "mode": "scale", "value": 0.5}])
        other = rich_file()
        apply_plan(other, [{"op": "set_velocity", "mode": "scale", "value": 0.25}])
        self.assertEqual([n.velocity for n in extract_notes(midi).sorted()],
                         [n.velocity for n in extract_notes(other).sorted()])

    def test_track_ops_leave_consistent_file(self):
        midi = rich_file()
        result = apply_plan(midi, [
            {"op": "clone_track", "track": 1, "name": "Copy"},
            {"op": "merge_tracks", "tracks": [1, 4]},
            {"op": "move_track", "track": 0, "to": 2},
            {"op": "delete_track", "track": [0]},
        ])
        self.assertTrue(result["ok"], result["failed"])
        self.assertTrue(validate(midi, normalize=True)["ok"])
        self.assertEqual(len(MidiFile.read_bytes(midi.to_bytes()).tracks), len(midi.tracks))


class TestFuzz(unittest.TestCase):
    """Random files and random edits must never break the reader/writer."""

    def _random_file(self, rng: random.Random) -> MidiFile:
        smpte = rng.random() < 0.15
        division = (Division(smpte_fps=rng.choice([24.0, 25.0, 29.97, 30.0]),
                             smpte_ticks_per_frame=rng.choice([40, 80, 100]))
                    if smpte else Division(ppq=rng.choice([96, 192, 480, 960])))
        midi = MidiFile(format=rng.choice([0, 1, 1, 1, 2]), division=division)
        for track_index in range(rng.randint(1, 4)):
            track = midi.new_track("T%d" % track_index if rng.random() < 0.7 else None)
            if rng.random() < 0.5:
                track.add(MetaEvent.make_tempo(rng.randint(0, 2000), bpm=rng.uniform(40, 220)))
            if rng.random() < 0.5:
                track.add(MetaEvent.make_time_signature(0, rng.choice([3, 4, 5, 6, 7]), rng.choice([4, 8])))
            if rng.random() < 0.3:
                track.add(MetaEvent.make_lyric(rng.randint(0, 1000), "歌词 \u00e9\u00e8"))
            if rng.random() < 0.2:
                # stay clear of meta types whose payload has constrained fields
                unknown_type = rng.choice([t for t in range(0x0A, 0x20)] + [0x40, 0x45, 0x60, 0x7E, 0x7F])
                track.add(MetaEvent(unknown_type, bytes(rng.randrange(256) for _ in range(rng.randint(0, 6))),
                                    tick=rng.randint(0, 500)))
            if rng.random() < 0.2:
                track.add(SysExEvent(bytes(rng.randrange(128) for _ in range(rng.randint(0, 8))) + b"\xf7",
                                     tick=rng.randint(0, 500)))
            if rng.random() < 0.08:
                track.add(SystemEvent(0xF8, b"", tick=rng.randint(0, 500)))
            tick = 0
            open_notes = []
            for _ in range(rng.randint(1, 60)):
                tick += rng.choice([0, 0, 1, 30, 120, 240, 480, 960, 5000, 100000])
                roll = rng.random()
                channel = rng.randrange(16)
                if roll < 0.45:
                    pitch = rng.randrange(128)
                    velocity = rng.choice([0, 1, 64, 100, 127])
                    track.add(ChannelEvent("note_on", channel, pitch, velocity, tick))
                    open_notes.append((channel, pitch, tick))
                elif roll < 0.75 and open_notes:
                    channel, pitch, start = open_notes.pop(rng.randrange(len(open_notes)))
                    track.add(ChannelEvent("note_off", channel, pitch, rng.randrange(128), tick))
                elif roll < 0.85:
                    track.add(ChannelEvent("control_change", channel, rng.randrange(128), rng.randrange(128), tick))
                elif roll < 0.92:
                    track.add(ChannelEvent("program_change", channel, rng.randrange(128), None, tick))
                else:
                    track.add(ChannelEvent("pitch_bend", channel, rng.randrange(128), rng.randrange(128), tick))
            for channel, pitch, start in open_notes:
                track.add(ChannelEvent("note_off", channel, pitch, 64, start + rng.randint(1, 480)))
            track.sort_events()
        return midi

    def test_random_files_round_trip(self):
        rng = random.Random(20240607)
        for iteration in range(60):
            midi = self._random_file(rng)
            running = iteration % 3 == 0
            zero_off = iteration % 4 == 0
            data = midi.to_bytes(running_status=running, note_off_as_note_on_0=zero_off)
            back = MidiFile.read_bytes(data)
            if zero_off:
                # writing note-offs as note-on velocity 0 is a deliberate
                # representation change: compare musical content instead
                self.assertEqual(
                    [(n.track, n.channel, n.pitch, n.start, n.end, n.velocity) for n in extract_notes(back).sorted()],
                    [(n.track, n.channel, n.pitch, n.start, n.end, n.velocity) for n in extract_notes(midi).sorted()],
                    "iteration %d: notes changed" % iteration,
                )
            else:
                self.assertEqual(
                    [e.fingerprint() for _t, _i, e in back.iter_events()],
                    [e.fingerprint() for _t, _i, e in midi.iter_events()],
                    "iteration %d: event stream changed" % iteration,
                )
            again = back.to_bytes(running_status=running, note_off_as_note_on_0=zero_off)
            self.assertEqual(again, data, "iteration %d: not byte idempotent" % iteration)
            self.assertEqual(
                MidiFile.read_bytes(again).to_bytes(running_status=running, note_off_as_note_on_0=zero_off), data
            )
            validate(back)          # must not raise
            extract_notes(back)     # must not raise
            if HAVE_MIDO:
                mido.MidiFile(file=io.BytesIO(data))

    def test_random_edits_never_corrupt(self):
        rng = random.Random(99)
        names = sorted(OPS)
        for _ in range(40):
            midi = self._random_file(rng)
            plan = [dict(OP_ARGS[rng.choice(names)], op=rng.choice(list(OP_ARGS)))]
            try:
                result = apply_plan(midi, plan)
            except MidiKitError:
                continue
            if not result["ok"]:
                continue
            data = midi.to_bytes()
            self.assertEqual(MidiFile.read_bytes(data).to_bytes(), data)

    def test_truncated_and_corrupted_input_never_crashes(self):
        data = rich_file().to_bytes()
        for cut in range(0, len(data), 7):
            chunk = data[:cut]
            try:
                midi = MidiFile.read_bytes(chunk)
                midi.to_bytes()
            except MidiKitError:
                pass
            except Exception as exc:  # pragma: no cover - defensive
                self.fail("truncation at %d raised %s: %s" % (cut, type(exc).__name__, exc))

    def test_bitflip_fuzz(self):
        rng = random.Random(7)
        data = rich_file().to_bytes()
        for _ in range(400):
            corrupted = bytearray(data)
            position = rng.randrange(len(corrupted))
            corrupted[position] ^= 1 << rng.randrange(8)
            try:
                midi = MidiFile.read_bytes(bytes(corrupted))
                midi.to_bytes()
                validate(midi)
            except MidiKitError:
                pass
            except Exception as exc:  # pragma: no cover - defensive
                self.fail("bit flip at %d raised %s: %s" % (position, type(exc).__name__, exc))

    def test_header_fuzz(self):
        data = bytearray(rich_file().to_bytes())
        for value in (0, 1, 2, 3, 0xFF):
            mutated = bytearray(data)
            mutated[9] = value                      # format byte
            try:
                midi = MidiFile.read_bytes(bytes(mutated))
            except MidiKitError:
                continue
            self.assertIn(midi.format, (0, 1, 2, 3, 0xFF))


class TestValidatorCoverage(unittest.TestCase):
    def test_smpte_offset_range_is_checked(self):
        midi = MidiFile(format=0, division=480)
        track = midi.new_track("t")
        track.add(MetaEvent(0x54, bytes([0x11, 0x57, 0x1F, 0x13, 0x23]), tick=0))   # minute 87
        codes = [issue["code"] for issue in validate(midi)["issues"]]
        self.assertIn("V_SMPTE_RANGE", codes)

    def test_valid_smpte_offset_passes(self):
        midi = MidiFile(format=0, division=480)
        track = midi.new_track("t")
        track.add(MetaEvent.make_smpte_offset(0, hour=1, minute=2, second=3, frame=4, subframe=5, frame_rate=25))
        report = validate(midi, normalize=True)
        self.assertTrue(report["ok"], report["issues"])

    def test_selector_control_and_velocity_filters(self):
        from midikit.selectors import EventSelector

        midi = rich_file()
        self.assertEqual(len(EventSelector({"control": 7}).filter(midi)), 1)
        self.assertEqual(len(EventSelector({"control": "channel_volume"}).filter(midi)), 1)
        self.assertEqual(len(EventSelector({"control": 10}).filter(midi)), 0)
        self.assertEqual(len(EventSelector({"program": 48}).filter(midi)), 1)
        self.assertEqual(len(EventSelector({"velocity": 90}).filter(midi)), 1)
        self.assertEqual(len(EventSelector({"velocity_min": 100}).filter(midi)), 1)
        self.assertEqual(len(EventSelector({"value_min": 100}).filter(midi)), 1)
        # the fixture's pitch bend sits at the bottom of its range
        self.assertEqual(len(EventSelector({"type": "pitch_bend", "value": 0}).filter(midi)), 1)
        self.assertEqual(len(EventSelector({"type": "pitch_bend", "value_min": 8192}).filter(midi)), 0)


class TestSplitFeature(unittest.TestCase):
    def setUp(self):
        self.midi = rich_file()

    def test_split_by_channel(self):
        parts = split_by_channel(self.midi)
        self.assertEqual([p.channel for p in parts], [0, 1, 9])
        total = sum(p.notes for p in parts)
        self.assertEqual(total, len(extract_notes(self.midi).notes))
        for part in parts:
            self.assertEqual(part.midi.format, 1)
            self.assertEqual(len(part.midi.tracks), 2)
            self.assertEqual(part.midi.tracks[0].name, "Conductor")
            report = validate(part.midi, normalize=True)
            self.assertTrue(report["ok"], report["issues"])
            self.assertEqual(part.midi.division.to_dict(), self.midi.division.to_dict())

    def test_every_part_keeps_tempo_and_time_signature(self):
        for part in split_by_channel(self.midi):
            tempos = [e for _t, _i, e in part.midi.iter_events() if e.is_meta and e.type_byte == 0x51]
            sigs = [e for _t, _i, e in part.midi.iter_events() if e.is_meta and e.type_byte == 0x58]
            keys = [e for _t, _i, e in part.midi.iter_events() if e.is_meta and e.type_byte == 0x59]
            self.assertTrue(tempos, part.name)
            self.assertTrue(sigs, part.name)
            self.assertTrue(keys, part.name)
            # a part must never carry another channel's events
            for _t, _i, event in part.midi.iter_events():
                if event.is_channel:
                    self.assertEqual(event.channel, part.channel)

    def test_split_channel_subset_and_min_events(self):
        parts = split_by_channel(self.midi, channels=[9])
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].channel, 9)
        self.assertEqual(parts[0].name, "Drums")
        self.assertEqual(split_by_channel(self.midi, channels=[7]), [])
        self.assertEqual(split_by_channel(self.midi, min_events=10 ** 6), [])

    def test_split_without_conductor_is_format_zero_with_channel_prefix(self):
        parts = split_by_channel(self.midi, keep_conductor=False, channels=[0])
        self.assertEqual(len(parts), 1)
        part = parts[0]
        self.assertEqual(part.midi.format, 0)
        self.assertEqual(len(part.midi.tracks), 1)
        events = part.midi.tracks[0].events
        self.assertTrue(events[0].is_meta and events[0].type_byte == 0x20, "channel prefix must be first")
        self.assertEqual(events[0].channel, 0)
        self.assertTrue(events[1].is_meta and events[1].type_byte == 0x03, "track name follows the prefix")
        self.assertEqual([e for e in events if e.is_meta and e.type_byte == 0x20].__len__(), 1)
        report = validate(part.midi, normalize=True)
        self.assertTrue(report["ok"], report["issues"])
        self.assertFalse([i for i in report["issues"] if i["code"] == "V_CHANNEL_PREFIX_POSITION"])

    def test_split_by_track(self):
        parts = split_by_track(self.midi)
        self.assertEqual(len(parts), len(self.midi.tracks))
        self.assertEqual([p.name for p in parts], [t.name for t in self.midi.tracks])
        for part in parts:
            self.assertEqual(part.midi.format, 0)
            self.assertTrue(validate(part.midi, normalize=True)["ok"])

    def test_channel_prefix_first_for_every_format_zero_part(self):
        for part in split_by_channel(self.midi, keep_conductor=False):
            first = part.midi.tracks[0].events[0]
            self.assertTrue(first.is_meta and first.type_byte == 0x20, part.name)
            report = validate(part.midi, normalize=True)
            self.assertTrue(report["ok"], report["issues"])
        for part in split_by_track(self.midi):
            channels = {e.channel for e in part.midi.tracks[0].events if e.is_channel}
            if len(channels) == 1:
                first = part.midi.tracks[0].events[0]
                self.assertTrue(first.is_meta and first.type_byte == 0x20, part.name)

    def test_split_midi_dispatch_and_errors(self):
        self.assertEqual(len(split_midi(self.midi, by="channel")), 3)
        self.assertEqual(len(split_midi(self.midi, by="track")), 4)
        with self.assertRaises(SchemaError):
            split_midi(self.midi, by="nope")
        with self.assertRaises(SchemaError):
            split_by_channel(self.midi, channels=[99])

    def test_name_template_and_sanitising(self):
        parts = split_by_channel(self.midi)
        self.assertEqual(render_name("{stem}_{ch2}.mid", parts[0], "song"), "song_01.mid")
        self.assertEqual(render_name("{stem}-{name}.mid", parts[2], "song"), "song-Drums.mid")
        self.assertEqual(render_name("{index2}{channel1}", parts[1], "s"), "012.mid")
        self.assertEqual(sanitize_filename("Piano (Acoustic)"), "Piano_Acoustic")
        self.assertEqual(sanitize_filename("钢琴 主旋律"), "钢琴_主旋律")
        self.assertEqual(sanitize_filename("///"), "part")

    def test_write_parts_creates_standalone_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            parts = split_by_channel(self.midi)
            rows = write_parts(parts, tmp, stem="song")
            self.assertEqual(len(rows), 3)
            for row in rows:
                self.assertTrue(os.path.isfile(row["path"]))
                loaded = MidiFile.read(row["path"])
                self.assertTrue(validate(loaded, normalize=True)["ok"])
                self.assertEqual(len(extract_notes(loaded).notes), row["notes"])
                self.assertEqual(row["bytes"], os.path.getsize(row["path"]))
            self.assertEqual(sorted(os.listdir(tmp)),
                             ["song_ch01.mid", "song_ch02.mid", "song_ch10.mid"])

    def test_write_parts_dry_run_and_collisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            parts = split_by_channel(self.midi)
            rows = write_parts(parts, tmp, template="{stem}.mid", stem="same", dry_run=True)
            self.assertEqual(os.listdir(tmp), [])
            self.assertEqual(len(set(row["filename"] for row in rows)), 3)
            rows = write_parts(parts, os.path.join(tmp, "nested"), template="{stem}.mid", stem="same")
            self.assertEqual(len(rows), 3)
            self.assertEqual(len(set(row["filename"] for row in rows)), 3)
            self.assertEqual(len(os.listdir(os.path.join(tmp, "nested"))), 3)

    def test_cli_split_command(self):
        from midikit.cli import main

        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "in.mid")
            self.midi.write(source)
            out_dir = os.path.join(tmp, "stems")
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = main(["split", source, "--out-dir", out_dir, "--compact"])
            payload = json.loads(buffer.getvalue())
            self.assertEqual(code, 0, payload)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["data"]["count"], 3)
            self.assertTrue(all(item["ok"] for item in payload["data"]["validation"]))
            self.assertEqual(len(os.listdir(out_dir)), 3)
            # dry run writes nothing
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = main(["split", source, "--out-dir", os.path.join(tmp, "none"), "--dry-run", "--compact"])
            payload = json.loads(buffer.getvalue())
            self.assertEqual(code, 0)
            self.assertTrue(payload["data"]["dry_run"])
            self.assertFalse(os.path.exists(os.path.join(tmp, "none")))
            # by track
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = main(["split", source, "--out-dir", os.path.join(tmp, "t"), "--by", "track", "--compact"])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(buffer.getvalue())["data"]["count"], 4)


class TestCliMatrix(unittest.TestCase):
    """Every command answers with a valid envelope and the documented exit code."""

    def run_cli(self, argv):
        from midikit.cli import main

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(argv)
        text = buffer.getvalue().strip()
        if text.startswith("{"):
            try:
                return code, json.loads(text)
            except ValueError:
                # --format jsonl prints one object per line
                return code, {"ok": True, "lines": [json.loads(line) for line in text.splitlines() if line.strip()]}
        return code, {"raw": text}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "demo.mid")
        rich_file().write(self.path)
        self.other = os.path.join(self.tmp.name, "other.mid")
        m = rich_file()
        apply_plan(m, [{"op": "transpose", "semitones": 2}])
        m.write(self.other)

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_only_commands(self):
        cases = [
            ["info", self.path],
            ["info", self.path, "--notes"],
            ["tracks", self.path],
            ["dump", self.path, "--type", "note_on", "--limit", "5"],
            ["dump", self.path, "--format", "jsonl", "--limit", "2"],
            ["notes", self.path, "--unit", "beats"],
            ["stats", self.path],
            ["pianoroll", self.path],
            ["pianoroll", self.path, "--pitch-min", "50", "--pitch-max", "80", "--velocity"],
            ["validate", self.path],
            ["diff", self.path, self.other],
            ["ops"],
            ["ops", "--describe", "transpose"],
            ["schema", "all"],
            ["version"],
            ["selftest"],
        ]
        for argv in cases:
            code, payload = self.run_cli(argv + ["--compact"])
            self.assertEqual(code, 0, "%s -> exit %s (%s)" % (argv[0], code, payload.get("error")))
            self.assertTrue(payload.get("ok"), argv)
            if "lines" in payload:                      # --format jsonl: one object per line
                self.assertTrue(payload["lines"])
                self.assertIn("tick", payload["lines"][0])
                continue
            self.assertEqual(payload.get("command", "").split()[0], argv[0])

    def test_write_commands(self):
        out = os.path.join(self.tmp.name, "out.mid")
        cases = [
            (["op", "transpose", self.path, "--semitones", "1", "-o", out], 0),
            (["op", "set_tempo", self.path, "--bpm", "90", "-o", out], 0),
            (["apply", self.path, "--ops", json.dumps({"ops": [{"op": "sort_events"}]}), "-o", out], 0),
            (["create", os.path.join(self.tmp.name, "new.mid"), "--note", "C4 0 1 90", "--tempo", "100"], 0),
            (["from-json", self.doc_path(), "-o", out], 0),
            (["to-json", self.path, "-o", os.path.join(self.tmp.name, "doc.json")], 0),
            (["split", self.path, "--out-dir", os.path.join(self.tmp.name, "stems")], 0),
        ]
        for argv, expected in cases:
            code, payload = self.run_cli(argv + ["--compact"])
            self.assertEqual(code, expected, "%s -> %s" % (argv[0], payload.get("error")))
            self.assertTrue(payload.get("ok"), argv)

    def doc_path(self):
        path = os.path.join(self.tmp.name, "doc.json")
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(to_document(rich_file(), mode="events"), handle, ensure_ascii=False)
        return path

    def test_error_exit_codes(self):
        out = os.path.join(self.tmp.name, "never.mid")
        cases = [
            (["info", os.path.join(self.tmp.name, "missing.mid")], 4, "E_PARSE"),
            (["op", "transpose", self.path, "--semitones", "1"], 1, "E_USAGE"),
            (["op", "nope", self.path, "-o", out], 1, "E_USAGE"),
            (["op", "delete_notes", self.path, "--match", '{"nope":1}', "-o", out], 1, "E_SCHEMA"),
            (["op", "delete_notes", self.path, "--match", '{"pitch":"G9"}', "--require-match", "-o", out], 3, "E_OP"),
            (["apply", self.path, "--ops", '{"ops":[{"op":"bogus"}]}', "-o", out], 1, "E_SCHEMA"),
            (["apply", self.path, "--ops", '{"ops":[{"op":"delete_notes","match":{"pitch":"G9"},"require_match":true}]}',
              "-o", out], 3, "E_OP"),
            (["split", self.path], 1, "E_USAGE"),
        ]
        for argv, expected, code in cases:
            exit_code, payload = self.run_cli(argv + ["--compact"])
            self.assertEqual(exit_code, expected, "%s -> %s" % (argv[0], payload))
            self.assertFalse(payload.get("ok"), argv)
            self.assertEqual(payload.get("error", {}).get("code"), code, argv)
        self.assertFalse(os.path.exists(out))

    def test_validation_failure_exit_code(self):
        broken = os.path.join(self.tmp.name, "broken.mid")
        midi = MidiFile(format=0, division=480)
        track = midi.new_track("t")
        track.add(ChannelEvent("note_on", 0, 60, 100, tick=0))
        track.add(ChannelEvent("note_off", 0, 62, 64, tick=10))
        # a format 0 file with two tracks is invalid
        midi.tracks.append(MidiTrack([MetaEvent.make_end_of_track(0)]))
        midi.write(broken, normalize_eot=False)
        code, payload = self.run_cli(["validate", broken, "--compact"])
        self.assertEqual(code, 2)
        self.assertFalse(payload["data"]["ok"])
        self.assertGreater(payload["data"]["counts"]["error"], 0)

    def test_cli_launcher_and_module_entry(self):
        for argv in ([sys.executable, os.path.join(ROOT, "midi.py"), "version", "--compact"],
                     [sys.executable, "-m", "midikit", "version", "--compact"]):
            proc = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(json.loads(proc.stdout)["ok"])

    def test_documents_survive_cli_json_round_trip(self):
        doc = os.path.join(self.tmp.name, "rt.json")
        back = os.path.join(self.tmp.name, "rt.mid")
        self.run_cli(["to-json", self.path, "-o", doc, "--mode", "both", "--compact"])
        self.run_cli(["from-json", doc, "-o", back, "--compact"])
        self.assertEqual(open(self.path, "rb").read(), open(back, "rb").read())


class TestSplitOnRealPiece(unittest.TestCase):
    """The split feature on the composed piece: 5 stems + one conductor."""

    PATH = os.path.join(ROOT, "pieces", "sunrise.mid")

    @unittest.skipUnless(os.path.exists(PATH), "demo piece is not present")
    def test_piece_splits_into_stems(self):
        midi = MidiFile.read(self.PATH)
        parts = split_by_channel(midi)
        self.assertEqual(len(parts), 5)
        self.assertEqual(sum(p.notes for p in parts), len(extract_notes(midi).notes))
        with tempfile.TemporaryDirectory() as tmp:
            rows = write_parts(parts, tmp, stem="sunrise")
            self.assertEqual(len(rows), 5)
            for row in rows:
                loaded = MidiFile.read(row["path"])
                self.assertTrue(validate(loaded, normalize=True)["ok"], row["path"])
                markers = [e.text for _t, _i, e in loaded.iter_events() if e.is_meta and e.type_byte == 6]
                self.assertEqual(markers, ["Intro", "Verse", "Chorus", "Outro"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
