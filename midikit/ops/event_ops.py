"""Event level operations: insert / delete / update / move / copy."""

from __future__ import annotations

from typing import Any, Dict, List, Sequence, Tuple

from ..constants import note_name
from ..errors import OpError, SchemaError
from ..events import Event, event_from_dict
from ..selectors import EventSelector, resolve_track_indices
from .registry import (
    OpContext,
    Param,
    clamp_pitch,
    delete_matched,
    insert_events_into,
    match_events,
    register,
)


def _events_arg(value: Any) -> List[Dict[str, Any]]:
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise SchemaError("'events' must be an event object or an array of event objects")
    out: List[Dict[str, Any]] = []
    for item in value:
        if isinstance(item, Event):
            out.append(item.to_dict())
        elif isinstance(item, dict):
            out.append(item)
        else:
            raise SchemaError("each event must be an object, got %s" % type(item).__name__)
    return out


@register(
    "insert_events",
    "插入任意事件（原始事件 JSON，可含 meta/sysex/CC/弯音等）",
    category="event",
    params=[
        Param("events", "json", required=True, help="事件对象或事件对象数组"),
        Param("track", "int", default=0, help="目标轨索引（负数从末尾算）"),
        Param("at", "time", default=0, help="基准位置，事件 tick 相对该位置"),
        Param("create_track", "bool", default=True, help="目标轨不存在时自动创建"),
    ],
    examples=[
        {
            "op": "insert_events",
            "track": 0,
            "events": [
                {"tick": 0, "type": "control_change", "channel": 0, "control": 7, "value": 100},
                {"tick": 0, "type": "meta", "meta_type": "marker", "text": "Intro"},
            ],
        }
    ],
)
def op_insert_events(ctx: OpContext, p: Dict[str, Any]) -> Dict[str, Any]:
    items = _events_arg(p["events"])
    if not items:
        return {"inserted": 0}
    base = int(p["at"] or 0)
    events: List[Event] = []
    for item in items:
        data = dict(item)
        data["tick"] = int(data.get("tick", data.get("at", 0)) or 0) + base
        events.append(event_from_dict(data))
    track_index = int(p["track"])
    if track_index < 0:
        track_index = len(ctx.midi.tracks) + track_index
    inserted = insert_events_into(ctx, track_index, events, create_track=bool(p["create_track"]))
    return {"inserted": inserted, "track": track_index, "events": [ev.to_dict() for ev in events[:20]]}


@register(
    "delete_events",
    "按选择器删除事件（可精确到轨道/索引/类型/tick/通道/文本）",
    category="event",
    params=[
        Param("match", "selector", default={}, help="事件选择器，空则要求显式限定"),
        Param("tracks", "int_list", default=None, help="限定轨道（可用负索引）"),
        Param("require_match", "bool", default=False, help="没有任何匹配时报错"),
    ],
    examples=[
        {"op": "delete_events", "match": {"type": "control_change", "control": 7}},
        {"op": "delete_events", "match": {"type": "text", "text_contains": "draft"}},
    ],
)
def op_delete_events(ctx: OpContext, p: Dict[str, Any]) -> Dict[str, Any]:
    selector = p["match"]
    selector_obj = EventSelector(selector)
    if selector_obj.is_empty and not p["tracks"]:
        raise SchemaError("delete_events needs a selector (or a tracks filter); refusing to delete everything")
    matches = match_events(ctx.midi, selector, p["tracks"], require=bool(p["require_match"]))
    removed = delete_matched(ctx, matches)
    return {
        "deleted": removed,
        "matched": len(matches),
        "selector": selector_obj.describe(),
        "sample": [_describe(m) for m in matches[:10]],
    }


@register(
    "update_events",
    "批量修改命中事件的字段（如 velocity/channel/value/text/tick）",
    category="event",
    params=[
        Param("match", "selector", required=True, help="事件选择器"),
        Param("set", "json", required=True, help="要覆盖的字段对象，例如 {\"velocity\": 90}"),
        Param("tracks", "int_list", default=None),
        Param("require_match", "bool", default=False),
    ],
    examples=[{"op": "update_events", "match": {"type": "note_on", "channel": 0}, "set": {"velocity": 80}}],
)
def op_update_events(ctx: OpContext, p: Dict[str, Any]) -> Dict[str, Any]:
    changes = p["set"]
    if not isinstance(changes, dict) or not changes:
        raise SchemaError("'set' must be a non-empty object of fields to overwrite")
    matches = match_events(ctx.midi, p["match"], p["tracks"], require=bool(p["require_match"]))
    updated = 0
    touched_tracks = set()
    for track_index, event_index, event in matches:
        data = event.to_dict()
        data.update(changes)
        try:
            replacement = event_from_dict(data)
        except SchemaError as exc:
            raise OpError(
                "cannot apply %s to event %d of track %d (%s): %s"
                % (changes, event_index, track_index, event.describe(), exc.message)
            )
        ctx.midi.tracks[track_index].events[event_index] = replacement
        updated += 1
        touched_tracks.add(track_index)
    for track_index in touched_tracks:
        ctx.midi.tracks[track_index].sort_events()
    ctx.invalidate_all()
    return {"updated": updated, "matched": len(matches), "set": changes}


@register(
    "move_events",
    "平移/缩放命中事件的时间位置（delta / seconds / set_tick / scale）",
    category="event",
    params=[
        Param("match", "selector", default={}, help="事件选择器（空表示全部事件）"),
        Param("tracks", "int_list", default=None),
        Param("delta", "time", default=0, help="相对位移（tick，可写 2b/250ms）"),
        Param("seconds", "float", default=None, help="按秒平移（受速度映射影响）"),
        Param("set_tick", "time", default=None, help="对齐：把最早的事件移动到该 tick"),
        Param("scale", "float", default=None, help="按比例缩放时间轴"),
        Param("anchor", "time", default=0, help="缩放锚点"),
        Param("clamp_min", "int", default=0, help="不允许早于该 tick"),
    ],
    examples=[
        {"op": "move_events", "match": {"type": "meta", "meta_type": "marker"}, "delta": "1bar"},
        {"op": "move_events", "match": {"tick_min": 960}, "delta": -960},
    ],
)
def op_move_events(ctx: OpContext, p: Dict[str, Any]) -> Dict[str, Any]:
    matches = match_events(ctx.midi, p["match"], p["tracks"])
    if not matches:
        return {"moved": 0}
    tempo_map = ctx.tempo_map
    anchor = int(p["anchor"] or 0)
    scale = float(p["scale"]) if p["scale"] is not None else None
    delta = int(p["delta"] or 0)
    seconds = p["seconds"]
    if p["set_tick"] is not None:
        earliest = min(event.tick for _ti, _ei, event in matches)
        delta = int(p["set_tick"]) - earliest
    moved = 0
    touched = set()
    for track_index, _event_index, event in matches:
        new_tick = event.tick
        if seconds is not None:
            new_tick = tempo_map.seconds_to_tick(tempo_map.tick_to_seconds(event.tick) + float(seconds))
        else:
            if scale is not None:
                new_tick = anchor + (new_tick - anchor) * scale
            new_tick += delta
        new_tick = int(round(new_tick))
        if new_tick < int(p["clamp_min"] or 0):
            new_tick = int(p["clamp_min"] or 0)
        if new_tick != event.tick:
            event.tick = new_tick
            moved += 1
        touched.add(track_index)
    for track_index in touched:
        ctx.midi.tracks[track_index].sort_events()
    ctx.invalidate_notes()
    return {"moved": moved, "matched": len(matches), "delta": delta, "scale": scale}


@register(
    "copy_events",
    "复制命中事件到目标轨/目标位置（可重复多次）",
    category="event",
    params=[
        Param("match", "selector", required=True),
        Param("tracks", "int_list", default=None),
        Param("to_track", "int", default=None, help="目标轨，默认留在原轨"),
        Param("tick_offset", "time", default=0, help="副本相对原位置的偏移"),
        Param("repeat", "int", default=1, help="复制份数，第 n 份偏移 n*tick_offset"),
        Param("create_track", "bool", default=True),
        Param("include_meta", "bool", default=True, help="是否连 meta/sysex 一起复制"),
        Param("pair_notes", "bool", default=True, help="只命中 note_on 或 note_off 一半时，把另一半一起复制"),
    ],
    examples=[{"op": "copy_events", "match": {"tick_min": 0, "tick_max": 1920}, "to_track": 2, "tick_offset": 1920, "repeat": 3}],
)
def op_copy_events(ctx: OpContext, p: Dict[str, Any]) -> Dict[str, Any]:
    matches = match_events(ctx.midi, p["match"], p["tracks"])
    if not p["include_meta"]:
        matches = [m for m in matches if m[2].is_channel]
    if not matches:
        return {"copied": 0}
    if p.get("pair_notes", True):
        matches = _with_paired_notes(ctx, matches)
    offset = int(p["tick_offset"] or 0)
    repeat = max(1, int(p["repeat"] or 1))
    target = p["to_track"]
    copied = 0
    buckets: Dict[Tuple[int, int], List[Event]] = {}
    for track_index, _event_index, event in matches:
        dest = int(target) if target is not None else track_index
        if dest < 0:
            dest = len(ctx.midi.tracks) + dest
        for step in range(1, repeat + 1):
            clone = event.clone()
            clone.tick = max(0, event.tick + offset * step)
            buckets.setdefault((dest, track_index), []).append(clone)
    for (dest, _src), events in buckets.items():
        insert_events_into(ctx, dest, events, create_track=bool(p["create_track"]))
        copied += len(events)
    ctx.invalidate_all()
    return {"copied": copied, "source_events": len(matches), "repeat": repeat, "tick_offset": offset}


@register(
    "copy_region",
    "复制一个时间区间内的全部内容到别处（乐句复制/循环扩展）",
    category="event",
    params=[
        Param("start", "time", default=0),
        Param("end", "time", required=True),
        Param("to", "time", required=True, help="目标区间起点"),
        Param("repeat", "int", default=1),
        Param("tracks", "int_list", default=None),
        Param("to_track", "int", default=None),
        Param("include_meta", "bool", default=False, help="是否复制速度/拍号等 meta 事件"),
    ],
    examples=[{"op": "copy_region", "start": 0, "end": "4bar", "to": "4bar", "repeat": 3}],
)
def op_copy_region(ctx: OpContext, p: Dict[str, Any]) -> Dict[str, Any]:
    start = int(p["start"] or 0)
    end = int(p["end"])
    if end <= start:
        raise SchemaError("copy_region needs end > start (got %s .. %s)" % (start, end))
    matches = match_events(ctx.midi, {"tick_min": start, "tick_max": end - 1}, p["tracks"])
    if not p["include_meta"]:
        matches = [m for m in matches if m[2].is_channel]
    # keep notes that start inside the region playable: carry their note_off along
    indices = resolve_track_indices(ctx.midi, p["tracks"])
    hanging_offs = set()
    for note in ctx.notes(indices).notes:
        if note.off_event is not None and start <= note.start < end and note.end >= end:
            hanging_offs.add(id(note.off_event))
    if hanging_offs:
        have = set(id(ev) for _ti, _ei, ev in matches)
        for track_index in indices:
            for event_index, event in enumerate(ctx.midi.tracks[track_index].events):
                if id(event) in hanging_offs and id(event) not in have:
                    matches.append((track_index, event_index, event))
    if not matches:
        return {"copied": 0}
    to = int(p["to"])
    repeat = max(1, int(p["repeat"] or 1))
    span = end - start
    target = p["to_track"]
    copied = 0
    buckets: Dict[int, List[Event]] = {}
    for track_index, _event_index, event in matches:
        dest = int(target) if target is not None else track_index
        if dest < 0:
            dest = len(ctx.midi.tracks) + dest
        for step in range(repeat):
            clone = event.clone()
            clone.tick = max(0, to + span * step + (event.tick - start))
            buckets.setdefault(dest, []).append(clone)
    for dest, events in buckets.items():
        insert_events_into(ctx, dest, events, create_track=True)
        copied += len(events)
    ctx.invalidate_all()
    return {"copied": copied, "source_events": len(matches), "repeat": repeat, "span": span}


@register(
    "dedupe_events",
    "删除完全重复的事件（同 tick 同内容），保留首个",
    category="event",
    params=[
        Param("tracks", "int_list", default=None),
        Param("keep", "choice", default="first", choices=["first", "last"]),
        Param("types", "str_list", default=None, help="只对指定事件类型去重"),
    ],
    examples=[{"op": "dedupe_events"}],
)
def op_dedupe_events(ctx: OpContext, p: Dict[str, Any]) -> Dict[str, Any]:
    indices = resolve_track_indices(ctx.midi, p["tracks"])
    types = set(p["types"] or []) or None
    removed = 0
    for index in indices:
        track = ctx.midi.tracks[index]
        seen: Dict[Tuple, Event] = {}
        keep: List[Event] = []
        for event in track.events:
            if types is not None and event.kind == "channel" and event.type not in types:
                keep.append(event)
                continue
            key = event.fingerprint(include_tick=True)
            if key in seen:
                if p["keep"] == "last":
                    keep[keep.index(seen[key])] = event
                    seen[key] = event
                removed += 1
                continue
            seen[key] = event
            keep.append(event)
        if removed:
            track.events = keep
    ctx.invalidate_all()
    return {"deleted": removed, "tracks": indices}


@register(
    "sort_events",
    "按 (tick, 角色) 稳定排序事件；note_off 排在 note_on 之前",
    category="event",
    params=[Param("tracks", "int_list", default=None)],
    examples=[{"op": "sort_events"}],
)
def op_sort_events(ctx: OpContext, p: Dict[str, Any]) -> Dict[str, Any]:
    indices = resolve_track_indices(ctx.midi, p["tracks"])
    changed = 0
    for index in indices:
        track = ctx.midi.tracks[index]
        before = [id(ev) for ev in track.events]
        track.sort_events()
        if [id(ev) for ev in track.events] != before:
            changed += 1
    ctx.invalidate_notes()
    return {"tracks_reordered": changed, "tracks": indices}


def _with_paired_notes(
    ctx: OpContext,
    matches: Sequence[Tuple[int, int, Event]],
) -> List[Tuple[int, int, Event]]:
    """Add the missing half of any note that was only partially selected.

    Copying a note_on without its note_off leaves a hanging note in the
    destination, so the counterpart is included automatically.
    """
    from ..notes import extract_notes

    have = set(id(event) for _ti, _ei, event in matches)
    track_indices = sorted(set(ti for ti, _ei, _event in matches))
    location: Dict[int, Tuple[int, int]] = {}
    for track_index in track_indices:
        for event_index, event in enumerate(ctx.midi.tracks[track_index].events):
            location[id(event)] = (track_index, event_index)
    extra: List[Tuple[int, int, Event]] = []
    for track_index in track_indices:
        for note in extract_notes(ctx.midi, [track_index]).notes:
            on_event, off_event = note.on_event, note.off_event
            if on_event is None or off_event is None:
                continue
            has_on, has_off = id(on_event) in have, id(off_event) in have
            if has_on == has_off:
                continue
            missing = off_event if has_on else on_event
            where = location.get(id(missing))
            if where is None:
                continue
            have.add(id(missing))
            extra.append((where[0], where[1], missing))
    if not extra:
        return list(matches)
    combined = list(matches) + extra
    combined.sort(key=lambda item: (item[0], item[1]))
    return combined


def _describe(match: Tuple[int, int, Event]) -> Dict[str, Any]:
    track_index, event_index, event = match
    return {
        "track": track_index,
        "index": event_index,
        "tick": event.tick,
        "kind": event.kind,
        "type": event.type if event.is_channel else (event.name if event.is_meta else event.kind),
        "summary": event.describe(),
    }
