"""Reports: summaries, statistics, piano roll rendering and file diffing."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .constants import GM_DRUM_NOTES, KEY_SIGNATURES, NOTE_NAMES, note_name, program_name
from .events import DecodeOptions, Event
from .notes import Note, extract_notes, max_polyphony
from .smf import MidiFile
from .tempo import TempoMap

Y_AXIS_LABELS = NOTE_NAMES


def format_clock(seconds: float) -> str:
    return TempoMap.format_seconds(seconds)


def summarize(
    midi: MidiFile,
    opts: Optional[DecodeOptions] = None,
    tempo_map: Optional[TempoMap] = None,
    path: Optional[str] = None,
    max_list: int = 32,
) -> Dict[str, Any]:
    """Full structural + musical summary of a file."""
    tm = tempo_map or TempoMap.from_midi(midi)
    opts = opts or DecodeOptions()
    extraction = extract_notes(midi)
    duration_ticks = midi.duration_ticks()
    duration_seconds = tm.tick_to_seconds(duration_ticks)
    pitches = [n.pitch for n in extraction.notes]
    velocities = [n.velocity for n in extraction.notes]
    tracks: List[Dict[str, Any]] = []
    for index, track in enumerate(midi.tracks):
        track_notes = [n for n in extraction.notes if n.track == index]
        counts = track.count_by_type()
        tracks.append(
            {
                "index": index,
                "name": track.name,
                "instrument_name": track.instrument_name(),
                "channels": track.channels(),
                "channels_display": [c + 1 for c in track.channels()],
                "programs": track.programs(),
                "program_names": [program_name(p) for p in track.programs()],
                "events": len(track.events),
                "event_counts": counts,
                "notes": len(track_notes),
                "duration_ticks": track.duration_ticks,
                "duration_seconds": round(tm.tick_to_seconds(track.duration_ticks), 6),
                "is_drum": 9 in track.channels(),
            }
        )
    tempo_changes = [
        {"tick": tick, "tempo": tempo, "bpm": round(60000000.0 / tempo, 4)}
        for tick, tempo in tm.tempos
        if tempo
    ]
    time_signatures = [
        {"tick": tick, "numerator": num, "denominator": den} for tick, num, den in tm.time_signatures
    ]
    key_signatures = [
        {
            "tick": event.tick,
            "sf": event.key_signature[0],
            "mi": event.key_signature[1],
            "key": KEY_SIGNATURES.get(event.key_signature[0], ("?", "?"))[1 if event.key_signature[1] else 0],
        }
        for _ti, _ei, event in midi.iter_events()
        if event.is_meta and event.type_byte == 0x59 and event.key_signature
    ]
    text_events = [
        {"track": ti, "tick": event.tick, "meta_type": event.name, "text": event.text}
        for ti, _ei, event in midi.iter_events()
        if event.is_meta and event.text is not None
    ]
    drum_notes = sorted(set(n.pitch for n in extraction.notes if n.channel == 9))
    return {
        "file": {"path": path or midi.source_name, "bytes": len(midi.to_bytes())},
        "format": midi.format,
        "format_description": {
            0: "single multi-channel track",
            1: "one or more simultaneous tracks",
            2: "one or more independent single-track patterns",
        }.get(midi.format, "unknown"),
        "division": midi.division.to_dict(),
        "ticks_per_beat": midi.division.ticks_per_beat,
        "track_count": len(midi.tracks),
        "declared_track_count": midi.declared_track_count,
        "duration_ticks": duration_ticks,
        "duration_seconds": round(duration_seconds, 6),
        "duration_clock": format_clock(duration_seconds),
        "duration_bars": tm.bar_beat_tick(duration_ticks),
        "notes": {
            "count": len(extraction.notes),
            "pitch_min": min(pitches) if pitches else None,
            "pitch_max": max(pitches) if pitches else None,
            "pitch_min_name": note_name(min(pitches)) if pitches else None,
            "pitch_max_name": note_name(max(pitches)) if pitches else None,
            "velocity_min": min(velocities) if velocities else None,
            "velocity_max": max(velocities) if velocities else None,
            "velocity_mean": round(sum(velocities) / len(velocities), 3) if velocities else None,
            "max_polyphony": max_polyphony(extraction.notes),
            "unterminated": len(extraction.unterminated_note_ons),
            "orphan_note_offs": len(extraction.orphan_note_offs),
            "pitch_histogram": _histogram(pitches, 128),
            "per_channel": _counts(n.channel for n in extraction.notes),
        },
        "channels_used": sorted(set(c for t in midi.tracks for c in t.channels())),
        "programs_used": sorted(set(p for t in midi.tracks for p in t.programs())),
        "tempo": {
            "initial_bpm": round(tm.initial_bpm, 4),
            "initial_tempo": tm.initial_tempo,
            "changes": tempo_changes[:max_list],
            "change_count": len(tempo_changes),
            "min_bpm": round(min(60000000.0 / t for _tk, t in tm.tempos if t), 4) if tempo_changes else None,
            "max_bpm": round(max(60000000.0 / t for _tk, t in tm.tempos if t), 4) if tempo_changes else None,
        },
        "time_signatures": time_signatures[:max_list],
        "key_signatures": key_signatures[:max_list],
        "markers": [
            {"tick": item["tick"], "text": item["text"]}
            for item in text_events
            if item["meta_type"] == "marker"
        ][:max_list],
        "text_events": text_events[:max_list],
        "drum_notes": [{"note": p, "name": GM_DRUM_NOTES.get(p)} for p in drum_notes],
        "tracks": tracks,
        "extra_chunks": [
            {"type": ctype.decode("latin-1", "replace"), "length": len(payload)}
            for ctype, payload in midi.extra_chunks
        ],
        "warnings": midi.warnings,
    }


def _histogram(values: Iterable[int], size: int) -> Dict[str, int]:
    counts = [0] * size
    for value in values:
        if 0 <= value < size:
            counts[value] += 1
    return {str(index): count for index, count in enumerate(counts) if count}


def _counts(values: Iterable[int]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for value in values:
        key = str(value)
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items(), key=lambda item: int(item[0])))


def piano_roll(
    midi: MidiFile,
    start: int = 0,
    end: Optional[int] = None,
    width: int = 96,
    height: int = 24,
    tracks: Optional[Sequence[int]] = None,
    tempo_map: Optional[TempoMap] = None,
    grid_ticks: Optional[float] = None,
    show_velocity: bool = False,
    pitch_min: Optional[int] = None,
    pitch_max: Optional[int] = None,
) -> str:
    """Render a text piano roll so an agent can literally look at the music."""
    tm = tempo_map or TempoMap.from_midi(midi)
    extraction = extract_notes(midi, tracks)
    notes = extraction.notes
    if end is None:
        end = midi.duration_ticks() or 1
    if end <= start:
        end = start + 1
    visible = [n for n in notes if n.end > start and n.start < end]
    if not visible:
        return "(no notes between tick %d and %d)" % (start, end)
    if pitch_min is None:
        pitch_min = min(n.pitch for n in visible)
    if pitch_max is None:
        pitch_max = max(n.pitch for n in visible)
    full_range = (pitch_min, pitch_max)
    pitch_min = max(0, pitch_min - 1)
    pitch_max = min(127, pitch_max + 1)
    clipped = False
    if pitch_max - pitch_min + 1 > height:
        clipped = True
        # pick the window that carries the most musical weight: sustained pitched
        # notes count more than short percussive hits, and a drum-only range is
        # used only when there is nothing melodic to show
        pitched = [n for n in visible if n.channel != 9]
        weighted = pitched or visible
        counts = [0.0] * 128
        for note in weighted:
            counts[note.pitch] += 0.5 + min(note.duration, 960) / 960.0
        lowest = max(0, pitch_min)
        highest = min(128 - height, pitch_max - height + 1)
        best_start = lowest
        best_score = -1
        centre = (pitch_min + pitch_max - height) // 2
        for candidate in range(lowest, max(lowest, highest) + 1):
            score = sum(counts[candidate : candidate + height])
            # busiest window wins; ties prefer the window nearest the centre
            if score > best_score or (score == best_score and abs(candidate - centre) < abs(best_start - centre)):
                best_score = score
                best_start = candidate
        pitch_min = best_start
        pitch_max = best_start + height - 1
    rows = list(range(pitch_max, pitch_min - 1, -1))
    columns = max(8, int(width))
    span = float(end - start)
    scale = columns / span
    grid = [["." for _ in range(columns)] for _ in rows]

    # background grid: bar lines and beats
    if grid_ticks is None:
        numerator, denominator = tm.time_signature_at(start)
        beat = 4.0 * tm.division.ticks_per_beat / float(denominator)
        grid_ticks = beat
    tick = start - (start % grid_ticks) if grid_ticks else None
    while tick is not None and tick <= end:
        column = int((tick - start) * scale)
        if 0 <= column < columns:
            is_bar = False
            try:
                pos = tm.bar_beat_tick(int(tick))
                is_bar = pos["beat"] == 1 and pos["tick_in_beat"] == 0
            except Exception:
                is_bar = False
            for row in range(len(rows)):
                if grid[row][column] == ".":
                    grid[row][column] = "|" if is_bar else ":"
        tick += grid_ticks
        if grid_ticks <= 0:
            break

    for note in notes:
        if note.end <= start or note.start >= end:
            continue
        row = pitch_max - note.pitch
        if not 0 <= row < len(rows):
            continue
        first = int((max(note.start, start) - start) * scale)
        last = int((min(note.end, end) - start) * scale)
        if last <= first:
            last = first + 1
        for column in range(first, min(last, columns)):
            grid[row][column] = "#" if column == first else "="
        if show_velocity and 0 <= first < columns and grid[row][first] == "#":
            grid[row][first] = "0123456789ABCDEF"[min(15, note.velocity // 8)]
    header = "tick %d..%d  (%d columns, %.1f ticks/column)" % (start, end, columns, span / columns)
    pitch_label = "pitches %d-%d" % (pitch_min, pitch_max)
    if clipped:
        pitch_label += " (of %d-%d, use --height/--pitch-min/--pitch-max to zoom)" % full_range
    header += "  " + pitch_label
    lines = [header]
    label_width = 7
    ruler = [" "] * columns
    tick = start - (start % (grid_ticks or 1))
    while tick <= end:
        column = int((tick - start) * scale)
        if 0 <= column < columns:
            text = str(int(tick))
            for offset, ch in enumerate(text):
                if column + offset < columns:
                    ruler[column + offset] = ch
        tick += (grid_ticks or 1) * 4
    lines.append(" " * label_width + "".join(ruler))
    for row_index, pitch in enumerate(rows):
        label = ("%s%-3d" % (Y_AXIS_LABELS[pitch % 12], pitch)).ljust(label_width)
        lines.append(label + "".join(grid[row_index]))
    return "\n".join(lines)


def diff_files(
    a: MidiFile,
    b: MidiFile,
    name_a: str = "a",
    name_b: str = "b",
    max_items: int = 50,
) -> Dict[str, Any]:
    """Semantic diff between two MIDI files (not a byte diff)."""
    result: Dict[str, Any] = {
        "identical_bytes": a.to_bytes() == b.to_bytes(),
        "header": {},
        "tracks": {},
        "events": {},
        "notes": {},
    }
    if a.format != b.format:
        result["header"]["format"] = {"a": a.format, "b": b.format}
    if a.division != b.division:
        result["header"]["division"] = {"a": a.division.to_dict(), "b": b.division.to_dict()}
    if len(a.tracks) != len(b.tracks):
        result["header"]["track_count"] = {"a": len(a.tracks), "b": len(b.tracks)}
    result["tracks"]["names"] = {
        "a": [t.name for t in a.tracks],
        "b": [t.name for t in b.tracks],
    }
    result["tracks"]["event_counts"] = {
        "a": [len(t.events) for t in a.tracks],
        "b": [len(t.events) for t in b.tracks],
    }

    added: List[Dict[str, Any]] = []
    removed: List[Dict[str, Any]] = []
    for index in range(max(len(a.tracks), len(b.tracks))):
        left = a.tracks[index].events if index < len(a.tracks) else []
        right = b.tracks[index].events if index < len(b.tracks) else []
        counts_left: Dict[Tuple, int] = {}
        counts_right: Dict[Tuple, int] = {}
        for event in left:
            key = event.fingerprint(include_tick=True)
            counts_left[key] = counts_left.get(key, 0) + 1
        for event in right:
            key = event.fingerprint(include_tick=True)
            counts_right[key] = counts_right.get(key, 0) + 1
        for key, count in counts_left.items():
            delta = count - counts_right.get(key, 0)
            if delta > 0:
                removed.append({"track": index, "count": delta, "event": _fingerprint_summary(key)})
        for key, count in counts_right.items():
            delta = count - counts_left.get(key, 0)
            if delta > 0:
                added.append({"track": index, "count": delta, "event": _fingerprint_summary(key)})
    result["events"] = {
        "removed_count": sum(item["count"] for item in removed),
        "added_count": sum(item["count"] for item in added),
        "removed": removed[:max_items],
        "added": added[:max_items],
    }

    notes_a = set(_note_key(n) for n in extract_notes(a).notes)
    notes_b = set(_note_key(n) for n in extract_notes(b).notes)
    notes_added = sorted(notes_b - notes_a)
    notes_removed = sorted(notes_a - notes_b)
    result["notes"] = {
        "a": len(notes_a),
        "b": len(notes_b),
        "added_count": len(notes_added),
        "removed_count": len(notes_removed),
        "added": [_note_summary(key) for key in notes_added[:max_items]],
        "removed": [_note_summary(key) for key in notes_removed[:max_items]],
    }
    result["summary"] = {
        "same": (
            result["events"]["added_count"] == 0
            and result["events"]["removed_count"] == 0
            and not result["header"]
        ),
        "files": {name_a: a.source_name, name_b: b.source_name},
    }
    return result


def _note_key(note: Note) -> Tuple:
    return (note.track, note.channel, note.pitch, note.start, note.end, note.velocity)


def _note_summary(key: Tuple) -> Dict[str, Any]:
    track, channel, pitch, start, end, velocity = key
    return {
        "track": track,
        "channel": channel,
        "note": pitch,
        "pitch": note_name(pitch),
        "start": start,
        "end": end,
        "duration": end - start,
        "velocity": velocity,
    }


def _fingerprint_summary(key: Tuple) -> Dict[str, Any]:
    tick = key[0]
    body = key[1:]
    out: Dict[str, Any] = {"tick": tick}
    if body and body[0] == "channel":
        out["kind"] = "channel"
        out["type"] = body[1]
        out["channel"] = body[2]
        out["data"] = list(body[3:])
        if out["type"] in ("note_on", "note_off"):
            out["note"] = body[3]
            out["pitch"] = note_name(body[3])
            out["velocity"] = body[4]
    elif body and body[0] == "meta":
        out["kind"] = "meta"
        out["meta_type"] = body[1]
        out["data_hex"] = bytes(body[2]).hex()
    elif body and body[0] == "sysex":
        out["kind"] = "sysex"
        out["escape"] = body[1]
        out["data_hex"] = bytes(body[2]).hex()
    else:
        out["kind"] = "system"
        out["raw"] = list(body)
    return out


def event_rows(
    midi: MidiFile,
    opts: Optional[DecodeOptions] = None,
    tempo_map: Optional[TempoMap] = None,
    tracks: Optional[Sequence[int]] = None,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Flat event listing with track/index addressing, used by 'dump'."""
    tm = tempo_map or TempoMap.from_midi(midi)
    opts = opts or DecodeOptions()
    allowed = set(tracks) if tracks is not None else None
    rows: List[Dict[str, Any]] = []
    for track_index, track in enumerate(midi.tracks):
        if allowed is not None and track_index not in allowed:
            continue
        for event_index, event in enumerate(track.events):
            row = event.to_dict(opts)
            row["track"] = track_index
            row["index"] = event_index
            row["id"] = "%d/%d" % (track_index, event_index)
            pos = tm.bar_beat_tick(event.tick)
            row["bar"] = pos["bar"]
            row["beat"] = pos["beat"]
            row["seconds"] = round(tm.tick_to_seconds(event.tick), 6)
            rows.append(row)
            if limit is not None and len(rows) >= limit:
                return rows
    return rows
