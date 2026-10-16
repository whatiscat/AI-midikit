"""Declarative selectors so an agent can address events and notes precisely.

An event selector is a JSON object; every field is optional and an empty /
missing selector matches everything:

    {"track": 1, "type": "note_on", "channel": 0, "tick_min": 0, "tick_max": 1920,
     "note_min": 60, "note_max": 72, "velocity_min": 1, "limit": 8}

A note selector additionally understands start / end / duration ranges:

    {"track": 0, "pitch": 60, "start_min": 480, "duration_max": 240}
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .constants import META_NAME_TO_TYPE, TYPE_ALIASES, note_name, note_number
from .errors import SchemaError
from .events import Event, canonical_type, meta_type_number
from .notes import Note, NoteExtraction
from .smf import MidiFile

EVENT_TYPES = (
    "note_on",
    "note_off",
    "poly_aftertouch",
    "control_change",
    "program_change",
    "channel_aftertouch",
    "pitch_bend",
    "meta",
    "sysex",
    "escape",
    "system",
)

TYPE_GROUPS: Dict[str, Tuple[str, ...]] = {
    "note": ("note_on", "note_off"),
    "notes": ("note_on", "note_off"),
    "all_notes": ("note_on", "note_off"),
    "channel": (
        "note_on",
        "note_off",
        "poly_aftertouch",
        "control_change",
        "program_change",
        "channel_aftertouch",
        "pitch_bend",
    ),
    "channel_events": (
        "note_on",
        "note_off",
        "poly_aftertouch",
        "control_change",
        "program_change",
        "channel_aftertouch",
        "pitch_bend",
    ),
    "cc": ("control_change",),
    "controller": ("control_change",),
    "sysex": ("sysex", "escape"),
    "exclusive": ("sysex", "escape"),
    "tempo": ("meta",),
    "text": ("meta",),
}

EVENT_SELECTOR_KEYS = (
    "track",
    "tracks",
    "index",
    "indexes",
    "indices",
    "index_min",
    "index_max",
    "type",
    "types",
    "channel",
    "channels",
    "tick_min",
    "tick_max",
    "at",
    "from",
    "to",
    "start",
    "end",
    "note_min",
    "note_max",
    "pitch",
    "pitch_min",
    "pitch_max",
    "velocity_min",
    "velocity_max",
    "velocity",
    "control",
    "cc",
    "program",
    "value",
    "value_min",
    "value_max",
    "meta_type",
    "meta_types",
    "text",
    "text_contains",
    "text_regex",
    "status",
    "limit",
    "which",
    "not_type",
    "empty",
)

NOTE_SELECTOR_KEYS = (
    "track",
    "tracks",
    "channel",
    "channels",
    "pitch",
    "note",
    "note_min",
    "note_max",
    "pitch_min",
    "pitch_max",
    "velocity_min",
    "velocity_max",
    "start_min",
    "start_max",
    "end_min",
    "end_max",
    "duration_min",
    "duration_max",
    "from",
    "to",
    "start",
    "end",
    "index",
    "limit",
    "which",
    "unterminated",
    "empty",
)


def _as_int_list(value: Any, label: str) -> Optional[List[int]]:
    if value is None:
        return None
    if isinstance(value, bool):
        raise SchemaError("%s must be an integer or list of integers" % label)
    if isinstance(value, int):
        return [value]
    if isinstance(value, str):
        parts = [p for p in re.split(r"[,\s]+", value.strip()) if p]
        return [int(p) for p in parts]
    if isinstance(value, (list, tuple, set)):
        out: List[int] = []
        for item in value:
            out.extend(_as_int_list(item, label) or [])
        return out
    raise SchemaError("%s must be an integer or list of integers: %r" % (label, value))


def _as_str_list(value: Any, label: str) -> Optional[List[str]]:
    if value is None:
        return None
    if isinstance(value, str):
        parts = [p for p in re.split(r"[,\s]+", value.strip()) if p]
        return parts
    if isinstance(value, (list, tuple, set)):
        out: List[str] = []
        for item in value:
            out.extend(_as_str_list(item, label) or [])
        return out
    raise SchemaError("%s must be a string or list of strings: %r" % (label, value))


def _pitch_value(value: Any) -> int:
    if isinstance(value, str) and not re.match(r"^\s*[A-Ga-g#b\u266f\u266d]+\s*-?\d+\s*$", value) and " " in value:
        raise SchemaError("bad pitch: %r" % (value,))
    return note_number(value)


class _Range:
    __slots__ = ("min", "max")

    def __init__(self, minimum: Optional[float] = None, maximum: Optional[float] = None) -> None:
        self.min = minimum
        self.max = maximum

    @property
    def active(self) -> bool:
        return self.min is not None or self.max is not None

    def match(self, value: float) -> bool:
        if self.min is not None and value < self.min:
            return False
        if self.max is not None and value > self.max:
            return False
        return True

    def describe(self, label: str) -> str:
        if self.min is not None and self.max is not None:
            return "%s %g..%g" % (label, self.min, self.max)
        if self.min is not None:
            return "%s >= %g" % (label, self.min)
        return "%s <= %g" % (label, self.max)


class EventSelector:
    """Matcher over events, addressable by track, index, type, tick or payload."""

    def __init__(self, spec: Any = None) -> None:
        if spec is None or spec == {} or spec == "":
            spec = {}
        if isinstance(spec, (str, list, tuple)):
            spec = {"type": spec} if isinstance(spec, (list, tuple)) else parse_selector_arg(spec)
        if not isinstance(spec, dict):
            raise SchemaError("selector must be an object: %r" % (spec,))
        unknown = [k for k in spec if k not in EVENT_SELECTOR_KEYS]
        if unknown:
            raise SchemaError(
                "unknown selector field(s): %s" % ", ".join(sorted(unknown)),
                allowed=list(EVENT_SELECTOR_KEYS),
            )
        self.raw = dict(spec)
        self.tracks = _as_int_list(spec.get("track", spec.get("tracks")), "track")
        self.indexes = _as_int_list(spec.get("index", spec.get("indexes", spec.get("indices"))), "index")
        self.index_range = _Range(_opt_float(spec.get("index_min")), _opt_float(spec.get("index_max")))
        self.channels = _as_int_list(spec.get("channel", spec.get("channels")), "channel")
        self.tick_range = _Range(
            _opt_float(_first(spec, "tick_min", "from", "start")),
            _opt_float(_first(spec, "tick_max", "to", "end")),
        )
        if "at" in spec and spec["at"] is not None:
            value = _opt_float(spec["at"])
            self.tick_range = _Range(value, value)
        self.note_range = _Range(
            _opt_float(_first(spec, "note_min", "pitch_min")),
            _opt_float(_first(spec, "note_max", "pitch_max")),
        )
        if spec.get("pitch") is not None:
            if isinstance(spec["pitch"], (list, tuple)):
                pitches = [_pitch_value(p) for p in spec["pitch"]]
                self.note_range = _Range(min(pitches), max(pitches))
            else:
                value = _pitch_value(spec["pitch"])
                self.note_range = _Range(value, value)
        self.velocity_range = _Range(_opt_float(spec.get("velocity_min")), _opt_float(spec.get("velocity_max")))
        self.velocity = _opt_float(spec.get("velocity"))
        controls = spec.get("control", spec.get("cc"))
        self.controls = None if controls is None else [_control_number(v) for v in _as_token_list(controls)]
        programs = spec.get("program")
        self.programs = None if programs is None else [_program_number(v) for v in _as_token_list(programs)]
        self.values = _as_int_list(spec.get("value"), "value")
        self.value_range = _Range(_opt_float(spec.get("value_min")), _opt_float(spec.get("value_max")))
        self.meta_types = self._parse_meta_types(spec.get("meta_type", spec.get("meta_types")))
        self.text_contains = spec.get("text_contains", spec.get("text"))
        self.text_regex = spec.get("text_regex")
        if self.text_regex is not None:
            self._regex = re.compile(str(self.text_regex))
        else:
            self._regex = None
        self.statuses = _as_int_list(spec.get("status"), "status")
        self.limit = int(spec["limit"]) if spec.get("limit") is not None else None
        self.which = spec.get("which")
        self.exclude_types = self._parse_types(spec.get("not_type"))
        self.types, self.text_only = self._parse_types(spec.get("type", spec.get("types")))

    # -- parsing helpers ------------------------------------------------
    @staticmethod
    def _parse_types(value: Any):
        if value is None:
            return None, False
        text_only = False
        out: Set[str] = set()
        items = value if isinstance(value, (list, tuple, set)) else [value]
        for item in items:
            if isinstance(item, str):
                key = item.strip().lower()
                if key in TYPE_GROUPS:
                    if key == "text":
                        text_only = True
                    out.update(TYPE_GROUPS[key])
                    continue
                out.add(canonical_type(key))
            else:
                out.add(canonical_type(item))
        return (out or None), text_only

    @staticmethod
    def _parse_meta_types(value: Any) -> Optional[Set[int]]:
        if value is None:
            return None
        items = value if isinstance(value, (list, tuple, set)) else [value]
        out: Set[int] = set()
        for item in items:
            out.add(meta_type_number(item))
        return out

    # -- matching --------------------------------------------------------
    def matches(self, event: Event, track_index: int, event_index: int) -> bool:
        if self.tracks is not None and track_index not in self.tracks:
            return False
        if self.indexes is not None and event_index not in self.indexes:
            return False
        if self.index_range.active and not self.index_range.match(event_index):
            return False
        if self.types is not None:
            etype = event.kind if event.kind != "channel" else event.type
            if event.is_meta:
                etype = "meta"
            if etype not in self.types:
                return False
        if self.exclude_types is not None and event.kind == "channel" and event.type in self.exclude_types:
            return False
        if self.text_only and not (event.is_meta and event.type_byte in (0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09)):
            return False
        if self.tick_range.active and not self.tick_range.match(event.tick):
            return False
        if self.channels is not None:
            if not event.is_channel or event.channel not in self.channels:
                return False
        if self.statuses is not None and getattr(event, "status", None) not in self.statuses:
            return False
        if self.note_range.active:
            if not event.is_channel or event.type not in ("note_on", "note_off", "poly_aftertouch"):
                return False
            if not self.note_range.match(event.value1):
                return False
        if self.velocity_range.active or self.velocity is not None:
            if not event.is_channel or event.type not in ("note_on", "note_off"):
                return False
            velocity = event.value2 or 0
            if self.velocity_range.active and not self.velocity_range.match(velocity):
                return False
            if self.velocity is not None and velocity != self.velocity:
                return False
        if self.controls is not None:
            if not (event.is_channel and event.type == "control_change" and event.value1 in self.controls):
                return False
        if self.programs is not None:
            if not (event.is_channel and event.type == "program_change" and event.value1 in self.programs):
                return False
        if self.values is not None or self.value_range.active:
            value = _data_value(event)
            if value is None:
                return False
            if self.values is not None and value not in self.values:
                return False
            if self.value_range.active and not self.value_range.match(value):
                return False
        if self.meta_types is not None:
            if not event.is_meta or event.type_byte not in self.meta_types:
                return False
        if self.text_contains is not None:
            if not event.is_meta or event.text is None:
                return False
            if str(self.text_contains).lower() not in event.text.lower():
                return False
        if self._regex is not None:
            if not event.is_meta or event.text is None or not self._regex.search(event.text):
                return False
        return True

    @property
    def is_empty(self) -> bool:
        return not self.raw

    def apply_limits(self, items: List[Any]) -> List[Any]:
        if self.which == "first":
            count = self.limit or 1
            return items[:count]
        if self.which == "last":
            count = self.limit or 1
            return items[-count:]
        if self.limit is not None:
            return items[: self.limit]
        return items

    def describe(self) -> str:
        parts: List[str] = []
        if self.tracks is not None:
            parts.append("tracks=%s" % ",".join(str(t) for t in self.tracks))
        else:
            parts.append("tracks=all")
        if self.types is not None:
            parts.append("type=%s" % "|".join(sorted(self.types)))
        if self.indexes is not None:
            parts.append("index=%s" % ",".join(str(i) for i in self.indexes))
        if self.channels is not None:
            parts.append("channel=%s" % ",".join(str(c) for c in self.channels))
        if self.tick_range.active:
            parts.append(self.tick_range.describe("tick"))
        if self.note_range.active:
            parts.append(self.note_range.describe("note"))
        if self.velocity_range.active:
            parts.append(self.velocity_range.describe("velocity"))
        if self.controls is not None:
            parts.append("control=%s" % ",".join(str(c) for c in self.controls))
        if self.programs is not None:
            parts.append("program=%s" % ",".join(str(p) for p in self.programs))
        if self.values is not None:
            parts.append("value=%s" % ",".join(str(v) for v in self.values))
        if self.value_range.active:
            parts.append(self.value_range.describe("value"))
        if self.velocity is not None:
            parts.append("velocity=%g" % self.velocity)
        if self.meta_types is not None:
            parts.append("meta=%s" % ",".join(str(m) for m in sorted(self.meta_types)))
        if self.text_contains is not None:
            parts.append("text~%r" % (self.text_contains,))
        if self.limit is not None:
            parts.append("limit=%d" % self.limit)
        if self.which:
            parts.append("which=%s" % self.which)
        return " ".join(parts)

    def filter(self, midi: MidiFile, track_indices: Optional[Iterable[int]] = None) -> List[Tuple[int, int, Event]]:
        allowed = set(track_indices) if track_indices is not None else None
        out: List[Tuple[int, int, Event]] = []
        for track_index, track in enumerate(midi.tracks):
            if allowed is not None and track_index not in allowed:
                continue
            for event_index, event in enumerate(track.events):
                if self.matches(event, track_index, event_index):
                    out.append((track_index, event_index, event))
        return self.apply_limits(out)


class NoteSelector:
    """Matcher over extracted notes."""

    def __init__(self, spec: Any = None) -> None:
        if spec is None or spec == {} or spec == "":
            spec = {}
        if isinstance(spec, bool):
            raise SchemaError("note selector must be an object: %r" % (spec,))
        if isinstance(spec, int):
            spec = {"pitch": spec}
        elif isinstance(spec, str):
            spec = parse_selector_arg(spec)
            if not spec:
                raise SchemaError("note selector cannot be an empty string")
        if not isinstance(spec, dict):
            raise SchemaError("note selector must be an object: %r" % (spec,))
        unknown = [k for k in spec if k not in NOTE_SELECTOR_KEYS]
        if unknown:
            raise SchemaError(
                "unknown note selector field(s): %s" % ", ".join(sorted(unknown)),
                allowed=list(NOTE_SELECTOR_KEYS),
            )
        self.raw = dict(spec)
        self.tracks = _as_int_list(spec.get("track", spec.get("tracks")), "track")
        self.channels = _as_int_list(spec.get("channel", spec.get("channels")), "channel")
        self.note_range = _Range(
            _opt_float(_first(spec, "note_min", "pitch_min")),
            _opt_float(_first(spec, "note_max", "pitch_max")),
        )
        single = _first(spec, "pitch", "note")
        if single is not None:
            if isinstance(single, (list, tuple)):
                pitches = [_pitch_value(p) for p in single]
                self.note_range = _Range(min(pitches), max(pitches))
            else:
                value = _pitch_value(single)
                self.note_range = _Range(value, value)
        self.velocity_range = _Range(_opt_float(spec.get("velocity_min")), _opt_float(spec.get("velocity_max")))
        self.start_range = _Range(
            _opt_float(_first(spec, "start_min", "from", "start")),
            _opt_float(_first(spec, "start_max", "to")),
        )
        self.end_range = _Range(_opt_float(spec.get("end_min")), _opt_float(spec.get("end_max", spec.get("end"))))
        self.duration_range = _Range(_opt_float(spec.get("duration_min")), _opt_float(spec.get("duration_max")))
        self.indexes = _as_int_list(spec.get("index"), "index")
        self.limit = int(spec["limit"]) if spec.get("limit") is not None else None
        self.which = spec.get("which")
        self.unterminated = spec.get("unterminated")

    def matches(self, note: Note, index: Optional[int] = None) -> bool:
        if self.tracks is not None and note.track not in self.tracks:
            return False
        if self.channels is not None and note.channel not in self.channels:
            return False
        if self.indexes is not None and index is not None and index not in self.indexes:
            return False
        if self.note_range.active and not self.note_range.match(note.pitch):
            return False
        if self.velocity_range.active and not self.velocity_range.match(note.velocity):
            return False
        if self.start_range.active and not self.start_range.match(note.start):
            return False
        if self.end_range.active and not self.end_range.match(note.end):
            return False
        if self.duration_range.active and not self.duration_range.match(note.duration):
            return False
        if self.unterminated is not None and bool(self.unterminated) != bool(note.unterminated):
            return False
        return True

    @property
    def is_empty(self) -> bool:
        return not self.raw

    def apply_limits(self, notes: List[Note]) -> List[Note]:
        if self.which == "first":
            return notes[: (self.limit or 1)]
        if self.which == "last":
            return notes[-((self.limit or 1)) :]
        if self.limit is not None:
            return notes[: self.limit]
        return notes

    def describe(self) -> str:
        parts: List[str] = ["tracks=%s" % ("all" if self.tracks is None else ",".join(map(str, self.tracks)))]
        if self.channels is not None:
            parts.append("channel=%s" % ",".join(map(str, self.channels)))
        if self.note_range.active:
            parts.append(self.note_range.describe("note"))
        if self.velocity_range.active:
            parts.append(self.velocity_range.describe("velocity"))
        if self.start_range.active:
            parts.append(self.start_range.describe("start"))
        if self.end_range.active:
            parts.append(self.end_range.describe("end"))
        if self.duration_range.active:
            parts.append(self.duration_range.describe("duration"))
        if self.limit is not None:
            parts.append("limit=%d" % self.limit)
        return " ".join(parts)

    def select(self, extraction: NoteExtraction) -> List[Note]:
        ordered = extraction.sorted()
        out = [n for i, n in enumerate(ordered) if self.matches(n, i)]
        return self.apply_limits(out)


def parse_selector_arg(value: Any) -> Dict[str, Any]:
    """Accept a selector as a dict, JSON text, bare-key JSON or 'key=value,key=value'."""
    if value is None or value == "":
        return {}
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        raise SchemaError("selector must be an object or a string: %r" % (value,))
    text = value.strip()
    if not text:
        return {}
    if "=" in text and not text.startswith("{"):
        from .jsonio import loads_relaxed

        out: Dict[str, Any] = {}
        for chunk in re.split(r"[,\s]+", text):
            if not chunk:
                continue
            if "=" not in chunk:
                raise SchemaError("selector shorthand needs key=value pairs: %r" % (chunk,))
            key, raw = chunk.split("=", 1)
            key = key.strip()
            raw = raw.strip()
            if raw.lower() in ("true", "false"):
                out[key] = raw.lower() == "true"
            else:
                try:
                    out[key] = int(raw)
                except ValueError:
                    try:
                        out[key] = float(raw)
                    except ValueError:
                        out[key] = raw
        return out
    if text.startswith("{"):
        from .jsonio import loads_relaxed

        parsed = loads_relaxed(text)
        if not isinstance(parsed, dict):
            raise SchemaError("selector must be an object: %r" % (value,))
        return parsed
    return {"type": text}


def _data_value(event: Event) -> Optional[int]:
    """The primary data value of a non-note channel message.

    control_change -> CC value, pitch_bend -> 0..16383, program_change -> program,
    aftertouch -> pressure.  Note velocity has its own selector field.
    """
    if not event.is_channel or event.type in ("note_on", "note_off"):
        return None
    if event.type in ("control_change", "poly_aftertouch"):
        return event.value2
    if event.type == "pitch_bend":
        return event.value
    return event.value1


def _as_token_list(value: Any) -> List[Any]:
    """Split 'pan,7' / [1, 2] / 7 into a flat list of tokens."""
    if isinstance(value, (list, tuple, set)):
        out: List[Any] = []
        for item in value:
            out.extend(_as_token_list(item))
        return out
    if isinstance(value, str):
        return [part for part in re.split(r"[,\s]+", value.strip()) if part]
    return [value]


def _control_number(value: Any) -> int:
    from .constants import cc_number

    if isinstance(value, str) and not value.strip().isdigit():
        return cc_number(value)
    return int(value)


def _program_number(value: Any) -> int:
    from .constants import program_number

    if isinstance(value, str) and not value.strip().isdigit():
        return program_number(value)
    return int(value)


def _opt_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise SchemaError("expected a number, got a boolean")
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    return float(text)


def _first(spec: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if spec.get(key) is not None:
            return spec[key]
    return None


def resolve_track_indices(midi: MidiFile, spec: Any, default_all: bool = True) -> List[int]:
    """Turn a 'tracks' argument into a concrete, validated index list."""
    if spec is None:
        return list(range(len(midi.tracks))) if default_all else []
    indexes = _as_int_list(spec, "tracks") or []
    out: List[int] = []
    for index in indexes:
        resolved = index if index >= 0 else len(midi.tracks) + index
        if not 0 <= resolved < len(midi.tracks):
            raise SchemaError(
                "track index %d out of range (file has %d track(s))" % (index, len(midi.tracks)),
                tracks=indexes,
            )
        if resolved not in out:
            out.append(resolved)
    return out
