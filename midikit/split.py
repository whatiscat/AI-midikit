"""Split one MIDI file into one file per channel or per track.

Different from the 'split_track_by_channel' op, which puts every channel into a
separate track of the same file: this module produces independent, self
contained files (each one keeps the tempo map and the section markers so it can
be opened and played on its own).
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .constants import program_name
from .errors import SchemaError, WriteError
from .events import ChannelEvent, Event, MetaEvent
from .notes import extract_notes
from .smf import MidiFile, MidiTrack, sort_track_if_unsorted

#: meta events copied into every part so the file stands on its own
CONDUCTOR_META = (0x00, 0x21, 0x51, 0x54, 0x58, 0x59, 0x7F)
#: song level text: markers, cue points, copyright, generic text
SECTION_META = (0x01, 0x02, 0x06, 0x07)

DEFAULT_TEMPLATE = "{stem}_ch{ch2}.mid"
DEFAULT_TRACK_TEMPLATE = "{stem}_t{index2}_{name}.mid"

_UNSAFE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff._-]+")


def sanitize_filename(text: str, fallback: str = "part", max_length: int = 48) -> str:
    """Make a string safe for a file name (keeps CJK characters)."""
    cleaned = _UNSAFE.sub("_", str(text or "").strip())
    cleaned = cleaned.strip("._-")
    if not cleaned:
        cleaned = fallback
    return cleaned[:max_length]


class SplitPart:
    """One extracted part: a standalone MidiFile plus its metadata."""

    __slots__ = ("index", "channel", "source_tracks", "name", "midi", "notes", "events", "path")

    def __init__(
        self,
        index: int,
        channel: Optional[int],
        source_tracks: Sequence[int],
        name: str,
        midi: MidiFile,
        notes: int,
        events: int,
        path: Optional[str] = None,
    ) -> None:
        self.index = index
        self.channel = channel
        self.source_tracks = list(source_tracks)
        self.name = name
        self.midi = midi
        self.notes = notes
        self.events = events
        self.path = path

    def to_dict(self, include_bytes: bool = True) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "index": self.index,
            "channel": self.channel,
            "channel_display": None if self.channel is None else self.channel + 1,
            "name": self.name,
            "source_tracks": self.source_tracks,
            "notes": self.notes,
            "events": self.events,
            "tracks": len(self.midi.tracks),
            "path": self.path,
        }
        if include_bytes:
            out["bytes"] = len(self.midi.to_bytes())
        return out

    def __repr__(self) -> str:
        return "SplitPart(channel=%s, name=%r, notes=%d)" % (self.channel, self.name, self.notes)


def _conductor_events(midi: MidiFile, keep_markers: bool, tracks: Iterable[int]) -> List[Event]:
    allowed = set(tracks)
    out: List[Event] = []
    for index, track in enumerate(midi.tracks):
        if index not in allowed:
            continue
        for event in track.events:
            if not event.is_meta:
                if event.is_sysex and event.tick == 0:
                    continue
                continue
            if event.type_byte in CONDUCTOR_META or (keep_markers and event.type_byte in SECTION_META):
                out.append(event.clone())
    out.sort(key=lambda ev: (ev.tick, ev.type_byte))
    return out


def _channel_events(midi: MidiFile, channel: int) -> List[Event]:
    out: List[Event] = []
    for track in midi.tracks:
        for event in track.events:
            if event.is_channel and event.channel == channel:
                out.append(event.clone())
    out.sort(key=lambda ev: ev.tick)
    return out


def _track_events(track: MidiTrack) -> List[Event]:
    return [ev.clone() for ev in track.events if not (ev.is_meta and ev.type_byte == 0x2F)]


def _part_name_for_channel(midi: MidiFile, channel: int, events: Sequence[Event]) -> str:
    best_name = None
    best_count = -1
    for track in midi.tracks:
        count = sum(1 for ev in track.events if ev.is_channel and ev.channel == channel)
        if count > best_count and track.name:
            best_name, best_count = track.name, count
    programs = [ev.value1 for ev in events if isinstance(ev, ChannelEvent) and ev.type == "program_change"]
    if best_name and best_count > 0:
        if programs:
            return "%s (%s)" % (best_name, program_name(programs[-1]))
        return best_name
    if channel == 9:
        return "Drums"
    if programs:
        return program_name(programs[-1])
    return "Channel %d" % (channel + 1)


def split_by_channel(
    midi: MidiFile,
    channels: Optional[Sequence[int]] = None,
    keep_conductor: bool = True,
    keep_markers: bool = True,
    keep_extra_chunks: bool = True,
    output_format: Optional[int] = None,
    min_events: int = 1,
) -> List[SplitPart]:
    """One standalone file per MIDI channel.

    Every part keeps the tempo map (and optionally markers) so it plays
    correctly on its own.
    """
    available = sorted({ev.channel for _t, _i, ev in midi.iter_events() if ev.is_channel})
    wanted = [_validate_channel(ch) for ch in channels] if channels is not None else available
    parts: List[SplitPart] = []
    for channel in wanted:
        events = _channel_events(midi, channel)
        if len(events) < max(1, int(min_events)):
            continue
        name = _part_name_for_channel(midi, channel, events)
        part = MidiFile(
            format=int(output_format) if output_format is not None else 1,
            division=midi.division,
            extra_chunks=list(midi.extra_chunks) if keep_extra_chunks else [],
        )
        part.header_extra = midi.header_extra
        if keep_conductor:
            conductor = MidiTrack(
                _conductor_events(midi, keep_markers, range(len(midi.tracks))),
                name="Conductor",
            )
            part.tracks.append(conductor)
            track = MidiTrack(events, name=name)
            part.tracks.append(track)
        else:
            part.format = 0
            track = MidiTrack(events, name=name)
            part.tracks.append(track)
        if part.format == 0:
            track.events.insert(0, MetaEvent.make_channel_prefix(0, channel))
        sort_track_if_unsorted(track)
        extraction = extract_notes(part, [len(part.tracks) - 1] if keep_conductor else [0])
        parts.append(
            SplitPart(
                index=len(parts),
                channel=channel,
                source_tracks=_tracks_using_channel(midi, channel),
                name=name,
                midi=part,
                notes=len(extraction.notes),
                events=len(events),
            )
        )
    return parts


def split_by_track(
    midi: MidiFile,
    tracks: Optional[Sequence[int]] = None,
    keep_conductor: bool = False,
    keep_markers: bool = True,
    keep_extra_chunks: bool = True,
    output_format: Optional[int] = None,
    min_events: int = 1,
) -> List[SplitPart]:
    """One standalone file per source track (a format 0 file explodes into parts)."""
    if tracks is None:
        indices = list(range(len(midi.tracks)))
    else:
        indices = []
        for value in tracks:
            index = int(value)
            if index < 0:
                index += len(midi.tracks)
            if not 0 <= index < len(midi.tracks):
                raise SchemaError("track index %d out of range (file has %d)" % (value, len(midi.tracks)))
            if index not in indices:
                indices.append(index)
    parts: List[SplitPart] = []
    for index in indices:
        source = midi.tracks[index]
        events = _track_events(source)
        if len(events) < max(1, int(min_events)):
            continue
        name = source.name or "Track %d" % (index + 1)
        part = MidiFile(
            format=int(output_format) if output_format is not None else (1 if keep_conductor else 0),
            division=midi.division,
            extra_chunks=list(midi.extra_chunks) if keep_extra_chunks else [],
        )
        part.header_extra = midi.header_extra
        if keep_conductor:
            others = [i for i in range(len(midi.tracks)) if i != index]
            part.tracks.append(MidiTrack(_conductor_events(midi, keep_markers, others), name="Conductor"))
        track = MidiTrack(events, name=None)
        if not any(ev.is_meta and ev.type_byte == 0x03 for ev in events):
            track.set_name(name)
        sort_track_if_unsorted(track)
        part.tracks.append(track)
        channels = sorted({ev.channel for ev in events if ev.is_channel})
        if len(channels) == 1 and part.format == 0:
            track.events.insert(0, MetaEvent.make_channel_prefix(0, channels[0]))
            sort_track_if_unsorted(track)
        extraction = extract_notes(part, [len(part.tracks) - 1])
        parts.append(
            SplitPart(
                index=len(parts),
                channel=channels[0] if len(channels) == 1 else None,
                source_tracks=[index],
                name=name,
                midi=part,
                notes=len(extraction.notes),
                events=len(events),
            )
        )
    return parts


def split_midi(
    midi: MidiFile,
    by: str = "channel",
    channels: Optional[Sequence[int]] = None,
    tracks: Optional[Sequence[int]] = None,
    keep_conductor: Optional[bool] = None,
    keep_markers: bool = True,
    keep_extra_chunks: bool = True,
    output_format: Optional[int] = None,
    min_events: int = 1,
) -> List[SplitPart]:
    """Dispatch to split_by_channel ('channel') or split_by_track ('track')."""
    kind = str(by or "channel").strip().lower()
    if kind in ("channel", "channels", "ch"):
        return split_by_channel(
            midi,
            channels=channels,
            keep_conductor=True if keep_conductor is None else keep_conductor,
            keep_markers=keep_markers,
            keep_extra_chunks=keep_extra_chunks,
            output_format=output_format,
            min_events=min_events,
        )
    if kind in ("track", "tracks", "trk"):
        return split_by_track(
            midi,
            tracks=tracks if tracks is not None else None,
            keep_conductor=False if keep_conductor is None else keep_conductor,
            keep_markers=keep_markers,
            keep_extra_chunks=keep_extra_chunks,
            output_format=output_format,
            min_events=min_events,
        )
    raise SchemaError("split 'by' must be 'channel' or 'track': %r" % (by,))


def _validate_channel(value: Any) -> int:
    channel = int(value)
    if not 0 <= channel <= 15:
        raise SchemaError("channel must be 0-15: %r" % (value,))
    return channel


def _tracks_using_channel(midi: MidiFile, channel: int) -> List[int]:
    out = []
    for index, track in enumerate(midi.tracks):
        if any(ev.is_channel and ev.channel == channel for ev in track.events):
            out.append(index)
    return out


def render_name(template: str, part: SplitPart, stem: str) -> str:
    """Fill a file name template: {stem} {index} {index2} {channel} {channel1} {ch2} {name}."""
    text = str(template or DEFAULT_TEMPLATE)
    values = {
        "{stem}": sanitize_filename(stem, "part"),
        "{index}": str(part.index),
        "{index2}": "%02d" % part.index,
        "{channel}": "" if part.channel is None else str(part.channel),
        "{channel1}": "" if part.channel is None else str(part.channel + 1),
        "{ch2}": "%02d" % (0 if part.channel is None else part.channel + 1),
        "{name}": sanitize_filename(part.name),
    }
    for token, value in values.items():
        text = text.replace(token, value)
    if not text.lower().endswith((".mid", ".midi")):
        text += ".mid"
    return text


def write_parts(
    parts: Sequence[SplitPart],
    out_dir: str,
    template: str = DEFAULT_TEMPLATE,
    stem: str = "part",
    dry_run: bool = False,
    running_status: bool = False,
    note_off_as_note_on_0: bool = False,
) -> List[Dict[str, Any]]:
    """Write every part into out_dir; returns the report rows."""
    rows: List[Dict[str, Any]] = []
    used: Dict[str, int] = {}
    directory = os.path.abspath(out_dir)
    if not dry_run:
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as exc:
            raise WriteError("cannot create output directory %s: %s" % (out_dir, exc), path=out_dir)
    for part in parts:
        filename = render_name(template, part, stem)
        if filename in used:
            used[filename] += 1
            root, ext = os.path.splitext(filename)
            filename = "%s_%d%s" % (root, used[filename], ext)
        else:
            used[filename] = 1
        path = os.path.join(directory, filename)
        row = part.to_dict(include_bytes=False)
        row["path"] = path if not dry_run else None
        row["filename"] = filename
        if dry_run:
            row["bytes"] = len(part.midi.to_bytes())
        else:
            part.midi.write(path, running_status=running_status, note_off_as_note_on_0=note_off_as_note_on_0)
            part.path = path
            row["path"] = path
            row["bytes"] = os.path.getsize(path)
        rows.append(row)
    return rows
