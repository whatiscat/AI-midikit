"""Lossless JSON interchange for MIDI files.

The document format is the agent-facing representation of a whole file:

    {
      "format": 1,
      "division": {"type": "ppq", "ticks_per_beat": 480},
      "tracks": [
        {"name": "Piano", "events": [ ... ]},
        {"name": "Bass",  "notes":  [ {"pitch": "C2", "start": 0, "duration": 960} ]}
      ]
    }

A track carries raw 'events' (lossless) and/or musical 'notes' (a compact
view); both are merged on load.  The informational 'notes_view' key written
by mode 'both' is ignored, so such a document reloads once and only once.

Round-tripping a file through to_document / from_document preserves every
byte of event payload.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Sequence

#: bare object keys, e.g. {track:1} - PowerShell 5.1 strips quotes from native args
_BARE_KEY_RE = re.compile(r"([{,]s*)([A-Za-z_][A-Za-z0-9_\-]*)(s*:)")


def loads_relaxed(text: str) -> Any:
    """json.loads with a forgiving fallback for hand written / shell mangled input."""
    try:
        return json.loads(text)
    except ValueError as first_error:
        candidate = _BARE_KEY_RE.sub(r'\1"\2"\3', text)
        if "'" in candidate:
            candidate = candidate.replace("'", '"')
        try:
            return json.loads(candidate)
        except ValueError:
            raise first_error

from .bytestream import bytes_from_hex, hex_bytes
from .errors import SchemaError
from .events import DEFAULT_OPTS, DecodeOptions, Event, MetaEvent, event_from_dict
from .notes import Note, note_from_dict, notes_to_events
from .smf import Division, MidiFile, MidiTrack, sort_track_if_unsorted


def to_document(
    midi: MidiFile,
    opts: Optional[DecodeOptions] = None,
    mode: str = "events",
    include_summary: bool = False,
    include_notes: bool = False,
    tempo_map=None,
) -> Dict[str, Any]:
    """Serialise a MidiFile to a JSON-friendly document.

    mode: 'events' (lossless), 'notes' (musical view) or 'both'.
    """
    o = opts or DEFAULT_OPTS
    doc: Dict[str, Any] = {
        "format": midi.format,
        "division": midi.division.to_dict(),
        "tracks": [],
    }
    if midi.header_extra:
        doc["header_extra_hex"] = hex_bytes(midi.header_extra)
    if midi.extra_chunks:
        doc["extra_chunks"] = [
            {"type": ctype.decode("latin-1", "replace"), "data_hex": hex_bytes(payload)}
            for ctype, payload in midi.extra_chunks
        ]
    want_events = mode in ("events", "both")
    want_notes = mode == "notes"
    want_view = mode == "both" or include_notes
    for index, track in enumerate(midi.tracks):
        entry: Dict[str, Any] = {"index": index, "name": track.name, "events_count": len(track.events)}
        if want_events:
            entry["events"] = [event.to_dict(o) for event in track.events]
        if want_notes or want_view:
            from .notes import extract_notes

            extraction = extract_notes(midi, [index])
            listing = [note.to_dict(o, tempo_map=tempo_map) for note in extraction.sorted()]
            if want_notes:
                entry["notes"] = listing
            else:
                entry["notes_view"] = listing
        doc["tracks"].append(entry)
    if include_summary:
        from .analysis import summarize

        doc["summary"] = summarize(midi, o, tempo_map)
    return doc


def from_document(doc: Dict[str, Any], encoding: str = "utf-8") -> MidiFile:
    """Build a MidiFile from a document produced by to_document (or by hand)."""
    if not isinstance(doc, dict):
        raise SchemaError("MIDI document must be an object")
    if "midi" in doc and isinstance(doc["midi"], dict):
        doc = doc["midi"]
    fmt = int(doc.get("format", 1))
    division = Division.from_dict(doc.get("division", doc.get("ppq", 480)))
    midi = MidiFile(format=fmt, division=division)
    header_extra = doc.get("header_extra_hex") or doc.get("header_extra")
    if header_extra:
        midi.header_extra = bytes_from_hex(str(header_extra)) if isinstance(header_extra, str) else bytes(header_extra)
    for spec in doc.get("extra_chunks", []) or []:
        ctype = spec.get("type", "")
        payload = spec.get("data_hex", spec.get("data", ""))
        data = bytes_from_hex(payload) if isinstance(payload, str) else bytes(bytearray(payload))
        chunk_type = ctype.encode("latin-1") if isinstance(ctype, str) else bytes(ctype)
        if len(chunk_type) != 4:
            raise SchemaError("extra chunk type must be 4 bytes: %r" % (ctype,))
        midi.extra_chunks.append((chunk_type, data))
    tracks = doc.get("tracks")
    if tracks is None:
        raise SchemaError("document needs a 'tracks' array")
    if not isinstance(tracks, (list, tuple)):
        raise SchemaError("'tracks' must be an array")
    for index, spec in enumerate(tracks):
        if isinstance(spec, (list, tuple)):
            spec = {"events": list(spec)}
        if not isinstance(spec, dict):
            raise SchemaError("track #%d must be an object or an array of events" % index)
        track = MidiTrack()
        events: List[Event] = []
        for raw in spec.get("events", []) or []:
            events.append(event_from_dict(raw))
        notes_spec = spec.get("notes")
        if notes_spec:
            notes: List[Note] = []
            for item in notes_spec:
                note = note_from_dict(item) if isinstance(item, dict) else None
                if note is None:
                    raise SchemaError("track #%d note must be an object" % index)
                note.track = index
                notes.append(note)
            events.extend(notes_to_events(notes))
        track.events = events
        if spec.get("name") and not any(ev.is_meta and ev.type_byte == 0x03 for ev in events):
            track.set_name(str(spec["name"]), encoding)
        sort_track_if_unsorted(track)
        midi.tracks.append(track)
    return midi
