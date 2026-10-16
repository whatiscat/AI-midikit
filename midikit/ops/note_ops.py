"""Note level operations: everything that edits sounding notes."""

from __future__ import annotations

import math
import random
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..constants import note_name, note_number, scale_steps
from ..errors import OpError, SchemaError
from ..notes import Note, note_from_dict, notes_to_events, parse_note_spec
from ..selectors import NoteSelector, resolve_track_indices
from .registry import (
    OpContext,
    Param,
    apply_note_transform,
    clamp_pitch,
    clamp_velocity,
    diatonic_transpose,
    insert_events_into,
    match_events,
    match_notes,
    register,
)

UNIT_HELP = "时间单位：ticks / beats / seconds / bars（数字前可带 2b、500ms 后缀）"


def _convert_unit(ctx: OpContext, value: int, unit: str) -> int:
    if unit == "ticks" or value is None:
        return int(value or 0)
    tempo_map = ctx.tempo_map
    if unit == "beats":
        return int(round(value * tempo_map.division.ticks_per_beat))
    if unit == "seconds":
        return tempo_map.seconds_to_tick(value)
    if unit == "bars":
        numerator, denominator = tempo_map.time_signature_at(0)
        return int(round(value * tempo_map.bar_ticks(numerator, denominator)))
    raise SchemaError("unknown unit %r (use ticks, beats, seconds or bars)" % (unit,))


def _note_from_item(
    ctx: OpContext,
    item: Any,
    unit: str,
    track: int,
    channel: int,
) -> Note:
    if isinstance(item, str):
        return parse_note_spec(item, unit=unit, default_track=track, default_channel=channel, tempo_map=ctx.tempo_map)
    if isinstance(item, Note):
        return item.clone()
    if not isinstance(item, dict):
        raise SchemaError("note must be an object or a string like 'C4 0 480 100'")
    data = dict(item)
    if unit != "ticks":
        if data.get("start") is not None:
            data["start"] = _convert_unit(ctx, float(data["start"]), unit)
        if data.get("at") is not None:
            data["at"] = _convert_unit(ctx, float(data["at"]), unit)
        if data.get("tick") is not None:
            data["tick"] = _convert_unit(ctx, float(data["tick"]), unit)
        if data.get("duration") is not None:
            data["duration"] = _convert_unit(ctx, float(data["duration"]), unit)
        if data.get("length") is not None:
            data["length"] = _convert_unit(ctx, float(data["length"]), unit)
        if data.get("end") is not None:
            data["end"] = _convert_unit(ctx, float(data["end"]), unit)
    return note_from_dict(data, default_track=track, default_channel=channel)


@register(
    "insert_notes",
    "写入音符（自动生成 note_on/note_off，可按 ticks/beats/seconds/bars 计时）",
    category="note",
    params=[
        Param("notes", "note_list", required=True, help="音符数组，元素为对象或 'C4 0 1 100' 字符串"),
        Param("track", "int", default=0, help="默认目标轨（音符对象内的 track 优先）"),
        Param("channel", "int", default=0, help="默认通道"),
        Param("unit", "choice", default="ticks", choices=["ticks", "beats", "seconds", "bars"], help=UNIT_HELP),
        Param("create_track", "bool", default=True),
        Param("track_name", "str", default=None, help="新建轨道时使用的名称"),
    ],
    examples=[
        {"op": "insert_notes", "track": 1, "channel": 0, "unit": "beats",
         "notes": [{"pitch": "C4", "start": 0, "duration": 1, "velocity": 100},
                   {"pitch": "E4", "start": 1, "duration": 1, "velocity": 96}]},
        {"op": "insert_notes", "track": 0, "notes": ["C4 0 480 100", "G4 480 480 90"]},
    ],
)
def op_insert_notes(ctx: OpContext, p: Dict[str, Any]) -> Dict[str, Any]:
    items = p["notes"]
    if not items:
        return {"inserted": 0}
    grouped: Dict[int, List[Note]] = {}
    total = 0
    for item in items:
        note = _note_from_item(ctx, item, p["unit"], int(p["track"]), int(p["channel"]))
        grouped.setdefault(note.track, []).append(note)
        total += 1
    for track_index, notes in grouped.items():
        events = notes_to_events(notes)
        insert_events_into(
            ctx,
            track_index,
            events,
            create_track=bool(p["create_track"]),
            name=p["track_name"] if len(grouped) == 1 else None,
        )
    return {
        "inserted": total,
        "tracks": sorted(grouped),
        "by_track": {str(k): len(v) for k, v in grouped.items()},
    }


@register(
    "delete_notes",
    "删除符合条件的音符（连同其 note_on/note_off）",
    category="note",
    params=[
        Param("match", "selector", default={}, help="音符选择器"),
        Param("tracks", "int_list", default=None),
        Param("require_match", "bool", default=False),
    ],
    examples=[
        {"op": "delete_notes", "match": {"pitch": "C4"}},
        {"op": "delete_notes", "match": {"note_max": 47, "channel": 0}},
        {"op": "delete_notes", "match": {"start_min": 3840}},
    ],
)
def op_delete_notes(ctx: OpContext, p: Dict[str, Any]) -> Dict[str, Any]:
    selector = NoteSelector(p["match"])
    if selector.is_empty and not p["tracks"]:
        raise SchemaError("delete_notes needs a selector or a tracks filter; refusing to delete every note")
    result = apply_note_transform(ctx, selector, lambda note: None, p["tracks"], require=bool(p["require_match"]))
    return {
        "deleted_notes": result["notes_affected"],
        "events_removed": result["events_removed"],
        "selector": result["selector"],
    }


@register(
    "update_notes",
    "批量修改音符属性（音高/力度/起点/时值/通道，支持相对偏移）",
    category="note",
    params=[
        Param("match", "selector", required=True),
        Param("set", "json", default=None, help='绝对赋值对象：{"velocity": 90, "pitch": "D4"}'),
        Param("pitch_offset", "int", default=0, help="音高半音偏移"),
        Param("velocity_offset", "int", default=0),
        Param("start_offset", "time", default=0, help="起点位移（可写 1/16? 不支持，用 tick/beats）"),
        Param("duration_offset", "time", default=0),
        Param("channel", "int", default=None, help="改到指定通道"),
        Param("tracks", "int_list", default=None),
    ],
    examples=[
        {"op": "update_notes", "match": {"channel": 0}, "set": {"velocity": 70}},
        {"op": "update_notes", "match": {"note_min": 60, "note_max": 72}, "pitch_offset": 12},
    ],
)
def op_update_notes(ctx: OpContext, p: Dict[str, Any]) -> Dict[str, Any]:
    changes = p["set"] or {}
    if not isinstance(changes, dict):
        raise SchemaError("'set' must be an object")

    def transform(note: Note) -> Note:
        if changes.get("pitch") is not None or changes.get("note") is not None:
            note.pitch = note_number(changes.get("pitch", changes.get("note")))
        if changes.get("velocity") is not None or changes.get("vel") is not None:
            note.velocity = clamp_velocity(int(changes.get("velocity", changes.get("vel"))))
        if changes.get("start") is not None or changes.get("at") is not None:
            note.start = int(changes.get("start", changes.get("at")))
        if changes.get("end") is not None:
            note.end = int(changes["end"])
        elif changes.get("duration") is not None:
            note.end = note.start + int(changes["duration"])
        elif changes.get("length") is not None:
            note.end = note.start + int(changes["length"])
        if changes.get("channel") is not None:
            note.channel = int(changes["channel"])
        note.pitch = clamp_pitch(note.pitch + int(p["pitch_offset"] or 0))
        note.velocity = clamp_velocity(note.velocity + int(p["velocity_offset"] or 0))
        start_offset = int(p["start_offset"] or 0)
        duration_offset = int(p["duration_offset"] or 0)
        if start_offset:
            note.start += start_offset
        if duration_offset:
            note.end += duration_offset
        if p["channel"] is not None:
            note.channel = int(p["channel"])
        if note.start < 0:
            note.start = 0
        if note.end <= note.start:
            note.end = note.start + 1
        return note

    result = apply_note_transform(ctx, p["match"], transform, p["tracks"])
    return {"updated_notes": result["notes_affected"], "selector": result["selector"], "set": changes}


@register(
    "transpose",
    "移调：半音移调或按音阶级数移调（可限定音域与调式）",
    category="note",
    params=[
        Param("match", "selector", default={}),
        Param("tracks", "int_list", default=None),
        Param("semitones", "int", default=0, help="半音数，可负"),
        Param("degrees", "int", default=0, help="音阶级数（配合 scale/root）"),
        Param("scale", "str", default=None, help="调式：major/minor/dorian/pentatonic/blues ..."),
        Param("root", "str", default="C", help="调式主音，如 C/F#/Bb"),
        Param("clamp", "bool", default=True, help="超出 0-127 时折回音域内；false 则丢弃该音"),
        Param("out_of_range", "choice", default="fold", choices=["fold", "clamp", "drop"]),
    ],
    examples=[
        {"op": "transpose", "semitones": 2, "match": {"channel": 0}},
        {"op": "transpose", "degrees": 2, "scale": "major", "root": "C"},
        {"op": "transpose", "semitones": -12, "match": {"note_min": 60}},
    ],
)
def op_transpose(ctx: OpContext, p: Dict[str, Any]) -> Dict[str, Any]:
    semitones = int(p["semitones"] or 0)
    degrees = int(p["degrees"] or 0)
    steps = scale_steps(p["scale"]) if p["scale"] else None
    root_pc = note_number(p["root"]) % 12 if p["root"] is not None else 0
    if semitones == 0 and degrees == 0:
        raise SchemaError("transpose needs semitones and/or degrees")
    mode = p["out_of_range"] or "fold"
    dropped = 0

    def transform(note: Note) -> Optional[Note]:
        nonlocal dropped
        pitch = note.pitch
        if steps and degrees:
            pitch = diatonic_transpose(pitch, degrees, root_pc, steps)
        pitch += semitones
        if 0 <= pitch <= 127:
            note.pitch = pitch
            return note
        if mode == "drop":
            dropped += 1
            return None
        if mode == "clamp":
            note.pitch = clamp_pitch(pitch)
            return note
        while pitch < 0:
            pitch += 12
        while pitch > 127:
            pitch -= 12
        note.pitch = clamp_pitch(pitch)
        return note

    result = apply_note_transform(ctx, p["match"], transform, p["tracks"])
    return {
        "transposed": result["notes_affected"],
        "semitones": semitones,
        "degrees": degrees,
        "scale": p["scale"],
        "dropped": dropped,
        "selector": result["selector"],
    }


@register(
    "quantize",
    "量化：把音符起点/终点吸附到网格（支持强度与摇摆）",
    category="note",
    params=[
        Param("match", "selector", default={}),
        Param("tracks", "int_list", default=None),
        Param("grid", "grid", required=True, help="网格：1/16、eighth、1/8t、beat、bar 或 tick 数"),
        Param("strength", "float", default=1.0, help="0-1，量化强度"),
        Param("target", "choice", default="start", choices=["start", "end", "both", "duration"]),
        Param("swing", "float", default=0.0, help="0-1，奇数格后移 swing*grid/2"),
        Param("anchor", "time", default=0, help="网格原点"),
    ],
    examples=[
        {"op": "quantize", "grid": "1/16", "strength": 1.0},
        {"op": "quantize", "grid": "1/8", "swing": 0.6, "target": "both"},
    ],
)
def op_quantize(ctx: OpContext, p: Dict[str, Any]) -> Dict[str, Any]:
    grid = float(p["grid"])
    if grid <= 0:
        raise SchemaError("quantize grid must be > 0")
    strength = float(p["strength"] if p["strength"] is not None else 1.0)
    if not 0.0 <= strength <= 1.0:
        raise SchemaError("quantize strength must be between 0 and 1")
    swing = float(p["swing"] or 0.0)
    anchor = int(p["anchor"] or 0)
    target = p["target"]

    def snap(value: float) -> float:
        # round half up so a note exactly between two grid slots moves forward
        slot = math.floor((value - anchor) / grid + 0.5)
        position = anchor + slot * grid
        if swing and int(slot) % 2 == 1:
            position += swing * grid / 2.0
        return value + (position - value) * strength

    def transform(note: Note) -> Note:
        if target in ("start", "both"):
            note.start = max(0, int(round(snap(note.start))))
        if target in ("end", "both"):
            note.end = max(0, int(round(snap(note.end))))
        if target == "duration":
            duration = snap(note.duration)
            note.end = note.start + max(1, int(round(duration)))
        if note.end <= note.start:
            note.end = note.start + 1
        return note

    result = apply_note_transform(ctx, p["match"], transform, p["tracks"])
    return {
        "quantized": result["notes_affected"],
        "grid_ticks": grid,
        "strength": strength,
        "swing": swing,
        "target": target,
        "selector": result["selector"],
    }


@register(
    "humanize",
    "人性化：给时值与力度加入可控随机抖动（seed 固定则结果可复现）",
    category="note",
    params=[
        Param("match", "selector", default={}),
        Param("tracks", "int_list", default=None),
        Param("timing", "int", default=10, help="起点抖动上限（tick，双向）"),
        Param("duration", "int", default=0, help="时值抖动上限（tick，双向）"),
        Param("velocity", "int", default=8, help="力度抖动上限（双向）"),
        Param("seed", "int", default=0, help="随机种子"),
        Param("min_velocity", "int", default=1),
    ],
    examples=[{"op": "humanize", "timing": 12, "velocity": 10, "seed": 7}],
)
def op_humanize(ctx: OpContext, p: Dict[str, Any]) -> Dict[str, Any]:
    rng = random.Random(int(p["seed"] or 0))
    timing = int(p["timing"] or 0)
    duration = int(p["duration"] or 0)
    velocity = int(p["velocity"] or 0)
    minimum = int(p["min_velocity"] or 0)
    offsets = {"notes": 0}

    def transform(note: Note) -> Note:
        offsets["notes"] += 1
        if timing:
            note.start = max(0, note.start + rng.randint(-timing, timing))
        if duration:
            note.end = max(note.start + 1, note.end + rng.randint(-duration, duration))
        if velocity:
            note.velocity = max(minimum, clamp_velocity(note.velocity + rng.randint(-velocity, velocity)))
        return note

    result = apply_note_transform(ctx, p["match"], transform, p["tracks"])
    return {"humanized": result["notes_affected"], "seed": int(p["seed"] or 0), "selector": result["selector"]}


@register(
    "set_velocity",
    "力度处理：设置/缩放/偏移/限幅/渐变/随机",
    category="note",
    params=[
        Param("match", "selector", default={}),
        Param("tracks", "int_list", default=None),
        Param("mode", "choice", default="set", choices=["set", "scale", "offset", "limit", "ramp", "random"]),
        Param("value", "float", default=None, help="set/scale/offset/ramp 起点值"),
        Param("value_to", "float", default=None, help="ramp 终点值"),
        Param("min", "int", default=1),
        Param("max", "int", default=127),
        Param("seed", "int", default=0),
    ],
    examples=[
        {"op": "set_velocity", "mode": "scale", "value": 0.8},
        {"op": "set_velocity", "mode": "limit", "min": 40, "max": 100},
        {"op": "set_velocity", "mode": "ramp", "value": 30, "value_to": 120},
    ],
)
def op_set_velocity(ctx: OpContext, p: Dict[str, Any]) -> Dict[str, Any]:
    mode = p["mode"]
    value = p["value"]
    if mode in ("set", "scale", "offset", "ramp") and value is None:
        raise SchemaError("set_velocity mode %r needs 'value'" % mode)
    if mode == "ramp" and p["value_to"] is None:
        raise SchemaError("set_velocity mode 'ramp' needs 'value_to'")
    rng = random.Random(int(p["seed"] or 0))
    low = clamp_velocity(int(p["min"] if p["min"] is not None else 0))
    high = clamp_velocity(int(p["max"] if p["max"] is not None else 127))
    if high < low:
        low, high = high, low
    selected = match_notes(ctx, p["match"], p["tracks"])
    if not selected:
        return {"updated_notes": 0, "mode": mode}
    ordered = sorted(selected, key=lambda n: (n.start, n.pitch))
    ramp_map = {}
    if mode == "ramp" and len(ordered) > 0:
        span = float(ordered[-1].start - ordered[0].start) or 1.0
        for note in ordered:
            ratio = (note.start - ordered[0].start) / span if span else 0.0
            ramp_map[id(note)] = float(value) + (float(p["value_to"]) - float(value)) * ratio

    def transform(note: Note) -> Note:
        current = note.velocity
        if mode == "set":
            current = float(value)
        elif mode == "scale":
            current = current * float(value)
        elif mode == "offset":
            current = current + float(value)
        elif mode == "limit":
            current = min(max(current, low), high)
        elif mode == "ramp":
            current = ramp_map.get(id(note), float(value))
        elif mode == "random":
            current = rng.uniform(low, high)
        note.velocity = clamp_velocity(int(round(current)))
        return note

    result = apply_note_transform(ctx, p["match"], transform, p["tracks"])
    return {"updated_notes": result["notes_affected"], "mode": mode, "selector": result["selector"]}


@register(
    "set_note_length",
    "时值处理：统一时值/按比例缩放/连奏/断奏",
    category="note",
    params=[
        Param("match", "selector", default={}),
        Param("tracks", "int_list", default=None),
        Param("mode", "choice", default="set", choices=["set", "scale", "legato", "staccato", "gate"]),
        Param("value", "float", default=None, help="set=时值(tick)；scale/gate=比例(如 0.5)"),
        Param("min", "time", default=1, help="最小时值"),
        Param("max", "time", default=None, help="最大时值"),
    ],
    examples=[
        {"op": "set_note_length", "mode": "scale", "value": 0.5},
        {"op": "set_note_length", "mode": "legato"},
        {"op": "set_note_length", "mode": "set", "value": 240},
    ],
)
def op_set_note_length(ctx: OpContext, p: Dict[str, Any]) -> Dict[str, Any]:
    mode = p["mode"]
    value = p["value"]
    if mode == "set" and value is None:
        raise SchemaError("set_note_length mode 'set' needs 'value' (ticks)")
    if mode == "gate" and value is None:
        value = 0.5
    if mode in ("scale",) and value is None:
        value = 1.0
    minimum = int(p["min"] or 1)
    maximum = int(p["max"]) if p["max"] is not None else None
    indices = resolve_track_indices(ctx.midi, p["tracks"])
    next_start: Dict[Tuple[int, int, int], int] = {}
    if mode == "legato":
        for index in indices:
            extraction = ctx.notes([index])
            previous: Dict[Tuple[int, int], Note] = {}
            for note in sorted(extraction.notes, key=lambda n: (n.start, n.pitch)):
                key = (index, note.channel)
                if key in previous:
                    prev_note = previous[key]
                    if note.start > prev_note.start:
                        next_start[(index, prev_note.start, prev_note.pitch)] = note.start
                previous[key] = note
    def transform(note: Note) -> Note:
        if mode == "set":
            note.end = note.start + int(round(float(value)))
        elif mode == "scale":
            note.end = note.start + max(1, int(round(note.duration * float(value))))
        elif mode == "gate":
            note.end = note.start + max(1, int(round(note.duration * float(value))))
        elif mode == "staccato":
            note.end = note.start + max(1, int(round(note.duration * float(value if value is not None else 0.5))))
        elif mode == "legato":
            target = next_start.get((note.track, note.start, note.pitch))
            if target is not None:
                note.end = max(note.start + 1, target)
            else:
                note.end = note.start + max(1, note.duration)
        if note.end - note.start < minimum:
            note.end = note.start + minimum
        if maximum is not None and note.end - note.start > maximum:
            note.end = note.start + maximum
        return note

    result = apply_note_transform(ctx, p["match"], transform, p["tracks"])
    return {"updated_notes": result["notes_affected"], "mode": mode, "selector": result["selector"]}


@register(
    "limit_pitch_range",
    "限制音域：把超范围的音符折回八度、夹紧或丢弃",
    category="note",
    params=[
        Param("match", "selector", default={}),
        Param("tracks", "int_list", default=None),
        Param("min_pitch", "pitch", default=0),
        Param("max_pitch", "pitch", default=127),
        Param("mode", "choice", default="fold", choices=["fold", "clamp", "drop"]),
    ],
    examples=[{"op": "limit_pitch_range", "min_pitch": "C3", "max_pitch": "C6", "mode": "fold"}],
)
def op_limit_pitch_range(ctx: OpContext, p: Dict[str, Any]) -> Dict[str, Any]:
    low = clamp_pitch(int(p["min_pitch"]))
    high = clamp_pitch(int(p["max_pitch"]))
    if high < low:
        low, high = high, low
    mode = p["mode"]
    dropped = 0

    def transform(note: Note) -> Optional[Note]:
        nonlocal dropped
        pitch = note.pitch
        if low <= pitch <= high:
            return note
        if mode == "drop":
            dropped += 1
            return None
        if mode == "clamp":
            note.pitch = min(max(pitch, low), high)
            return note
        while pitch < low:
            pitch += 12
        while pitch > high:
            pitch -= 12
        if pitch < low or pitch > high:
            note.pitch = min(max(pitch, low), high)
        else:
            note.pitch = pitch
        return note

    result = apply_note_transform(ctx, p["match"], transform, p["tracks"])
    return {"updated_notes": result["notes_affected"], "dropped": dropped, "range": [low, high], "mode": mode}


@register(
    "remove_duplicate_notes",
    "去除重复音符（同音高、起点相近），可按最长/最响保留",
    category="note",
    params=[
        Param("tracks", "int_list", default=None),
        Param("tolerance", "int", default=0, help="起点容差（tick）"),
        Param("keep", "choice", default="first", choices=["first", "longest", "loudest"]),
        Param("match", "selector", default={}),
    ],
    examples=[{"op": "remove_duplicate_notes", "tolerance": 10, "keep": "longest"}],
)
def op_remove_duplicate_notes(ctx: OpContext, p: Dict[str, Any]) -> Dict[str, Any]:
    selector = NoteSelector(p["match"])
    indices = resolve_track_indices(ctx.midi, p["tracks"])
    tolerance = int(p["tolerance"] or 0)
    victims: List[Tuple[int, int, int, int]] = []
    for index in indices:
        extraction = ctx.notes([index])
        groups: Dict[Tuple[int, int], List[Note]] = {}
        for note in sorted(extraction.notes, key=lambda n: (n.start, n.pitch)):
            if not selector.matches(note):
                continue
            key = (note.channel, note.pitch)
            bucket = None
            for existing in groups.get(key, []):
                if abs(existing.start - note.start) <= tolerance:
                    bucket = existing
                    break
            if bucket is None:
                groups.setdefault(key, []).append(note)
                continue
            keep_new = False
            if p["keep"] == "longest" and note.duration > bucket.duration:
                keep_new = True
            elif p["keep"] == "loudest" and note.velocity > bucket.velocity:
                keep_new = True
            if keep_new:
                groups[key][groups[key].index(bucket)] = note
                victims.append((index, bucket.start, bucket.pitch, bucket.channel))
            else:
                victims.append((index, note.start, note.pitch, note.channel))
    if not victims:
        return {"deleted_notes": 0}
    victim_keys = set(victims)

    def transform(note: Note) -> Optional[Note]:
        key = (note.track, note.start, note.pitch, note.channel)
        if key in victim_keys:
            victim_keys.discard(key)
            return None
        return note

    result = apply_note_transform(ctx, NoteSelector({}), transform, p["tracks"])
    return {"deleted_notes": result["notes_affected"], "tolerance": tolerance, "keep": p["keep"]}


@register(
    "strum",
    "和弦扫弦：把同一时刻的音符按顺序错开（吉他/竖琴效果）",
    category="note",
    params=[
        Param("match", "selector", default={}),
        Param("tracks", "int_list", default=None),
        Param("window", "time", default=30, help="整个和弦的最大错开时间（tick）"),
        Param("direction", "choice", default="up", choices=["up", "down", "random"]),
        Param("tolerance", "int", default=2, help="判定为同一和弦的起点容差"),
        Param("seed", "int", default=0),
        Param("keep_end", "bool", default=True, help="终点保持不动（只推迟起点）"),
    ],
    examples=[{"op": "strum", "window": 40, "direction": "up"}],
)
def op_strum(ctx: OpContext, p: Dict[str, Any]) -> Dict[str, Any]:
    indices = resolve_track_indices(ctx.midi, p["tracks"])
    selector = NoteSelector(p["match"])
    window = int(p["window"] or 0)
    tolerance = int(p["tolerance"] or 0)
    rng = random.Random(int(p["seed"] or 0))
    plan: Dict[Tuple[int, int, int], int] = {}
    for index in indices:
        extraction = ctx.notes([index])
        selected = [n for n in extraction.notes if selector.matches(n)]
        clusters: List[List[Note]] = []
        for note in sorted(selected, key=lambda n: (n.start, n.pitch)):
            if clusters and abs(note.start - clusters[-1][0].start) <= tolerance:
                clusters[-1].append(note)
            else:
                clusters.append([note])
        for cluster in clusters:
            if len(cluster) < 2:
                continue
            ordered = sorted(cluster, key=lambda n: n.pitch)
            if p["direction"] == "down":
                ordered = list(reversed(ordered))
            elif p["direction"] == "random":
                rng.shuffle(ordered)
            step = window / float(len(ordered) - 1) if len(ordered) > 1 else 0.0
            for position, note in enumerate(ordered):
                offset = int(round(position * step))
                if offset:
                    plan[(note.track, note.start, note.pitch, note.channel)] = offset
    if not plan:
        return {"strummed": 0}

    def transform(note: Note) -> Note:
        offset = plan.get((note.track, note.start, note.pitch, note.channel))
        if offset:
            if p["keep_end"]:
                note.end += offset
            note.start += offset
        return note

    result = apply_note_transform(ctx, selector, transform, p["tracks"])
    return {"strummed": result["notes_affected"], "window": window, "direction": p["direction"]}


@register(
    "crop",
    "裁剪时间区间：保留 [start, end) 内容，越界音符被剪短或删除",
    category="note",
    params=[
        Param("start", "time", default=0),
        Param("end", "time", required=True),
        Param("tracks", "int_list", default=None),
        Param("shift", "bool", default=False, help="裁剪后整体平移到 0"),
        Param("keep_meta", "bool", default=True, help="保留区间外的 meta 事件（速度/拍号等）"),
        Param("keep_notes_crossing_start", "bool", default=True, help="起点在区间前但延续到区间内的音符保留并剪短"),
    ],
    examples=[{"op": "crop", "start": "1bar", "end": "5bar", "shift": True}],
)
def op_crop(ctx: OpContext, p: Dict[str, Any]) -> Dict[str, Any]:
    start = int(p["start"] or 0)
    end = int(p["end"])
    if end <= start:
        raise SchemaError("crop needs end > start (got %s .. %s)" % (start, end))
    indices = resolve_track_indices(ctx.midi, p["tracks"])
    removed_events = 0
    kept_notes = 0

    # 1) note aware clipping
    selector = NoteSelector({})
    clip_start = start if p["keep_notes_crossing_start"] else None

    def transform(note: Note) -> Optional[Note]:
        nonlocal kept_notes
        if note.end <= start or note.start >= end:
            return None
        if clip_start is not None and note.start < start:
            note.start = start
        if note.end > end:
            note.end = end
        if note.end <= note.start:
            return None
        kept_notes += 1
        return note

    result = apply_note_transform(ctx, selector, transform, indices)

    # 2) drop remaining events outside the window
    for index in indices:
        track = ctx.midi.tracks[index]
        keep = []
        for event in track.events:
            if start <= event.tick < end:
                keep.append(event)
                continue
            if p["keep_meta"] and (event.is_meta or event.is_sysex):
                keep.append(event)
                continue
            removed_events += 1
        track.events = keep

    if p["shift"]:
        for index in indices:
            for event in ctx.midi.tracks[index].events:
                event.tick = max(0, event.tick - start)
            ctx.midi.tracks[index].sort_events()
    ctx.invalidate_all()
    return {
        "notes_kept": kept_notes,
        "events_removed": removed_events,
        "events_rebuilt": result["events_added"],
        "range": [start, end],
        "shifted": bool(p["shift"]),
    }
