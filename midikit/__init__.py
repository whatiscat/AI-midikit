"""midikit - a precise Standard MIDI File 1.1 toolkit built for AI agents.

Quick start::

    from midikit import MidiFile, extract_notes, TempoMap, apply_plan

    midi = MidiFile.read("song.mid")
    notes = extract_notes(midi).notes
    apply_plan(midi, [{"op": "transpose", "semitones": 2}])
    midi.write("song_up2.mid")

Everything the library does is also reachable from the command line
(python -m midikit ...), which speaks JSON in and JSON out.
"""

from __future__ import annotations

__version__ = "1.0.0"

from .bytestream import ByteReader, ByteWriter, hex_bytes, vlq_bytes
from .errors import (
    MidiKitError,
    NotFoundError,
    OpError,
    ParseError,
    SchemaError,
    UsageError,
    ValidationFailed,
    WriteError,
)
from .events import (
    ChannelEvent,
    DecodeOptions,
    Event,
    MetaEvent,
    SysExEvent,
    SystemEvent,
    event_from_dict,
)
from .jsonio import from_document, to_document
from .notes import Note, NoteExtraction, extract_notes, notes_to_events
from .plan import apply_plan, describe_plan, normalize_plan
from .selectors import EventSelector, NoteSelector, resolve_track_indices
from .smf import Division, MidiFile, MidiTrack, read_midi
from .split import (
    SplitPart,
    sanitize_filename,
    split_by_channel,
    split_by_track,
    split_midi,
    write_parts,
)
from .tempo import TempoMap, parse_time
from .validate import validate

__all__ = [
    "__version__",
    "MidiFile",
    "MidiTrack",
    "Division",
    "read_midi",
    "Event",
    "ChannelEvent",
    "MetaEvent",
    "SysExEvent",
    "SystemEvent",
    "event_from_dict",
    "DecodeOptions",
    "Note",
    "NoteExtraction",
    "extract_notes",
    "notes_to_events",
    "TempoMap",
    "parse_time",
    "EventSelector",
    "NoteSelector",
    "resolve_track_indices",
    "apply_plan",
    "describe_plan",
    "normalize_plan",
    "to_document",
    "from_document",
    "SplitPart",
    "split_midi",
    "split_by_channel",
    "split_by_track",
    "write_parts",
    "sanitize_filename",
    "validate",
    "ByteReader",
    "ByteWriter",
    "vlq_bytes",
    "hex_bytes",
    "MidiKitError",
    "ParseError",
    "SchemaError",
    "OpError",
    "UsageError",
    "NotFoundError",
    "ValidationFailed",
    "WriteError",
]
