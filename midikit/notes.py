"""Note extraction (note_on / note_off pairing) and note-level helpers."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .constants import GM_DRUM_NOTES, note_name, note_number
from .errors import SchemaError
from .events import DEFAULT_OPTS, ChannelEvent, DecodeOptions, Event, MetaEvent
from .smf import MidiFile, MidiTrack


class Note:
    """A sounded note: one note_on paired with one note_off."""

    __slots__ = (
        "track",
        "channel",
        "pitch",
        "velocity",
        "start",
        "end",
        "off_velocity",
        "on_event",
        "off_event",
        "unterminated",
    )

    def __init__(
        self,
        track: int,
        channel: int,
        pitch: int,
        velocity: int,
        start: int,
        end: int,
        off_velocity: int = 0,
        on_event: Optional[Event] = None,
        off_event: Optional[Event] = None,
        unterminated: bool = False,
    ) -> None:
        self.track = int(track)
        self.channel = int(channel)
        self.pitch = int(pitch)
        self.velocity = int(velocity)
        self.start = int(start)
        self.end = int(end)
        self.off_velocity = int(off_velocity)
        self.on_event = on_event
        self.off_event = off_event
        self.unterminated = bool(unterminated)

    @property
    def duration(self) -> int:
        return self.end - self.start

    @property
    def is_drum(self) -> bool:
        return self.channel == 9

    def clone(self) -> "Note":
        return Note(
            self.track,
            self.channel,
            self.pitch,
            self.velocity,
            self.start,
            self.end,
            self.off_velocity,
            self.on_event,
            self.off_event,
            self.unterminated,
        )

    def to_dict(self, opts: Optional[DecodeOptions] = None, unit: str = "ticks", tempo_map=None) -> Dict[str, Any]:
        o = opts or DEFAULT_OPTS
        d: Dict[str, Any] = {
            "track": self.track,
            "channel": self.channel,
            "channel_display": self.channel + 1,
            "note": self.pitch,
            "start": self.start,
            "end": self.end,
            "duration": self.duration,
            "velocity": self.velocity,
        }
        if o.names:
            d["pitch"] = note_name(self.pitch)
            if self.channel == o.drum_channel:
                d["drum"] = GM_DRUM_NOTES.get(self.pitch)
        if self.unterminated:
            d["unterminated"] = True
        if unit != "ticks" and tempo_map is not None:
            if unit == "beats":
                d["start_beats"] = round(tempo_map.tick_to_beat(self.start), 6)
                d["duration_beats"] = round(tempo_map.tick_to_beat(self.duration), 6)
            elif unit == "seconds":
                d["start_seconds"] = round(tempo_map.tick_to_seconds(self.start), 6)
                d["duration_seconds"] = round(tempo_map.tick_to_seconds(self.end) - tempo_map.tick_to_seconds(self.start), 6)
        if tempo_map is not None:
            pos = tempo_map.bar_beat_tick(self.start)
            d["bar"] = pos["bar"]
            d["beat"] = pos["beat"]
        return d

    def describe(self, opts: Optional[DecodeOptions] = None, tempo_map=None) -> str:
        base = "t%-2d ch%-2d %-6s vel=%-3d tick %d..%d (%d)" % (
            self.track,
            self.channel + 1,
            note_name(self.pitch),
            self.velocity,
            self.start,
            self.end,
            self.duration,
        )
        if tempo_map is not None:
            pos = tempo_map.bar_beat_tick(self.start)
            base += " bar %d:%d" % (pos["bar"], pos["beat"])
        if self.unterminated:
            base += " [unterminated]"
        return base

    def __repr__(self) -> str:
        return "Note(%s)" % self.describe()


def note_from_dict(d: Dict[str, Any], default_track: int = 0, default_channel: int = 0, unit: str = "ticks") -> Note:
    """Build a Note from JSON.  Accepts pitch/note, start/at, duration/end/length."""
    if not isinstance(d, dict):
        raise SchemaError("note must be an object, got %s" % type(d).__name__)
    pitch_raw = d.get("note", d.get("pitch", d.get("key", d.get("n"))))
    if pitch_raw is None:
        raise SchemaError("note needs a pitch (field 'note' or 'pitch')")
    pitch = note_number(pitch_raw)
    start = int(d.get("start", d.get("at", d.get("tick", d.get("time", 0)))) or 0)
    if d.get("end") is not None:
        end = int(d["end"])
    elif d.get("duration") is not None:
        end = start + int(d["duration"])
    elif d.get("length") is not None:
        end = start + int(d["length"])
    else:
        end = start + int(d.get("d", 0) or 0)
    if end < start:
        raise SchemaError("note end (%d) is before its start (%d)" % (end, start), note=d)
    return Note(
        track=int(d.get("track", default_track) or 0),
        channel=int(d.get("channel", default_channel) if d.get("channel") is not None else default_channel),
        pitch=pitch,
        velocity=int(d.get("velocity", d.get("vel", 100)) if d.get("velocity", d.get("vel")) is not None else 100),
        start=start,
        end=end,
    )


def parse_note_spec(
    spec: str,
    unit: str = "ticks",
    default_track: int = 0,
    default_channel: int = 0,
    tempo_map=None,
) -> Note:
    """Parse 'C4 0 480 100 0 1' -> pitch, start, duration, [velocity], [channel], [track].

    start/duration follow the CLI unit (ticks, beats, seconds or bars).
    """
    from .tempo import parse_time

    parts = [p for p in str(spec).replace(",", " ").split() if p]
    if len(parts) < 3:
        raise SchemaError(
            "note spec needs at least: pitch start duration (got %r)" % (spec,),
            spec=spec,
        )
    pitch = note_number(parts[0])
    start = parse_time(parts[1], tempo_map, default_unit=unit)
    duration = parse_time(parts[2], tempo_map, default_unit=unit)
    velocity = int(float(parts[3])) if len(parts) > 3 else 100
    channel = int(parts[4]) if len(parts) > 4 else default_channel
    track = int(parts[5]) if len(parts) > 5 else default_track
    if not 0 <= velocity <= 127:
        raise SchemaError("velocity out of range 0-127: %r" % (velocity,))
    if not 0 <= channel <= 15:
        raise SchemaError("channel out of range 0-15: %r" % (channel,))
    return Note(track, channel, pitch, velocity, start, start + duration)


class NoteExtraction:
    """Notes plus the pairing problems found while extracting them."""

    __slots__ = ("notes", "orphan_note_offs", "unterminated_note_ons", "pairing")

    def __init__(self, notes=None, pairing: str = "lifo") -> None:
        self.notes: List[Note] = list(notes or [])
        self.orphan_note_offs: List[Dict[str, Any]] = []
        self.unterminated_note_ons: List[Dict[str, Any]] = []
        self.pairing = pairing

    def by_track(self) -> Dict[int, List[Note]]:
        out: Dict[int, List[Note]] = {}
        for note in self.notes:
            out.setdefault(note.track, []).append(note)
        return out

    def sorted(self) -> List[Note]:
        return sorted(self.notes, key=lambda n: (n.track, n.start, n.pitch, n.channel))

    def to_dict(self, opts: Optional[DecodeOptions] = None, tempo_map=None, unit: str = "ticks") -> Dict[str, Any]:
        return {
            "count": len(self.notes),
            "pairing": self.pairing,
            "unterminated": len(self.unterminated_note_ons),
            "orphan_note_offs": len(self.orphan_note_offs),
            "notes": [n.to_dict(opts, unit=unit, tempo_map=tempo_map) for n in self.sorted()],
        }


def extract_notes(
    midi: MidiFile,
    track_indices: Optional[Iterable[int]] = None,
    pairing: str = "lifo",
    close_unterminated: bool = True,
) -> NoteExtraction:
    """Pair note_on / note_off events into notes.

    Note-on with velocity 0 counts as a note-off (as required by the spec).
    Overlapping notes of the same pitch and channel are matched LIFO by default
    (the most recently started note is the first to be released).
    """
    if pairing not in ("lifo", "fifo"):
        raise SchemaError("pairing must be 'lifo' or 'fifo': %r" % (pairing,))
    wanted = set(track_indices) if track_indices is not None else None
    result = NoteExtraction(pairing=pairing)
    for track_index, track in enumerate(midi.tracks):
        if wanted is not None and track_index not in wanted:
            continue
        open_notes: Dict[Tuple[int, int], List[Tuple[ChannelEvent, int]]] = {}
        last_tick = 0
        for event_index, event in enumerate(track.events):
            last_tick = max(last_tick, event.tick)
            if not isinstance(event, ChannelEvent):
                continue
            if event.is_note_on:
                open_notes.setdefault((event.channel, event.value1), []).append((event, event_index))
            elif event.is_note_off:
                key = (event.channel, event.value1)
                stack = open_notes.get(key)
                if not stack:
                    result.orphan_note_offs.append(
                        {
                            "track": track_index,
                            "event_index": event_index,
                            "tick": event.tick,
                            "channel": event.channel,
                            "note": event.value1,
                            "pitch": note_name(event.value1),
                        }
                    )
                    continue
                if pairing == "fifo":
                    on_event, _on_index = stack.pop(0)
                else:
                    on_event, on_index = stack.pop()
                    # a zero-length note may have been stored off-before-on by sorting
                    if on_event.tick == event.tick and on_index > event_index and stack:
                        on_event, _on_index = stack.pop()
                result.notes.append(
                    Note(
                        track=track_index,
                        channel=event.channel,
                        pitch=event.value1,
                        velocity=on_event.value2,
                        start=on_event.tick,
                        end=event.tick,
                        off_velocity=event.value2,
                        on_event=on_event,
                        off_event=event,
                    )
                )
        for (channel, pitch), stack in sorted(open_notes.items()):
            for on_event, event_index in stack:
                result.unterminated_note_ons.append(
                    {
                        "track": track_index,
                        "event_index": event_index,
                        "tick": on_event.tick,
                        "channel": channel,
                        "note": pitch,
                        "pitch": note_name(pitch),
                    }
                )
                if close_unterminated:
                    result.notes.append(
                        Note(
                            track=track_index,
                            channel=channel,
                            pitch=pitch,
                            velocity=on_event.value2,
                            start=on_event.tick,
                            end=max(last_tick, on_event.tick),
                            on_event=on_event,
                            off_event=None,
                            unterminated=True,
                        )
                    )
    return result


def notes_to_events(notes: Sequence[Note], encoding: str = "utf-8") -> List[Event]:
    """Turn notes into note_on / note_off events (sorted, note-offs first)."""
    events: List[Event] = []
    for note in notes:
        end = note.end if note.end > note.start else note.start + 1
        events.append(ChannelEvent("note_on", note.channel, note.pitch, note.velocity, note.start))
        events.append(ChannelEvent("note_off", note.channel, note.pitch, note.off_velocity or 64, end))
    decorated = [(ev.sort_key(i), ev) for i, ev in enumerate(events)]
    decorated.sort(key=lambda item: item[0])
    return [ev for _key, ev in decorated]


def polyphony_at(notes: Sequence[Note], tick: int) -> int:
    return sum(1 for n in notes if n.start <= tick < n.end)


def max_polyphony(notes: Sequence[Note]) -> int:
    points: List[Tuple[int, int]] = []
    for note in notes:
        points.append((note.start, 1))
        points.append((max(note.end, note.start + 1), -1))
    points.sort()
    current = 0
    best = 0
    for _tick, delta in points:
        current += delta
        best = max(best, current)
    return best
