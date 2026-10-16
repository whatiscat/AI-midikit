"""Operation registry: metadata, parameter validation and shared edit helpers.

Every edit operation declares its parameters once; the same declaration drives
JSON plan validation, the generated command line interface and the
self-describing 'ops' command.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from ..errors import OpError, SchemaError
from ..events import ChannelEvent, Event, MetaEvent, event_from_dict
from ..notes import Note, NoteExtraction, extract_notes, notes_to_events
from ..selectors import EventSelector, NoteSelector, resolve_track_indices
from ..smf import MidiFile, MidiTrack
from ..tempo import TempoMap, parse_time


class Param:
    """Declaration of one operation parameter."""

    __slots__ = ("name", "type", "default", "required", "help", "choices", "aliases")

    def __init__(
        self,
        name: str,
        type: str = "str",
        default: Any = None,
        required: bool = False,
        help: str = "",
        choices: Optional[Sequence[str]] = None,
        aliases: Sequence[str] = (),
    ) -> None:
        self.name = name
        self.type = type
        self.default = default
        self.required = required
        self.help = help
        self.choices = list(choices) if choices else None
        self.aliases = tuple(aliases)

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"name": self.name, "type": self.type}
        if self.required:
            out["required"] = True
        if self.default is not None:
            out["default"] = self.default
        if self.choices:
            out["choices"] = self.choices
        if self.aliases:
            out["aliases"] = list(self.aliases)
        if self.help:
            out["help"] = self.help
        return out


class Op:
    """A named, validated edit operation."""

    def __init__(
        self,
        name: str,
        func: Callable[["OpContext", Dict[str, Any]], Dict[str, Any]],
        summary: str = "",
        category: str = "edit",
        params: Sequence[Param] = (),
        examples: Sequence[Dict[str, Any]] = (),
        notes: str = "",
    ) -> None:
        self.name = name
        self.func = func
        self.summary = summary
        self.category = category
        self.params = list(params)
        self.examples = list(examples)
        self.notes = notes

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "name": self.name,
            "summary": self.summary,
            "category": self.category,
            "params": [p.to_dict() for p in self.params],
        }
        if self.examples:
            out["examples"] = self.examples
        if self.notes:
            out["notes"] = self.notes
        return out

    def __repr__(self) -> str:
        return "Op(%s)" % self.name


OPS: Dict[str, Op] = {}


def register(
    name: str,
    summary: str = "",
    category: str = "edit",
    params: Sequence[Param] = (),
    examples: Sequence[Dict[str, Any]] = (),
    notes: str = "",
):
    """Decorator registering an operation implementation."""

    def decorator(func):
        if name in OPS:
            raise RuntimeError("duplicate op registration: %s" % name)
        OPS[name] = Op(name, func, summary, category, params, examples, notes)
        return func

    return decorator


def get_op(name: str) -> Op:
    key = str(name).strip()
    if key not in OPS:
        raise SchemaError(
            "unknown op %r" % (name,),
            available=sorted(OPS),
        )
    return OPS[key]


def describe_ops(category: Optional[str] = None) -> List[Dict[str, Any]]:
    items = [op.to_dict() for op in OPS.values() if category is None or op.category == category]
    return sorted(items, key=lambda item: (item["category"], item["name"]))


# ---------------------------------------------------------------------------
# parameter coercion
# ---------------------------------------------------------------------------


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("true", "yes", "y", "1", "on"):
        return True
    if text in ("false", "no", "n", "0", "off", ""):
        return False
    raise SchemaError("expected a boolean: %r" % (value,))


def _as_list(value: Any, item_type: str) -> List[Any]:
    if isinstance(value, (list, tuple)):
        items = list(value)
    elif isinstance(value, str):
        items = [part for part in value.replace(",", " ").split() if part]
    else:
        items = [value]
    if item_type == "int":
        return [int(item) for item in items]
    return [item if isinstance(item, str) else str(item) for item in items]


def coerce_param(param: Param, value: Any, ctx: Optional["OpContext"] = None) -> Any:
    if value is None:
        return None
    kind = param.type
    if kind == "int":
        if isinstance(value, bool):
            return int(value)
        return int(value)
    if kind == "float":
        return float(value)
    if kind == "bool":
        return _as_bool(value)
    if kind == "str":
        return value if isinstance(value, str) else str(value)
    if kind == "pitch":
        from ..constants import note_number

        return note_number(value)
    if kind == "program":
        from ..constants import program_number

        if isinstance(value, bool):
            raise SchemaError("program must be a number or a GM instrument name")
        if isinstance(value, int):
            return value
        text = str(value).strip()
        if text.isdigit():
            number = int(text)
            return number - 1 if number > 127 else number
        return program_number(text)
    if kind == "json":
        if isinstance(value, str):
            try:
                return json.loads(value)
            except ValueError as exc:
                raise SchemaError("parameter %r expects JSON: %s" % (param.name, exc))
        return value
    if kind == "int_list":
        return _as_list(value, "int")
    if kind == "str_list":
        return _as_list(value, "str")
    if kind == "time":
        if isinstance(value, bool):
            raise SchemaError("parameter %r expects a time value" % param.name)
        if isinstance(value, (int, float)):
            return int(value)
        tempo_map = ctx.tempo_map if ctx is not None else None
        try:
            return parse_time(value, tempo_map, default_unit="tick")
        except ValueError as exc:
            raise SchemaError("parameter %r: %s" % (param.name, exc))
    if kind == "grid":
        if ctx is None:
            return value
        return ctx.grid_ticks(value)
    if kind == "selector":
        if value is None or value == {}:
            return {}
        if isinstance(value, (str, dict)):
            from ..selectors import parse_selector_arg

            try:
                return parse_selector_arg(value)
            except SchemaError as exc:
                raise SchemaError("parameter %r: %s" % (param.name, exc.message))
        raise SchemaError("parameter %r expects a selector object" % param.name)
    if kind == "note_list":
        if isinstance(value, (list, tuple)):
            return list(value)
        return [value]
    if kind == "choice":
        text = str(value)
        if param.choices and text not in param.choices:
            raise SchemaError(
                "parameter %r must be one of %s (got %r)" % (param.name, ", ".join(param.choices), value)
            )
        return text
    return value


def validate_params(op: Op, raw: Any, ctx: Optional["OpContext"] = None) -> Dict[str, Any]:
    """Check and coerce the parameters of one op call."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise SchemaError("op %r expects a parameter object, got %s" % (op.name, type(raw).__name__))
    by_name = {p.name: p for p in op.params}
    alias_map: Dict[str, str] = {}
    for param in op.params:
        for alias in param.aliases:
            alias_map.setdefault(alias, param.name)
    values: Dict[str, Any] = {}
    for key, value in raw.items():
        target = key if key in by_name else alias_map.get(key)
        if target is None:
            raise SchemaError(
                "op %r has no parameter %r" % (op.name, key),
                allowed=sorted(list(by_name) + list(alias_map)),
            )
        values[target] = value
    for param in op.params:
        if param.name not in values or values[param.name] is None:
            if param.required and param.name not in values:
                raise SchemaError("op %r requires parameter %r" % (op.name, param.name))
            if param.name not in values:
                if param.default is not None:
                    values[param.name] = param.default
                elif param.type == "bool":
                    values[param.name] = False
                elif param.type in ("selector",):
                    values[param.name] = {}
                elif param.type in ("int_list", "str_list", "note_list"):
                    values[param.name] = None
                else:
                    values[param.name] = None
    for name, value in list(values.items()):
        if name in by_name:
            values[name] = coerce_param(by_name[name], value, ctx)
    return values


# ---------------------------------------------------------------------------
# context
# ---------------------------------------------------------------------------


class OpContext:
    """Carries the file being edited plus caches and the change log."""

    def __init__(
        self,
        midi: MidiFile,
        encoding: str = "utf-8",
        pairing: str = "lifo",
        strict: bool = False,
        default_unit: str = "tick",
    ) -> None:
        self.midi = midi
        self.encoding = encoding
        self.pairing = pairing
        self.strict = strict
        self.default_unit = default_unit
        self._notes: Dict[Tuple[int, ...], NoteExtraction] = {}
        self._tempo_map: Optional[TempoMap] = None
        self.changes: List[Dict[str, Any]] = []

    # -- caches ----------------------------------------------------------
    @property
    def tempo_map(self) -> TempoMap:
        if self._tempo_map is None:
            self._tempo_map = TempoMap.from_midi(self.midi)
        return self._tempo_map

    def invalidate_tempo(self) -> None:
        self._tempo_map = None

    def invalidate_notes(self) -> None:
        self._notes.clear()

    def notes(self, track_indices: Optional[Iterable[int]] = None) -> NoteExtraction:
        key = tuple(sorted(track_indices)) if track_indices is not None else tuple(range(len(self.midi.tracks)))
        if key not in self._notes:
            self._notes[key] = extract_notes(self.midi, key, pairing=self.pairing)
        return self._notes[key]

    def track(self, index: int) -> MidiTrack:
        return self.midi.track(index)

    def ensure_track(self, index: int, create: bool = True, name: Optional[str] = None) -> MidiTrack:
        if 0 <= index < len(self.midi.tracks):
            track = self.midi.tracks[index]
            if name:
                track.set_name(name, self.encoding)
            return track
        if not create:
            raise SchemaError("track index %d does not exist (file has %d)" % (index, len(self.midi.tracks)))
        while len(self.midi.tracks) <= index:
            self.midi.tracks.append(MidiTrack())
        track = self.midi.tracks[index]
        if name:
            track.set_name(name, self.encoding)
        return track

    def invalidate_all(self) -> None:
        self.invalidate_notes()
        self.invalidate_tempo()

    # -- helpers ----------------------------------------------------------
    def grid_ticks(self, grid: Any) -> float:
        """Resolve a grid spec ('1/16', 'beat', 'bar', '1/8t', 120) into ticks."""
        return grid_to_ticks(grid, self.tempo_map)

    def log(self, op: str, **info: Any) -> Dict[str, Any]:
        item = {"op": op}
        item.update(info)
        self.changes.append(item)
        return item


# ---------------------------------------------------------------------------
# shared edit helpers
# ---------------------------------------------------------------------------


def match_events(
    midi: MidiFile,
    selector: Any,
    tracks: Any = None,
    require: bool = False,
) -> List[Tuple[int, int, Event]]:
    sel = selector if isinstance(selector, EventSelector) else EventSelector(selector)
    indices = resolve_track_indices(midi, tracks)
    if sel.tracks is None and tracks is not None:
        sel.tracks = list(indices)
    found = sel.filter(midi)
    if require and not found:
        raise OpError("no events matched selector: %s" % sel.describe(), selector=sel.raw)
    return found


def match_notes(
    ctx: OpContext,
    selector: Any,
    tracks: Any = None,
    require: bool = False,
) -> List[Note]:
    sel = selector if isinstance(selector, NoteSelector) else NoteSelector(selector)
    indices = resolve_track_indices(ctx.midi, tracks)
    if sel.tracks is None and tracks is not None:
        sel.tracks = list(indices)
    extraction = ctx.notes(indices)
    selected = sel.select(extraction)
    if require and not selected:
        raise OpError("no notes matched selector: %s" % sel.describe(), selector=sel.raw)
    return selected


def apply_note_transform(
    ctx: OpContext,
    selector: Any,
    transform: Callable[[Note], Any],
    tracks: Any = None,
    require: bool = False,
) -> Dict[str, Any]:
    """Edit matched notes by rebuilding their note_on / note_off events.

    The transform receives a copy of the note and returns a Note, a list of
    Notes, or None to delete it.
    """
    sel = selector if isinstance(selector, NoteSelector) else NoteSelector(selector)
    indices = resolve_track_indices(ctx.midi, tracks)
    if sel.tracks is None and tracks is not None:
        sel.tracks = list(indices)
    plans: List[Tuple[int, List[Note]]] = []
    for index in indices:
        extraction = extract_notes(ctx.midi, [index], pairing=ctx.pairing)
        selected = sel.select(extraction)
        if selected:
            plans.append((index, selected))
    if require and not plans:
        raise OpError("no notes matched selector: %s" % sel.describe(), selector=sel.raw)

    total_notes = 0
    removed = 0
    added = 0
    details: List[Dict[str, Any]] = []
    for index, selected in plans:
        track = ctx.midi.tracks[index]
        remove_ids: Set[int] = set()
        for note in selected:
            if note.on_event is not None:
                remove_ids.add(id(note.on_event))
            if note.off_event is not None:
                remove_ids.add(id(note.off_event))
        before = len(track.events)
        if remove_ids:
            track.events = [ev for ev in track.events if id(ev) not in remove_ids]
        removed += before - len(track.events)
        new_notes: List[Note] = []
        for note in selected:
            result = transform(note.clone())
            if result is None:
                continue
            if isinstance(result, (list, tuple)):
                new_notes.extend(item for item in result if item is not None)
            else:
                new_notes.append(result)
        events = notes_to_events(new_notes)
        track.events.extend(events)
        track.sort_events()
        added += len(events)
        total_notes += len(selected)
        details.append({"track": index, "notes": len(selected), "events_added": len(events)})
    ctx.invalidate_notes()
    return {
        "notes_affected": total_notes,
        "events_removed": removed,
        "events_added": added,
        "tracks": details,
        "selector": sel.describe(),
    }


def insert_events_into(
    ctx: OpContext,
    track_index: int,
    events: Sequence[Event],
    create_track: bool = True,
    name: Optional[str] = None,
) -> int:
    track = ctx.ensure_track(track_index, create=create_track, name=name)
    for event in events:
        track.events.append(event)
    track.sort_events()
    ctx.invalidate_notes()
    return len(events)


def remove_events(track: MidiTrack, items: Sequence[Tuple[int, int, Event]]) -> int:
    """Remove matched events from one track by identity."""
    ids = set(id(event) for _ti, _ei, event in items if _ti == 0)
    return len(ids)


def _identity_ids(items: Sequence[Tuple[int, int, Event]], track_index: int) -> Set[int]:
    return set(id(event) for ti, _ei, event in items if ti == track_index)


def delete_matched(ctx: OpContext, matches: Sequence[Tuple[int, int, Event]]) -> int:
    by_track: Dict[int, Set[int]] = {}
    for track_index, _event_index, event in matches:
        by_track.setdefault(track_index, set()).add(id(event))
    removed = 0
    for track_index, ids in by_track.items():
        track = ctx.midi.tracks[track_index]
        before = len(track.events)
        track.events = [ev for ev in track.events if id(ev) not in ids]
        removed += before - len(track.events)
    ctx.invalidate_all()
    return removed


GRID_NAMES: Dict[str, float] = {
    "whole": 4.0,
    "semibreve": 4.0,
    "half": 2.0,
    "minim": 2.0,
    "quarter": 1.0,
    "crotchet": 1.0,
    "beat": 1.0,
    "eighth": 0.5,
    "8th": 0.5,
    "quaver": 0.5,
    "sixteenth": 0.25,
    "16th": 0.25,
    "semiquaver": 0.25,
    "thirtysecond": 0.125,
    "32nd": 0.125,
    "sixtyfourth": 0.0625,
    "64th": 0.0625,
}


def grid_to_ticks(grid: Any, tempo_map: TempoMap) -> float:
    """Resolve a musical grid into ticks.

    Accepts a fraction of a whole note ('1/16'), a note name ('eighth'),
    'beat' / 'bar', a triplet or dotted variant ('1/8t', '1/4.'), or a tick count.
    """
    if grid is None:
        raise SchemaError("a grid value is required")
    ppq = tempo_map.division.ticks_per_beat
    if isinstance(grid, bool):
        raise SchemaError("bad grid value: %r" % (grid,))
    if isinstance(grid, (int, float)):
        return float(grid)
    text = str(grid).strip().lower().replace(" ", "")
    if not text:
        raise SchemaError("empty grid value")
    multiplier = 1.0
    while text and text[-1] in "td":
        if text[-1] == "t":
            multiplier *= 2.0 / 3.0
        else:
            multiplier *= 1.5
        text = text[:-1]
    if text in ("bar", "bars", "measure"):
        numerator, denominator = tempo_map.time_signature_at(0)
        return tempo_map.bar_ticks(numerator, denominator)
    if text in GRID_NAMES:
        return GRID_NAMES[text] * ppq * multiplier
    if "/" in text:
        try:
            numerator, denominator = text.split("/")
            return (float(numerator) / float(denominator)) * 4.0 * ppq * multiplier
        except (ValueError, ZeroDivisionError):
            raise SchemaError("bad grid value: %r" % (grid,))
    try:
        return float(text) * multiplier
    except ValueError:
        raise SchemaError(
            "bad grid value: %r (use a fraction like 1/16, a name like eighth, or ticks)" % (grid,)
        )


def clamp_pitch(pitch: int) -> int:
    return 0 if pitch < 0 else (127 if pitch > 127 else int(pitch))


def clamp_velocity(velocity: int) -> int:
    return 0 if velocity < 0 else (127 if velocity > 127 else int(velocity))


def diatonic_transpose(pitch: int, degrees: int, root_pc: int = 0, steps: Sequence[int] = (0, 2, 4, 5, 7, 9, 11)) -> int:
    """Move a pitch by N scale degrees inside a scale.

    Pitches outside the scale keep their chromatic offset from the nearest
    scale tone below them.
    """
    if degrees == 0:
        return int(pitch)
    scale_set = set((int(root_pc) + int(s)) % 12 for s in steps)
    scale_pitches = [p for p in range(0, 128) if p % 12 in scale_set]
    if not scale_pitches:
        raise ValueError("empty scale")
    lower = int(pitch)
    while lower % 12 not in scale_set and lower > 0:
        lower -= 1
    remainder = int(pitch) - lower
    if lower in scale_pitches:
        index = scale_pitches.index(lower)
    else:
        index = -1
    target = index + int(degrees)
    octaves, offset = divmod(target, len(scale_pitches))
    return scale_pitches[offset] + octaves * 12 + remainder
