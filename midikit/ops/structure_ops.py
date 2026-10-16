"""Track / structure / meta (tempo, time signature, text) operations."""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

from ..constants import META_NAME_TO_TYPE, program_name, program_number
from ..errors import OpError, SchemaError
from ..events import ChannelEvent, Event, MetaEvent, meta_type_number
from ..notes import notes_to_events
from ..selectors import EventSelector, resolve_track_indices
from ..smf import Division, MidiTrack
from .registry import (
    OpContext,
    Param,
    delete_matched,
    insert_events_into,
    match_events,
    register,
)

TEXT_META_CHOICES = [
    "text",
    "copyright",
    "track_name",
    "instrument_name",
    "lyric",
    "marker",
    "cue_point",
    "program_name",
    "device_name",
]


def _replace_meta_at(ctx: OpContext, track_index: int, type_byte: int, tick: int, event: MetaEvent) -> None:
    track = ctx.ensure_track(track_index)
    track.events = [ev for ev in track.events if not (ev.is_meta and ev.type_byte == type_byte and ev.tick == tick)]
    track.events.append(event)
    track.sort_events()


@register(
    "set_tempo",
    "设置速度（BPM 或微秒/四分音符），可替换全部速度",
    category="meta",
    params=[
        Param("bpm", "float", default=None, help="每分钟拍数"),
        Param("tempo", "int", default=None, help="微秒/四分音符（与 bpm 二选一）"),
        Param("at", "time", default=0, help="生效位置"),
        Param("track", "int", default=0, help="写入哪条轨（默认第 0 轨）"),
        Param("replace_all", "bool", default=False, help="先删除所有已有速度事件"),
    ],
    examples=[
        {"op": "set_tempo", "bpm": 128},
        {"op": "set_tempo", "bpm": 90, "at": "8bar"},
        {"op": "set_tempo", "bpm": 120, "replace_all": True},
    ],
)
def op_set_tempo(ctx: OpContext, p: Dict[str, Any]) -> Dict[str, Any]:
    if p["bpm"] is None and p["tempo"] is None:
        raise SchemaError("set_tempo needs bpm or tempo")
    event = MetaEvent.make_tempo(int(p["at"] or 0), tempo=p["tempo"], bpm=p["bpm"])
    removed = 0
    if p["replace_all"]:
        for track in ctx.midi.tracks:
            before = len(track.events)
            track.events = [ev for ev in track.events if not (ev.is_meta and ev.type_byte == 0x51)]
            removed += before - len(track.events)
    _replace_meta_at(ctx, int(p["track"]), 0x51, event.tick, event)
    ctx.invalidate_tempo()
    return {
        "bpm": round(event.bpm, 4),
        "tempo": event.tempo,
        "tick": event.tick,
        "track": int(p["track"]),
        "removed": removed,
    }


@register(
    "scale_tempo",
    "整体缩放速度（factor>1 变慢），或把所有速度统一为某个 BPM",
    category="meta",
    params=[
        Param("factor", "float", default=None, help="速度乘数，例如 1.1 表示放慢 10%"),
        Param("bpm", "float", default=None, help="统一为固定 BPM（与 factor 二选一）"),
        Param("tracks", "int_list", default=None),
    ],
    examples=[{"op": "scale_tempo", "factor": 0.9}, {"op": "scale_tempo", "bpm": 120}],
)
def op_scale_tempo(ctx: OpContext, p: Dict[str, Any]) -> Dict[str, Any]:
    if p["factor"] is None and p["bpm"] is None:
        raise SchemaError("scale_tempo needs factor or bpm")
    indices = resolve_track_indices(ctx.midi, p["tracks"])
    changed = 0
    target_tempo = None
    if p["bpm"] is not None:
        target_tempo = MetaEvent.make_tempo(0, bpm=p["bpm"]).tempo
    factor = float(p["factor"]) if p["factor"] is not None else 1.0
    for index in indices:
        for event in ctx.midi.tracks[index].events:
            if event.is_meta and event.type_byte == 0x51 and len(event.data) == 3:
                current = (event.data[0] << 16) | (event.data[1] << 8) | event.data[2]
                new_value = target_tempo if target_tempo is not None else int(round(current * factor))
                new_value = max(1, min(0xFFFFFF, new_value))
                event.data = bytes([(new_value >> 16) & 0xFF, (new_value >> 8) & 0xFF, new_value & 0xFF])
                changed += 1
    ctx.invalidate_tempo()
    return {"tempo_events_changed": changed, "factor": factor if target_tempo is None else None,
            "bpm": p["bpm"]}


@register(
    "tempo_ramp",
    "速度渐变：在区间内按线性/指数曲线写入一串速度事件",
    category="meta",
    params=[
        Param("bpm_start", "float", required=True),
        Param("bpm_end", "float", required=True),
        Param("start", "time", default=0),
        Param("end", "time", required=True),
        Param("steps", "int", default=16, help="写入多少个速度事件（含首尾）"),
        Param("curve", "choice", default="linear", choices=["linear", "exponential"]),
        Param("track", "int", default=0),
        Param("clear_range", "bool", default=True, help="先清掉区间内已有速度事件"),
    ],
    examples=[{"op": "tempo_ramp", "bpm_start": 90, "bpm_end": 140, "start": "8bar", "end": "16bar", "steps": 24}],
)
def op_tempo_ramp(ctx: OpContext, p: Dict[str, Any]) -> Dict[str, Any]:
    start = int(p["start"] or 0)
    end = int(p["end"])
    if end <= start:
        raise SchemaError("tempo_ramp needs end > start")
    steps = max(2, int(p["steps"] or 16))
    bpm_a = float(p["bpm_start"])
    bpm_b = float(p["bpm_end"])
    if bpm_a <= 0 or bpm_b <= 0:
        raise SchemaError("tempo_ramp bpm values must be > 0")
    track_index = int(p["track"])
    if p["clear_range"]:
        track = ctx.ensure_track(track_index)
        track.events = [
            ev
            for ev in track.events
            if not (ev.is_meta and ev.type_byte == 0x51 and start <= ev.tick <= end)
        ]
    written = 0
    for step in range(steps):
        ratio = step / float(steps - 1)
        if p["curve"] == "exponential":
            bpm = bpm_a * (bpm_b / bpm_a) ** ratio
        else:
            bpm = bpm_a + (bpm_b - bpm_a) * ratio
        tick = int(round(start + (end - start) * ratio))
        event = MetaEvent.make_tempo(tick, bpm=bpm)
        _replace_meta_at(ctx, track_index, 0x51, tick, event)
        written += 1
    ctx.invalidate_tempo()
    return {"tempo_events_written": written, "start": start, "end": end, "bpm_start": bpm_a, "bpm_end": bpm_b}


@register(
    "set_time_signature",
    "设置拍号（可指定位置）",
    category="meta",
    params=[
        Param("numerator", "int", required=True),
        Param("denominator", "int", default=4),
        Param("at", "time", default=0),
        Param("track", "int", default=0),
        Param("clocks_per_click", "int", default=24),
        Param("notated_32nd", "int", default=8),
    ],
    examples=[{"op": "set_time_signature", "numerator": 6, "denominator": 8, "at": 0}],
)
def op_set_time_signature(ctx: OpContext, p: Dict[str, Any]) -> Dict[str, Any]:
    event = MetaEvent.make_time_signature(
        int(p["at"] or 0),
        numerator=int(p["numerator"]),
        denominator=int(p["denominator"]),
        clocks_per_click=int(p["clocks_per_click"] or 24),
        notated_32nd_per_beat=int(p["notated_32nd"] or 8),
    )
    _replace_meta_at(ctx, int(p["track"]), 0x58, event.tick, event)
    ctx.invalidate_tempo()
    return {
        "numerator": int(p["numerator"]),
        "denominator": int(p["denominator"]),
        "tick": event.tick,
        "track": int(p["track"]),
    }


@register(
    "set_key_signature",
    "设置调号（key 名称或 sf/mi 数值）",
    category="meta",
    params=[
        Param("key", "str", default=None, help="如 C / Am / F#m / Eb major"),
        Param("sf", "int", default=None, help="-7..7 升降号数量"),
        Param("mi", "int", default=0, help="0 大调 1 小调"),
        Param("at", "time", default=0),
        Param("track", "int", default=0),
    ],
    examples=[{"op": "set_key_signature", "key": "D major"}, {"op": "set_key_signature", "sf": -2, "mi": 1}],
)
def op_set_key_signature(ctx: OpContext, p: Dict[str, Any]) -> Dict[str, Any]:
    if p["key"] is None and p["sf"] is None:
        raise SchemaError("set_key_signature needs key or sf")
    event = MetaEvent.make_key_signature(int(p["at"] or 0), sf=int(p["sf"] or 0), mi=int(p["mi"] or 0), key=p["key"])
    _replace_meta_at(ctx, int(p["track"]), 0x59, event.tick, event)
    sf, mi = event.key_signature
    from ..constants import KEY_SIGNATURES

    return {
        "key": KEY_SIGNATURES.get(sf, ("?", "?"))[1 if mi else 0],
        "sf": sf,
        "mi": mi,
        "tick": event.tick,
    }


@register(
    "add_text",
    "写入文本类 meta 事件（歌词/标记/版权/音轨名等）",
    category="meta",
    params=[
        Param("text", "str", required=True),
        Param("meta_type", "choice", default="marker", choices=TEXT_META_CHOICES),
        Param("at", "time", default=0),
        Param("track", "int", default=0),
        Param("replace", "bool", default=True, help="同一位置同类型的旧事件会被替换"),
        Param("encoding", "str", default=None, help="文本编码，默认跟随文件"),
    ],
    examples=[
        {"op": "add_text", "meta_type": "lyric", "text": "Hello", "at": "4bar"},
        {"op": "add_text", "meta_type": "marker", "text": "Chorus", "at": "8bar"},
    ],
)
def op_add_text(ctx: OpContext, p: Dict[str, Any]) -> Dict[str, Any]:
    encoding = p["encoding"] or ctx.encoding
    type_byte = meta_type_number(p["meta_type"])
    event = MetaEvent(type_byte, str(p["text"]).encode(encoding), int(p["at"] or 0))
    if p["replace"]:
        _replace_meta_at(ctx, int(p["track"]), type_byte, event.tick, event)
    else:
        track = ctx.ensure_track(int(p["track"]))
        track.events.append(event)
        track.sort_events()
    return {"meta_type": p["meta_type"], "tick": event.tick, "text": p["text"], "track": int(p["track"])}


@register(
    "strip_meta",
    "清理 meta 事件（例如去掉全部歌词/文本，保留速度与拍号）",
    category="meta",
    params=[
        Param("meta_types", "str_list", default=["lyric", "text", "marker", "cue_point"],
              help="要删除的 meta 名称，或 all"),
        Param("tracks", "int_list", default=None),
        Param("keep_tempo", "bool", default=True),
        Param("keep_time_signature", "bool", default=True),
        Param("keep_key_signature", "bool", default=True),
        Param("keep_end_of_track", "bool", default=True),
        Param("keep_track_name", "bool", default=True),
    ],
    examples=[{"op": "strip_meta", "meta_types": ["lyric", "text"]}],
)
def op_strip_meta(ctx: OpContext, p: Dict[str, Any]) -> Dict[str, Any]:
    names = p["meta_types"] or []
    remove_all = "all" in [str(n).lower() for n in names]
    wanted = set()
    for name in names:
        wanted.add(meta_type_number(name))
    protected = set()
    if p["keep_tempo"]:
        protected.add(0x51)
    if p["keep_time_signature"]:
        protected.add(0x58)
    if p["keep_key_signature"]:
        protected.add(0x59)
    if p["keep_end_of_track"]:
        protected.add(0x2F)
    if p["keep_track_name"]:
        protected.add(0x03)
    indices = resolve_track_indices(ctx.midi, p["tracks"])
    removed = 0
    for index in indices:
        track = ctx.midi.tracks[index]
        keep: List[Event] = []
        for event in track.events:
            if event.is_meta and event.type_byte not in protected:
                if remove_all or event.type_byte in wanted:
                    removed += 1
                    continue
            keep.append(event)
        track.events = keep
    ctx.invalidate_all()
    return {"deleted": removed, "tracks": indices, "protected": sorted(protected)}


@register(
    "set_channel",
    "改通道：把匹配事件搬到另一个 MIDI 通道",
    category="event",
    params=[
        Param("channel", "int", required=True, help="目标通道 0-15"),
        Param("from_channel", "int", default=None, help="只改这个通道的事件"),
        Param("match", "selector", default={}),
        Param("tracks", "int_list", default=None),
    ],
    examples=[{"op": "set_channel", "from_channel": 0, "channel": 2}],
)
def op_set_channel(ctx: OpContext, p: Dict[str, Any]) -> Dict[str, Any]:
    target = int(p["channel"])
    if not 0 <= target <= 15:
        raise SchemaError("channel must be 0-15: %r" % (p["channel"],))
    spec = dict(p["match"] or {})
    if p["from_channel"] is not None:
        spec["channel"] = int(p["from_channel"])
    spec.setdefault("type", "channel")
    matches = match_events(ctx.midi, spec, p["tracks"])
    changed = 0
    for _track_index, _event_index, event in matches:
        if event.is_channel and event.channel != target:
            event.channel = target
            changed += 1
    ctx.invalidate_all()
    return {"channel": target, "changed": changed, "matched": len(matches), "from_channel": p["from_channel"]}


@register(
    "set_instrument",
    "设置音色：写入 program_change（可含 bank select 与音色名）",
    category="event",
    params=[
        Param("program", "program", required=True, help="GM 序号(0-127 或 1-128)或名称，如 'Acoustic Grand Piano'"),
        Param("channel", "int", default=0),
        Param("track", "int", default=0),
        Param("at", "time", default=0),
        Param("bank_msb", "int", default=None),
        Param("bank_lsb", "int", default=None),
        Param("name", "bool", default=False, help="同时写入 program_name meta"),
        Param("replace", "bool", default=True, help="替换同位置已有的 program_change"),
    ],
    examples=[
        {"op": "set_instrument", "program": "Cello", "channel": 1},
        {"op": "set_instrument", "program": 25, "bank_msb": 0, "bank_lsb": 1},
    ],
)
def op_set_instrument(ctx: OpContext, p: Dict[str, Any]) -> Dict[str, Any]:
    raw = p["program"]
    try:
        program = program_number(raw)
    except (ValueError, TypeError):
        raise SchemaError(
            "unknown GM program: %r (use 0-127 as a number, 1-128 as a string, or a GM name)" % (raw,)
        )
    if not 0 <= program <= 127:
        raise SchemaError("program must be 0-127 (or 1-128): %r" % (raw,))
    track_index = int(p["track"])
    tick = int(p["at"] or 0)
    channel = int(p["channel"])
    track = ctx.ensure_track(track_index)
    inserted = []
    if p["bank_msb"] is not None:
        inserted.append(ChannelEvent("control_change", channel, 0, int(p["bank_msb"]), tick))
    if p["bank_lsb"] is not None:
        inserted.append(ChannelEvent("control_change", channel, 32, int(p["bank_lsb"]), tick))
    inserted.append(ChannelEvent("program_change", channel, program, tick))
    if p["name"]:
        inserted.append(MetaEvent.make_text(tick, "program_name", program_name(program), ctx.encoding))
    if p["replace"]:
        ids = set(id(ev) for ev in inserted)
        track.events = [
            ev
            for ev in track.events
            if id(ev) in ids
            or not (
                ev.tick == tick
                and (
                    (ev.is_channel and ev.type == "program_change" and ev.channel == channel)
                    or (ev.is_meta and ev.type_byte == 0x08)
                )
            )
        ]
    track.events.extend(inserted)
    track.sort_events()
    ctx.invalidate_all()
    return {
        "program": program,
        "program_number": program + 1,
        "program_name": program_name(program),
        "channel": channel,
        "tick": tick,
        "wrote": len(inserted),
    }


@register(
    "insert_track",
    "新建轨道（可指定位置，可从已有轨复制内容）",
    category="track",
    params=[
        Param("index", "int", default=-1, help="插入位置，-1 表示追加到末尾"),
        Param("name", "str", default=None),
        Param("copy_from", "int", default=None, help="复制该轨的事件"),
        Param("channel", "int", default=None, help="复制后把所有事件改到该通道"),
    ],
    examples=[{"op": "insert_track", "name": "Bass", "index": 2}],
)
def op_insert_track(ctx: OpContext, p: Dict[str, Any]) -> Dict[str, Any]:
    index = int(p["index"])
    if index < 0:
        index = len(ctx.midi.tracks) + index + 1
    index = max(0, min(index, len(ctx.midi.tracks)))
    events: List[Event] = []
    if p["copy_from"] is not None:
        source = ctx.midi.track(int(p["copy_from"]))
        events = [ev.clone() for ev in source.events if not (ev.is_meta and ev.type_byte == 0x2F)]
    track = MidiTrack()
    track.events = events
    if p["channel"] is not None:
        for event in track.events:
            if event.is_channel:
                event.channel = int(p["channel"])
    if p["name"]:
        track.set_name(str(p["name"]), ctx.encoding)
    elif p["copy_from"] is not None:
        source_name = ctx.midi.track(int(p["copy_from"])).name
        if source_name:
            track.set_name(source_name + " copy", ctx.encoding)
    ctx.midi.tracks.insert(index, track)
    ctx.invalidate_all()
    return {"index": index, "name": track.name, "events": len(track.events), "tracks": len(ctx.midi.tracks)}


@register(
    "delete_track",
    "删除轨道",
    category="track",
    params=[
        Param("track", "int_list", required=True, help="要删除的轨道索引"),
        Param("keep_min_one", "bool", default=True, help="不允许删到一条不剩"),
    ],
    examples=[{"op": "delete_track", "track": [3]}],
)
def op_delete_track(ctx: OpContext, p: Dict[str, Any]) -> Dict[str, Any]:
    indices = sorted(set(resolve_track_indices(ctx.midi, p["track"])), reverse=True)
    if p["keep_min_one"] and len(indices) >= len(ctx.midi.tracks):
        raise OpError("refusing to delete every track (set keep_min_one=false to allow)")
    removed = []
    for index in indices:
        removed.append({"index": index, "name": ctx.midi.tracks[index].name})
        del ctx.midi.tracks[index]
    ctx.invalidate_all()
    return {"deleted": len(removed), "tracks_removed": removed, "tracks": len(ctx.midi.tracks)}


@register(
    "move_track",
    "调整轨道顺序",
    category="track",
    params=[
        Param("track", "int", required=True),
        Param("to", "int", required=True),
    ],
    examples=[{"op": "move_track", "track": 2, "to": 0}],
)
def op_move_track(ctx: OpContext, p: Dict[str, Any]) -> Dict[str, Any]:
    source = resolve_track_indices(ctx.midi, [p["track"]])[0]
    target = int(p["to"])
    if target < 0:
        target = len(ctx.midi.tracks) + target
    target = max(0, min(target, len(ctx.midi.tracks) - 1))
    track = ctx.midi.tracks.pop(source)
    ctx.midi.tracks.insert(target, track)
    ctx.invalidate_all()
    return {"from": source, "to": target, "name": track.name}


@register(
    "clone_track",
    "复制轨道（可改名与整体位移）",
    category="track",
    params=[
        Param("track", "int", required=True),
        Param("to", "int", default=-1, help="插入位置，-1 追加"),
        Param("name", "str", default=None),
        Param("tick_offset", "time", default=0),
    ],
    examples=[{"op": "clone_track", "track": 1, "name": "Piano 2", "tick_offset": "4bar"}],
)
def op_clone_track(ctx: OpContext, p: Dict[str, Any]) -> Dict[str, Any]:
    source_index = resolve_track_indices(ctx.midi, [p["track"]])[0]
    source = ctx.midi.tracks[source_index]
    clone = source.clone()
    offset = int(p["tick_offset"] or 0)
    if offset:
        for event in clone.events:
            event.tick = max(0, event.tick + offset)
        clone.sort_events()
    name = p["name"] or ((source.name + " copy") if source.name else None)
    if name:
        clone.set_name(str(name), ctx.encoding)
    index = int(p["to"])
    if index < 0:
        index = len(ctx.midi.tracks) + index + 1
    index = max(0, min(index, len(ctx.midi.tracks)))
    ctx.midi.tracks.insert(index, clone)
    ctx.invalidate_all()
    return {"source": source_index, "index": index, "events": len(clone.events), "name": clone.name}


@register(
    "set_track_name",
    "重命名轨道（写入 track_name meta）",
    category="track",
    params=[
        Param("track", "int", default=0),
        Param("name", "str", required=True),
    ],
    examples=[{"op": "set_track_name", "track": 0, "name": "Conductor"}],
)
def op_set_track_name(ctx: OpContext, p: Dict[str, Any]) -> Dict[str, Any]:
    index = resolve_track_indices(ctx.midi, [p["track"]])[0]
    ctx.midi.tracks[index].set_name(str(p["name"]), ctx.encoding)
    return {"track": index, "name": p["name"]}


@register(
    "merge_tracks",
    "合并多条轨为一条（保留各事件通道信息）",
    category="track",
    params=[
        Param("tracks", "int_list", required=True, help="要合并的轨道索引（至少两条）"),
        Param("target", "int", default=None, help="保留哪条轨作为目标，默认第一条"),
        Param("name", "str", default=None),
        Param("rechannel", "int", default=None, help="合并后统一改到该通道"),
    ],
    examples=[{"op": "merge_tracks", "tracks": [1, 2, 3], "target": 1}],
)
def op_merge_tracks(ctx: OpContext, p: Dict[str, Any]) -> Dict[str, Any]:
    indices = resolve_track_indices(ctx.midi, p["tracks"])
    if len(indices) < 2:
        raise SchemaError("merge_tracks needs at least two tracks")
    target = int(p["target"]) if p["target"] is not None else indices[0]
    target = resolve_track_indices(ctx.midi, [target])[0]
    if target not in indices:
        indices.append(target)
    merged: List[Event] = []
    for index in indices:
        merged.extend(ev.clone() for ev in ctx.midi.tracks[index].events if not (ev.is_meta and ev.type_byte == 0x2F))
    if p["rechannel"] is not None:
        for event in merged:
            if event.is_channel:
                event.channel = int(p["rechannel"])
    for index in sorted(indices, reverse=True):
        del ctx.midi.tracks[index]
    insert_at = min(min(indices), len(ctx.midi.tracks))
    track = MidiTrack(merged, name=p["name"], encoding=ctx.encoding)
    if p["name"] is None:
        track.set_name("Merged", ctx.encoding)
    track.sort_events()
    ctx.midi.tracks.insert(insert_at, track)
    ctx.invalidate_all()
    return {"merged": len(indices), "target": insert_at, "events": len(track.events), "tracks": len(ctx.midi.tracks)}


@register(
    "split_track_by_channel",
    "按通道拆分轨道（每条通道一条新轨）",
    category="track",
    params=[
        Param("track", "int", default=0),
        Param("name_prefix", "str", default=None),
        Param("keep_meta", "bool", default=True, help="非通道事件复制到每条新轨"),
    ],
    examples=[{"op": "split_track_by_channel", "track": 0}],
)
def op_split_track_by_channel(ctx: OpContext, p: Dict[str, Any]) -> Dict[str, Any]:
    index = resolve_track_indices(ctx.midi, [p["track"]])[0]
    source = ctx.midi.tracks[index]
    base_name = p["name_prefix"] or source.name or "Track"
    channels: Dict[int, List[Event]] = {}
    others: List[Event] = []
    for event in source.events:
        if event.is_channel:
            channels.setdefault(event.channel, []).append(event)
        else:
            others.append(event)
    if len(channels) <= 1:
        return {"splits": 0, "reason": "track uses %d channel(s)" % len(channels)}
    new_tracks: List[MidiTrack] = []
    for position, channel in enumerate(sorted(channels)):
        events = [ev.clone() for ev in channels[channel]]
        if p["keep_meta"] or position == 0:
            events.extend(ev.clone() for ev in others)
        track = MidiTrack(events, name="%s ch%d" % (base_name, channel + 1), encoding=ctx.encoding)
        track.sort_events()
        new_tracks.append(track)
    ctx.midi.tracks.pop(index)
    for offset, track in enumerate(new_tracks):
        ctx.midi.tracks.insert(index + offset, track)
    ctx.invalidate_all()
    return {"splits": len(new_tracks), "tracks": len(ctx.midi.tracks), "channels": sorted(channels)}


@register(
    "set_format",
    "设置 SMF 格式（0 单轨 / 1 多轨同步 / 2 多轨独立），转 0 时可自动合并",
    category="file",
    params=[
        Param("format", "int", required=True, help="0、1 或 2"),
        Param("merge", "bool", default=True, help="转 0 时合并所有轨为一条"),
    ],
    examples=[{"op": "set_format", "format": 0}],
)
def op_set_format(ctx: OpContext, p: Dict[str, Any]) -> Dict[str, Any]:
    value = int(p["format"])
    if value not in (0, 1, 2):
        raise SchemaError("format must be 0, 1 or 2: %r" % (value,))
    merged = 0
    previous = ctx.midi.format
    if value == 0 and p["merge"] and len(ctx.midi.tracks) > 1:
        events: List[Event] = []
        for track in ctx.midi.tracks:
            events.extend(ev.clone() for ev in track.events if not (ev.is_meta and ev.type_byte == 0x2F))
        track = MidiTrack(events, name=ctx.midi.tracks[0].name or "Merged", encoding=ctx.encoding)
        track.sort_events()
        merged = len(ctx.midi.tracks)
        ctx.midi.tracks = [track]
    ctx.midi.format = value
    ctx.invalidate_all()
    return {"format": value, "previous": previous, "merged_tracks": merged, "tracks": len(ctx.midi.tracks)}


@register(
    "set_division",
    "设置时间分辨率（PPQ 或 SMPTE），可选按比例重算所有 tick",
    category="file",
    params=[
        Param("ppq", "int", default=None, help="每四分音符 tick 数"),
        Param("smpte_fps", "float", default=None, help="SMPTE 帧率 24/25/29.97/30"),
        Param("ticks_per_frame", "int", default=80),
        Param("rescale", "bool", default=True, help="按时间等价重算所有 tick"),
    ],
    examples=[{"op": "set_division", "ppq": 960, "rescale": True}],
)
def op_set_division(ctx: OpContext, p: Dict[str, Any]) -> Dict[str, Any]:
    if p["ppq"] is None and p["smpte_fps"] is None:
        raise SchemaError("set_division needs ppq or smpte_fps")
    old = ctx.midi.division
    if p["smpte_fps"] is not None:
        new_division = Division(smpte_fps=float(p["smpte_fps"]), smpte_ticks_per_frame=int(p["ticks_per_frame"] or 80))
    else:
        new_division = Division(ppq=int(p["ppq"]))
    rescaled = 0
    if p["rescale"] and new_division != old:
        tempo_map = ctx.tempo_map
        for track in ctx.midi.tracks:
            for event in track.events:
                if old.is_smpte and new_division.is_smpte:
                    new_tick = int(round(event.tick * (new_division.ticks_per_second / old.ticks_per_second)))
                elif not old.is_smpte and not new_division.is_smpte:
                    new_tick = int(round(event.tick * (new_division.ppq / float(old.ppq))))
                elif not old.is_smpte and new_division.is_smpte:
                    seconds = tempo_map.tick_to_seconds(event.tick)
                    new_tick = int(round(seconds * float(new_division.ticks_per_second)))
                else:
                    # SMPTE -> PPQ: exact for the initial tempo, approximate if the tempo changes
                    seconds = event.tick / float(old.ticks_per_second)
                    beats = seconds * tempo_map.initial_bpm / 60.0
                    new_tick = int(round(beats * new_division.ppq))
                if new_tick != event.tick:
                    event.tick = new_tick
                    rescaled += 1
            track.sort_events()
    ctx.midi.division = new_division
    ctx.invalidate_all()
    return {
        "division": new_division.to_dict(),
        "previous": old.to_dict(),
        "rescaled_events": rescaled,
    }


@register(
    "repair",
    "修复常见问题：排序、去重、补齐缺失的 note_off、删除孤儿 note_off、规范 end_of_track",
    category="file",
    params=[
        Param("tracks", "int_list", default=None),
        Param("sort", "bool", default=True),
        Param("dedupe", "bool", default=False, help="删除完全重复的事件"),
        Param("fix_notes", "bool", default=True),
        Param("drop_empty_tracks", "bool", default=False),
        Param("normalize_eot", "bool", default=True),
    ],
    examples=[{"op": "repair", "dedupe": True}],
)
def op_repair(ctx: OpContext, p: Dict[str, Any]) -> Dict[str, Any]:
    indices = resolve_track_indices(ctx.midi, p["tracks"]) if p["tracks"] else list(range(len(ctx.midi.tracks)))
    added_offs = 0
    dropped_offs = 0
    deduped = 0
    sorted_tracks = 0
    for index in indices:
        track = ctx.midi.tracks[index]
        if p["sort"]:
            before = [id(ev) for ev in track.events]
            track.sort_events()
            if [id(ev) for ev in track.events] != before:
                sorted_tracks += 1
        if p["dedupe"]:
            seen = set()
            keep: List[Event] = []
            for event in track.events:
                key = event.fingerprint()
                if key in seen:
                    deduped += 1
                    continue
                seen.add(key)
                keep.append(event)
            track.events = keep
        if p["fix_notes"]:
            open_notes: Dict[Any, List[ChannelEvent]] = {}
            keep = []
            for event in track.events:
                if isinstance(event, ChannelEvent) and event.is_note_on:
                    open_notes.setdefault((event.channel, event.value1), []).append(event)
                    keep.append(event)
                elif isinstance(event, ChannelEvent) and event.is_note_off:
                    stack = open_notes.get((event.channel, event.value1))
                    if stack:
                        stack.pop(0 if ctx.pairing == "fifo" else -1)
                        keep.append(event)
                    else:
                        dropped_offs += 1
                else:
                    keep.append(event)
            end_tick = max((ev.tick for ev in keep), default=0)
            for (channel, pitch), stack in open_notes.items():
                for on_event in stack:
                    keep.append(ChannelEvent("note_off", channel, pitch, 0, max(end_tick, on_event.tick)))
                    added_offs += 1
            track.events = keep
            track.sort_events()
        if p["normalize_eot"]:
            track.normalize_end_of_track()
    if p["drop_empty_tracks"] and len(ctx.midi.tracks) > 1:
        keep_tracks = [t for t in ctx.midi.tracks if len(t.events) > 1 or any(ev.is_channel for ev in t.events)]
        if keep_tracks:
            ctx.midi.tracks = keep_tracks
    ctx.invalidate_all()
    return {
        "tracks": indices,
        "sorted_tracks": sorted_tracks,
        "deduped_events": deduped,
        "note_off_added": added_offs,
        "orphan_note_off_removed": dropped_offs,
        "tracks_after": len(ctx.midi.tracks),
    }
