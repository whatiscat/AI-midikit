"""Command line interface designed for AI agents.

Every command prints a single JSON envelope on stdout unless --text is given:

    {"ok": true, "command": "info", "file": "...", "data": {...}, "warnings": []}

Exit codes: 0 ok, 1 usage, 2 validation, 3 operation, 4 parse, 5 write.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import __version__
from .analysis import diff_files, event_rows, piano_roll, summarize
from .constants import DEFAULT_PPQ, note_name
from .errors import (
    EXIT_OK,
    EXIT_OP,
    EXIT_PARSE,
    EXIT_USAGE,
    EXIT_VALIDATION,
    MidiKitError,
    SchemaError,
    UsageError,
    exit_code_for,
)
from .events import DEFAULT_OPTS, DecodeOptions
from .jsonio import from_document, to_document
from .notes import extract_notes, parse_note_spec
from .ops import OPS, OpContext, describe_ops, get_op, validate_params
from .plan import apply_plan, describe_plan, normalize_plan
from .selectors import EventSelector, resolve_track_indices
from .smf import MidiFile
from .split import (
    DEFAULT_TEMPLATE,
    DEFAULT_TRACK_TEMPLATE,
    split_midi,
    write_parts,
)
from .jsonio import loads_relaxed
from .tempo import TempoMap, parse_time
from .validate import validate as validate_midi

PROG = "midikit"
DEFAULT_LIMIT = 500


# ---------------------------------------------------------------------------
# output helpers
# ---------------------------------------------------------------------------


class Output:
    """Collects the JSON envelope and writes it once."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.json_mode = getattr(args, "format", "json") != "text"
        self.pretty = not getattr(args, "compact", False)
        self.quiet = bool(getattr(args, "quiet", False))
        self.command = getattr(args, "command", "")
        self.payload: Dict[str, Any] = {"ok": True, "command": self.command}

    def set(self, **values: Any) -> None:
        self.payload.update(values)

    def add_warnings(self, warnings: Optional[Sequence[Dict[str, Any]]]) -> None:
        if warnings:
            self.payload.setdefault("warnings", []).extend(warnings)

    def emit(self, text_lines: Optional[Sequence[str]] = None, exit_code: int = EXIT_OK) -> int:
        if self.json_mode:
            indent = 2 if self.pretty else None
            stream = sys.stdout
            try:
                stream.write(json.dumps(self.payload, ensure_ascii=False, indent=indent, default=str))
                stream.write("\n")
            except UnicodeEncodeError:
                stream.write(json.dumps(self.payload, ensure_ascii=True, indent=indent, default=str))
                stream.write("\n")
        elif text_lines is not None and not self.quiet:
            for line in text_lines:
                sys.stdout.write(line + "\n")
        sys.stdout.flush()
        report = getattr(self, "report_path", None)
        if report:
            with open(report, "w", encoding="utf-8") as handle:
                json.dump(self.payload, handle, ensure_ascii=False, indent=2, default=str)
        return exit_code


def fail(output: Output, exc: BaseException) -> int:
    if isinstance(exc, MidiKitError):
        error = exc.to_dict()
    else:
        error = {"code": "E_INTERNAL", "message": "%s: %s" % (type(exc).__name__, exc)}
    output.payload["ok"] = False
    output.payload["error"] = error
    code = exit_code_for(exc)
    if output.json_mode:
        output.emit(exit_code=code)
    else:
        sys.stderr.write("error: %s\n" % error.get("message"))
        if error.get("details"):
            sys.stderr.write("details: %s\n" % json.dumps(error["details"], ensure_ascii=False))
    return code


# ---------------------------------------------------------------------------
# file helpers
# ---------------------------------------------------------------------------


def load_midi(path: str, args: argparse.Namespace) -> MidiFile:
    strict = bool(getattr(args, "strict", False))
    encoding = getattr(args, "encoding", None) or "auto"
    return MidiFile.read(path, strict=strict, encoding=encoding)


def decode_options(args: argparse.Namespace) -> DecodeOptions:
    return DecodeOptions(
        encoding=getattr(args, "encoding", None) or "auto",
        names=not getattr(args, "no_names", False),
        full_data=bool(getattr(args, "raw", False)),
    )


def parse_json_arg(text: str, label: str) -> Any:
    if text is None:
        return None
    if isinstance(text, (dict, list)):
        return text
    source = text
    if text.startswith("@"):
        with open(text[1:], "r", encoding="utf-8-sig") as handle:
            source = handle.read()
    try:
        return loads_relaxed(source)
    except ValueError as exc:
        raise SchemaError("%s is not valid JSON: %s" % (label, exc))


def resolve_output(args: argparse.Namespace, plan_options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Decide where the edited file goes: --output, --in-place or plan options."""
    plan_options = plan_options or {}
    output = getattr(args, "output", None) or plan_options.get("output")
    in_place = bool(getattr(args, "in_place", False) or plan_options.get("in_place", False))
    dry_run = bool(getattr(args, "dry_run", False) or plan_options.get("dry_run", False))
    backup = bool(getattr(args, "backup", False) or plan_options.get("backup", False))
    return {"output": output, "in_place": in_place, "dry_run": dry_run, "backup": backup}


def write_result(
    midi: MidiFile,
    args: argparse.Namespace,
    target: Dict[str, Any],
    source_path: Optional[str],
    output: Output,
) -> Dict[str, Any]:
    """Write the edited file (unless dry-run) and describe what happened."""
    running_status = bool(getattr(args, "running_status", False))
    note_off_zero = bool(getattr(args, "note_off_as_note_on_0", False))
    path = target["output"]
    if target["in_place"]:
        if not source_path:
            raise UsageError("--in-place needs a real input file")
        path = source_path
    info: Dict[str, Any] = {"dry_run": target["dry_run"], "path": path}
    if target["dry_run"] or not path:
        info["bytes"] = len(midi.to_bytes(running_status=running_status, note_off_as_note_on_0=note_off_zero))
        return info
    if target["backup"] and source_path and os.path.abspath(path) == os.path.abspath(source_path):
        backup_path = source_path + ".bak.mid"
        shutil.copyfile(source_path, backup_path)
        info["backup"] = backup_path
    midi.write(path, running_status=running_status, note_off_as_note_on_0=note_off_zero)
    info["bytes"] = os.path.getsize(path)
    return info


def require_source_file(args: argparse.Namespace) -> str:
    path = getattr(args, "input", None)
    if not path:
        raise UsageError("a MIDI file argument is required")
    return path


def _unit_to_ticks(tempo_map: TempoMap, value: Any, unit: str) -> int:
    """Convert a number in the CLI unit into ticks."""
    number = float(value)
    if unit == "ticks":
        return int(round(number))
    if unit == "beats":
        return int(round(number * tempo_map.division.ticks_per_beat))
    if unit == "seconds":
        return tempo_map.seconds_to_tick(number)
    if unit == "bars":
        numerator, denominator = tempo_map.time_signature_at(0)
        return int(round(number * tempo_map.bar_ticks(numerator, denominator)))
    raise SchemaError("unknown unit %r (use ticks, beats, seconds or bars)" % (unit,))


def _write_encoding(args: argparse.Namespace) -> str:
    value = getattr(args, "encoding", None)
    return "utf-8" if value in (None, "", "auto") else str(value)


def _run(midi: MidiFile, plan: List[Dict[str, Any]], args: argparse.Namespace, encoding: str) -> Dict[str, Any]:
    if not plan:
        return {"ok": True, "applied": 0, "total": 0, "ops": [], "failed": None}
    return apply_plan(midi, plan, encoding=encoding, pairing=getattr(args, "pairing", "lifo"),
                      strict=bool(getattr(args, "strict", False)))


def track_summary_rows(midi: MidiFile) -> List[Dict[str, Any]]:
    rows = []
    for index, track in enumerate(midi.tracks):
        rows.append(
            {
                "index": index,
                "name": track.name,
                "events": len(track.events),
                "channels": track.channels(),
                "programs": track.programs(),
                "duration_ticks": track.duration_ticks,
            }
        )
    return rows


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def cmd_info(args: argparse.Namespace, output: Output) -> int:
    path = require_source_file(args)
    midi = load_midi(path, args)
    opts = decode_options(args)
    data = summarize(midi, opts, path=path)
    if args.notes:
        extraction = extract_notes(midi)
        data["note_list"] = [n.to_dict(opts) for n in extraction.sorted()]
    output.set(file=path, data=data, warnings=midi.warnings)
    lines = [
        "%s: format %d, %s" % (path, midi.format, midi.division.describe()),
        "duration %d ticks / %s / bar %d:%d"
        % (
            data["duration_ticks"],
            data["duration_clock"],
            data["duration_bars"]["bar"],
            data["duration_bars"]["beat"],
        ),
        "tracks: %d, notes: %d, tempo %.3f bpm" % (len(midi.tracks), data["notes"]["count"], data["tempo"]["initial_bpm"]),
    ]
    for row in data["tracks"]:
        lines.append(
            "  [%d] %-24s events=%-5d notes=%-5d ch=%s prog=%s"
            % (
                row["index"],
                (row["name"] or "-")[:24],
                row["events"],
                row["notes"],
                row["channels_display"] or "-",
                row["programs"] or "-",
            )
        )
    output.add_warnings(midi.warnings)
    return output.emit(lines)


def cmd_tracks(args: argparse.Namespace, output: Output) -> int:
    path = require_source_file(args)
    midi = load_midi(path, args)
    rows = []
    for index, track in enumerate(midi.tracks):
        entry = track.to_dict(decode_options(args), include_events=False)
        entry["index"] = index
        rows.append(entry)
    output.set(file=path, data={"track_count": len(rows), "tracks": rows}, warnings=midi.warnings)
    lines = ["[%d] %s" % (row["index"], row["name"] or "(unnamed)") for row in rows]
    return output.emit(lines)


def cmd_dump(args: argparse.Namespace, output: Output) -> int:
    path = require_source_file(args)
    midi = load_midi(path, args)
    opts = decode_options(args)
    selector = {}
    for key in ("type", "channel", "tick_min", "tick_max", "note_min", "note_max", "meta_type", "text_contains"):
        value = getattr(args, key.replace("-", "_"), None)
        if value is not None:
            selector[key] = value
    tracks = resolve_track_indices(midi, args.track) if args.track else None
    rows = event_rows(midi, opts, tracks=tracks)
    if selector:
        sel = EventSelector(selector)
        keep = []
        for row in rows:
            event = midi.tracks[row["track"]].events[row["index"]]
            if sel.matches(event, row["track"], row["index"]):
                keep.append(row)
        rows = sel.apply_limits(keep)
    offset = int(args.offset or 0)
    limit = int(args.limit) if args.limit is not None else DEFAULT_LIMIT
    window = rows[offset : offset + limit]
    if getattr(args, "format", "json") == "jsonl":
        for row in window:
            sys.stdout.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        sys.stdout.flush()
        return EXIT_OK
    output.set(
        file=path,
        data={
            "total_events": len(rows),
            "offset": offset,
            "limit": limit,
            "returned": len(window),
            "duration_ticks": midi.duration_ticks(),
            "events": window,
        },
        warnings=midi.warnings,
    )
    lines = []
    for row in window:
        event = midi.tracks[row["track"]].events[row["index"]]
        lines.append("[%s] %6d bar %3d:%-2d %s" % (row["id"], row["tick"], row["bar"], row["beat"], event.describe(opts)))
    return output.emit(lines)


def cmd_notes(args: argparse.Namespace, output: Output) -> int:
    path = require_source_file(args)
    midi = load_midi(path, args)
    opts = decode_options(args)
    from .tempo import TempoMap

    tm = TempoMap.from_midi(midi)
    tracks = resolve_track_indices(midi, args.track) if args.track else None
    extraction = extract_notes(midi, tracks, pairing=args.pairing)
    unit = args.unit or "ticks"
    notes = extraction.sorted()
    if args.limit:
        notes = notes[: int(args.limit)]
    payload = {
        "count": len(extraction.notes),
        "unterminated": len(extraction.unterminated_note_ons),
        "orphan_note_offs": len(extraction.orphan_note_offs),
        "unit": unit,
        "notes": [n.to_dict(opts, unit=unit, tempo_map=tm) for n in notes],
    }
    output.set(file=path, data=payload, warnings=midi.warnings)
    lines = [n.describe(opts, tempo_map=tm) for n in notes]
    if len(notes) != len(extraction.notes):
        lines.append("... %d more note(s)" % (len(extraction.notes) - len(notes)))
    return output.emit(lines)


def cmd_stats(args: argparse.Namespace, output: Output) -> int:
    path = require_source_file(args)
    midi = load_midi(path, args)
    opts = decode_options(args)
    from .tempo import TempoMap

    tm = TempoMap.from_midi(midi)
    data = summarize(midi, opts, tm, path=path)
    extraction = extract_notes(midi)
    hist = data["notes"]["pitch_histogram"]
    top = sorted(((int(k), v) for k, v in hist.items()), key=lambda item: -item[1])[:12]
    data["top_pitches"] = [{"note": p, "pitch": note_name(p), "count": c} for p, c in top]
    data["note_density_per_bar"] = round(
        len(extraction.notes) / max(1, data["duration_bars"]["bar"]), 4
    )
    durations = [n.duration for n in extraction.notes]
    if durations:
        durations_sorted = sorted(durations)
        data["note_duration_ticks"] = {
            "min": durations_sorted[0],
            "max": durations_sorted[-1],
            "median": durations_sorted[len(durations_sorted) // 2],
            "mean": round(sum(durations) / float(len(durations)), 3),
        }
    output.set(file=path, data=data, warnings=midi.warnings)
    lines = [
        "notes=%d  polyphony<=%d  duration=%s" % (data["notes"]["count"], data["notes"]["max_polyphony"], data["duration_clock"]),
        "pitches %s..%s  velocity %s..%s (mean %s)"
        % (
            data["notes"]["pitch_min_name"],
            data["notes"]["pitch_max_name"],
            data["notes"]["velocity_min"],
            data["notes"]["velocity_max"],
            data["notes"]["velocity_mean"],
        ),
        "channels=%s programs=%s" % (data["channels_used"], data["programs_used"]),
        "tempo %.3f bpm (%d change(s)), time signatures: %s"
        % (data["tempo"]["initial_bpm"], data["tempo"]["change_count"], data["time_signatures"]),
    ]
    return output.emit(lines)


def cmd_pianoroll(args: argparse.Namespace, output: Output) -> int:
    path = require_source_file(args)
    midi = load_midi(path, args)
    from .tempo import TempoMap

    tm = TempoMap.from_midi(midi)
    start = int(args.start or 0)
    end = int(args.end) if args.end is not None else None
    tracks = resolve_track_indices(midi, args.track) if args.track else None
    text = piano_roll(
        midi,
        start=start,
        end=end,
        width=int(args.width or 96),
        height=int(args.height or 24),
        tracks=tracks,
        tempo_map=tm,
        grid_ticks=float(args.grid) if args.grid else None,
        show_velocity=bool(args.velocity),
        pitch_min=args.pitch_min,
        pitch_max=args.pitch_max,
    )
    if getattr(args, "format", "json") == "text":
        return output.emit(text.splitlines())
    output.set(
        file=path,
        data={"start": start, "end": end or midi.duration_ticks(), "width": args.width, "height": args.height},
        text=text,
    )
    return output.emit(text.splitlines())


def cmd_validate(args: argparse.Namespace, output: Output) -> int:
    path = require_source_file(args)
    midi = load_midi(path, args)
    report = validate_midi(midi, tracks=args.track, check_notes=not args.no_notes)
    output.set(file=path, data=report)
    lines = [
        "%s: %s (errors=%d warnings=%d info=%d)"
        % (
            path,
            "OK" if report["ok"] else "INVALID",
            report["counts"]["error"],
            report["counts"]["warning"],
            report["counts"]["info"],
        )
    ]
    for issue in report["issues"]:
        location = ""
        if issue.get("track") is not None:
            location = " [track %s%s]" % (issue["track"], ", tick %s" % issue["tick"] if issue.get("tick") is not None else "")
        lines.append("  %-7s %-22s %s%s" % (issue["severity"].upper(), issue["code"], issue["message"], location))
    code = EXIT_OK if report["ok"] else EXIT_VALIDATION
    if args.strict and report["counts"]["warning"]:
        code = EXIT_VALIDATION
    return output.emit(lines, exit_code=code)


def cmd_diff(args: argparse.Namespace, output: Output) -> int:
    a = MidiFile.read(args.file_a, strict=args.strict, encoding=args.encoding or "auto")
    b = MidiFile.read(args.file_b, strict=args.strict, encoding=args.encoding or "auto")
    data = diff_files(a, b, os.path.basename(args.file_a), os.path.basename(args.file_b))
    output.set(data=data, warnings=(a.warnings + b.warnings))
    lines = [
        "identical bytes: %s" % data["identical_bytes"],
        "header changes: %s" % (json.dumps(data["header"]) if data["header"] else "none"),
        "events added: %d, removed: %d" % (data["events"]["added_count"], data["events"]["removed_count"]),
        "notes added: %d, removed: %d" % (data["notes"]["added_count"], data["notes"]["removed_count"]),
    ]
    return output.emit(lines)


def cmd_split(args: argparse.Namespace, output: Output) -> int:
    path = require_source_file(args)
    midi = load_midi(path, args)
    channels = None
    if args.channels:
        try:
            channels = [int(part) for part in str(args.channels).replace(",", " ").split()]
        except ValueError:
            raise UsageError("--channels expects channel numbers like 0,1,9 (got %r)" % (args.channels,))
    keep_conductor = None
    if args.no_conductor:
        keep_conductor = False
    elif args.with_conductor:
        keep_conductor = True
    parts = split_midi(
        midi,
        by=args.by,
        channels=channels,
        tracks=args.track,
        keep_conductor=keep_conductor,
        keep_markers=not args.no_markers,
        keep_extra_chunks=not args.drop_extra_chunks,
        output_format=args.midi_format,
        min_events=int(args.min_events or 1),
    )
    stem = os.path.splitext(os.path.basename(path))[0]
    template = args.name_template or (
        DEFAULT_TRACK_TEMPLATE if str(args.by).lower().startswith("track") else DEFAULT_TEMPLATE
    )
    rows = write_parts(
        parts,
        args.out_dir,
        template=template,
        stem=stem,
        dry_run=bool(args.dry_run),
        running_status=bool(getattr(args, "running_status", False)),
        note_off_as_note_on_0=bool(getattr(args, "note_off_as_note_on_0", False)),
    )
    checks = []
    if not args.dry_run:
        for part in parts:
            if part.path:
                report = validate_midi(MidiFile.read(part.path), normalize=False)
                checks.append({"path": part.path, "ok": report["ok"], "counts": report["counts"]})
    ok = all(item["ok"] for item in checks)
    output.set(
        file=path,
        data={
            "input": path,
            "out_dir": os.path.abspath(args.out_dir),
            "by": args.by,
            "dry_run": bool(args.dry_run),
            "count": len(rows),
            "channels_in_file": sorted({ev.channel for _t, _i, ev in midi.iter_events() if ev.is_channel}),
            "parts": rows,
            "validation": checks,
        },
        warnings=midi.warnings,
    )
    lines = ["split %s by %s -> %d file(s) in %s" % (path, args.by, len(rows), args.out_dir)]
    for row in rows:
        lines.append(
            "  ch=%-4s %-28s notes=%-5d events=%-5d %s"
            % (
                "-" if row["channel"] is None else row["channel_display"],
                row["name"][:28],
                row["notes"],
                row["events"],
                row["filename"],
            )
        )
    return output.emit(lines, exit_code=EXIT_OK if ok else EXIT_VALIDATION)


def cmd_to_json(args: argparse.Namespace, output: Output) -> int:
    path = require_source_file(args)
    midi = load_midi(path, args)
    doc = to_document(
        midi,
        decode_options(args),
        mode=args.mode,
        include_summary=bool(args.summary),
    )
    target = args.output
    if target:
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(doc, handle, ensure_ascii=False, indent=2, default=str)
        output.set(file=path, output=target, data={"tracks": len(doc["tracks"]), "documents": 1})
    else:
        output.set(file=path, data=doc)
    output.add_warnings(midi.warnings)
    return output.emit([])


def cmd_from_json(args: argparse.Namespace, output: Output) -> int:
    path = require_source_file(args)
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8-sig") as handle:
            text = handle.read()
    else:
        text = path
    doc = parse_json_arg(text, "document")
    midi = from_document(doc, encoding=args.encoding if args.encoding not in (None, "auto") else "utf-8")
    target = resolve_output(args)
    if not target["output"] and not target["in_place"] and not target["dry_run"]:
        raise UsageError("from-json needs --output PATH (or --dry-run)")
    info = write_result(midi, args, target, None, output)
    report = validate_midi(midi, normalize=True)
    output.set(
        output_path=info.get("path"),
        data={
            "written": info,
            "format": midi.format,
            "division": midi.division.to_dict(),
            "tracks": len(midi.tracks),
            "notes": len(extract_notes(midi).notes),
            "events": sum(len(t.events) for t in midi.tracks),
            "validation": {"ok": report["ok"], "counts": report["counts"]},
        },
    )
    return output.emit(["wrote %s" % info.get("path")])


def cmd_create(args: argparse.Namespace, output: Output) -> int:
    setup: List[Dict[str, Any]] = []
    if args.tempo is not None:
        setup.append({"op": "set_tempo", "bpm": float(args.tempo), "at": 0, "track": 0})
    if args.time_signature:
        numerator, denominator = _parse_fraction(args.time_signature)
        setup.append({"op": "set_time_signature", "numerator": numerator, "denominator": denominator, "at": 0, "track": 0})
    if args.key:
        setup.append({"op": "set_key_signature", "key": args.key, "at": 0, "track": 0})
    if args.name:
        setup.append({"op": "set_track_name", "track": 0, "name": args.name})

    midi = MidiFile(format=int(args.midi_format), division=int(args.ppq))
    midi.new_track(args.name or "Conductor")
    for spec in list(args.track or []):
        name, channel, program = _parse_track_spec(spec)
        index = len(midi.tracks)
        midi.new_track(name or ("Track %d" % index))
        if program is not None:
            setup.append({"op": "set_instrument", "program": int(program), "channel": channel, "track": index})

    if args.spec:
        spec_doc = parse_json_arg(open(args.spec, "r", encoding="utf-8-sig").read(), "spec")
        if isinstance(spec_doc, dict) and "ops" in spec_doc:
            setup = normalize_plan(spec_doc) + setup
        elif isinstance(spec_doc, dict) and "tracks" in spec_doc:
            midi = from_document(spec_doc, encoding=_write_encoding(args))

    encoding = _write_encoding(args)
    setup_result = _run(midi, setup, args, encoding)
    if not setup_result["ok"]:
        output.payload.update({"plan": setup_result, "ok": False, "error": setup_result["failed"]["error"]})
        return output.emit(exit_code=EXIT_OP)

    # the tempo map now exists, so beat/bar/second units can be resolved
    tempo_map = TempoMap.from_midi(midi)
    notes: List[Any] = list(args.note or [])
    if args.notes_file:
        extra = parse_json_arg(open(args.notes_file, "r", encoding="utf-8-sig").read(), "notes file")
        notes.extend(extra.get("notes", extra) if isinstance(extra, dict) else extra)
    if args.notes_json:
        payload = parse_json_arg(args.notes_json, "notes")
        notes.extend(payload if isinstance(payload, list) else payload.get("notes", []))
    plan: List[Dict[str, Any]] = []
    if notes:
        grouped: Dict[int, List[Any]] = {}
        default_channel = int(args.channel or 0)
        default_track = int(args.note_track or 0)
        for item in notes:
            if isinstance(item, str):
                note = parse_note_spec(
                    item,
                    unit=args.unit,
                    default_track=default_track,
                    default_channel=default_channel,
                    tempo_map=tempo_map,
                )
                grouped.setdefault(note.track, []).append(
                    {
                        "pitch": note.pitch,
                        "start": note.start,
                        "duration": note.duration,
                        "velocity": note.velocity,
                        "channel": note.channel,
                    }
                )
            elif isinstance(item, dict):
                entry = dict(item)
                if args.unit != "ticks":
                    for key in ("start", "at", "tick", "duration", "length", "end"):
                        if entry.get(key) is not None:
                            entry[key] = _unit_to_ticks(tempo_map, entry[key], args.unit)
                track_index = int(entry.get("track", default_track))
                grouped.setdefault(track_index, []).append(entry)
            else:
                raise SchemaError("each note must be a string like 'C4 0 1 100' or an object")
        for track_index, items in sorted(grouped.items()):
            plan.append(
                {
                    "op": "insert_notes",
                    "track": track_index,
                    "unit": "ticks",
                    "channel": default_channel,
                    "notes": items,
                }
            )
    for spec in args.meta or []:
        payload = parse_json_arg(spec, "--meta")
        events = payload if isinstance(payload, list) else [payload]
        plan.append({"op": "insert_events", "track": int(args.note_track or 0), "events": events})

    result = _run(midi, plan, args, encoding)
    if not result["ok"]:
        output.payload.update({"plan": result, "ok": False, "error": result["failed"]["error"]})
        return output.emit(exit_code=EXIT_OP)
    combined = {
        "applied": setup_result["applied"] + result["applied"],
        "total": setup_result["total"] + result["total"],
        "ops": setup_result["ops"] + result["ops"],
    }
    result = combined
    target = resolve_output(args)
    if not target["output"] and not target["dry_run"]:
        raise UsageError("create needs --output PATH (or --dry-run)")
    info = write_result(midi, args, target, None, output)
    report = validate_midi(midi, normalize=True)
    output.set(
        output_path=info.get("path"),
        data={
            "written": info,
            "format": midi.format,
            "division": midi.division.to_dict(),
            "tracks": len(midi.tracks),
            "notes": len(extract_notes(midi).notes),
            "events": sum(len(t.events) for t in midi.tracks),
            "plan": {"applied": result["applied"], "ops": [op.get("op") for op in result["ops"]]},
            "validation": {"ok": report["ok"], "counts": report["counts"]},
        },
    )
    return output.emit(["created %s" % info.get("path")])


def cmd_apply(args: argparse.Namespace, output: Output) -> int:
    plan_doc: Any = None
    if args.plan:
        with open(args.plan, "r", encoding="utf-8-sig") as handle:
            plan_doc = json.load(handle)
    elif args.ops:
        plan_doc = parse_json_arg(args.ops, "--ops")
    elif args.stdin:
        plan_doc = json.loads(sys.stdin.read())
    else:
        plan_doc = {"ops": []}
    options: Dict[str, Any] = {}
    if isinstance(plan_doc, dict):
        options = dict(plan_doc.get("options", {}) or {})
        # a plan may carry input/output/dry_run/backup at the top level
        for key in ("output", "dry_run", "backup", "in_place"):
            if key not in options and plan_doc.get(key) is not None:
                options[key] = plan_doc[key]
    input_path = args.input or (plan_doc.get("input") if isinstance(plan_doc, dict) else None)
    if not input_path:
        raise UsageError("apply needs a MIDI file (positional or plan 'input')")
    midi = load_midi(input_path, args)
    result = describe_plan(plan_doc)
    for item in result:
        if not item["known"]:
            raise SchemaError("unknown op %r in plan" % item["op"], available=sorted(OPS))
        if item.get("unknown_params"):
            raise SchemaError(
                "op %r has unknown parameter(s): %s" % (item["op"], ", ".join(item["unknown_params"])),
                op=item["op"],
            )
    execution = apply_plan(midi, plan_doc, encoding=args.encoding if args.encoding not in (None, "auto") else "utf-8",
                           pairing=args.pairing, strict=bool(args.strict))
    target = resolve_output(args, options)
    if not execution["ok"]:
        output.payload.update({"plan": execution, "ok": False, "error": execution["failed"]["error"]})
        output.payload["failed_op"] = execution["failed"]["op"]
        return output.emit(exit_code=EXIT_OP)
    if not target["output"] and not target["in_place"] and not target["dry_run"]:
        raise UsageError("apply needs --output PATH, --in-place or --dry-run")
    info = write_result(midi, args, target, input_path, output)
    report = validate_midi(midi, normalize=True)
    output.set(
        file=input_path,
        output_path=info.get("path"),
        data={
            "written": info,
            "ops": execution["ops"],
            "ops_applied": execution["applied"],
            "validation": {"ok": report["ok"], "counts": report["counts"], "issues": report["issues"][:20]},
            "tracks": len(midi.tracks),
        },
        warnings=midi.warnings,
    )
    lines = ["applied %d op(s) -> %s" % (execution["applied"], info.get("path") or "(dry run)")]
    for item in execution["ops"]:
        lines.append("  %s" % json.dumps(item, ensure_ascii=False)[:200])
    return output.emit(lines)


def cmd_op(args: argparse.Namespace, output: Output) -> int:
    op = get_op(args.op_name)
    params: Dict[str, Any] = {}
    for param in op.params:
        attr = "p_" + param.name
        value = getattr(args, attr, None)
        if value is None:
            continue
        params[param.name] = value
    midi = load_midi(args.input, args)
    ctx = OpContext(midi, encoding=args.encoding if args.encoding not in (None, "auto") else "utf-8", pairing=args.pairing)
    values = validate_params(op, params, ctx)
    report = op.func(ctx, values) or {}
    target = resolve_output(args)
    if not target["output"] and not target["in_place"] and not target["dry_run"]:
        raise UsageError("op %s needs --output PATH, --in-place or --dry-run" % op.name)
    info = write_result(midi, args, target, args.input, output)
    check = validate_midi(midi, normalize=True)
    output.set(
        file=args.input,
        output_path=info.get("path"),
        data={
            "op": op.name,
            "params": values,
            "result": report,
            "written": info,
            "validation": {"ok": check["ok"], "counts": check["counts"]},
        },
        warnings=midi.warnings,
    )
    lines = ["%s: %s" % (op.name, json.dumps(report, ensure_ascii=False)[:300])]
    lines.append("written: %s" % (info.get("path") or "(dry run)"))
    return output.emit(lines)


def cmd_ops(args: argparse.Namespace, output: Output) -> int:
    if args.describe:
        op = get_op(args.describe)
        output.set(data=op.to_dict())
        return output.emit([json.dumps(op.to_dict(), ensure_ascii=False, indent=2)])
    items = describe_ops(args.category)
    output.set(data={"count": len(items), "categories": sorted(set(i["category"] for i in items)), "ops": items})
    lines = []
    for item in items:
        params = ", ".join(
            "%s%s%s" % (p["name"], "=" + str(p.get("default")) if p.get("default") is not None else "", "" if p.get("required") else "?")
            for p in item["params"]
        )
        lines.append("[%s] %-24s %s  (%s)" % (item["category"], item["name"], item["summary"], params))
    return output.emit(lines)


SCHEMA_TOPICS = ("plan", "op", "document", "event", "note", "selector", "errors", "exit-codes")


def cmd_schema(args: argparse.Namespace, output: Output) -> int:
    topic = args.topic or "all"
    topics = list(SCHEMA_TOPICS) if topic == "all" else [topic]
    data: Dict[str, Any] = {}
    for item in topics:
        if item == "plan":
            data["plan"] = {
                "shape": {"ops": [{"op": "<name>", "...": "op parameters"}]},
                "also_accepts": "a bare array of op objects, or a single op object",
                "input_output": "the plan may set 'input' and 'output' (plus options.dry_run/backup/in_place)",
                "execution": "operations run in order against the in-memory file; nothing is written until every op succeeds",
                "addressing": "tracks are indexed from 0; events are addressed by track index plus event index or by selector",
            }
        elif item == "op":
            data["op"] = {"note": "run 'ops --json' or 'ops --describe NAME'", "count": len(OPS), "names": sorted(OPS)}
        elif item == "document":
            data["document"] = {
                "shape": {
                    "format": "0 | 1 | 2",
                    "division": {"type": "ppq", "ticks_per_beat": 480},
                    "tracks": [{"name": "Piano", "events": ["<event>"], "notes": ["<note>"]}],
                },
                "notes": "a track merges 'events' (lossless) and 'notes' (musical); "
                         "'notes_view' is informational and ignored on load",
            }
        elif item == "event":
            data["event"] = {
                "common": ["tick", "type", "channel"],
                "types": ["note_on", "note_off", "poly_aftertouch", "control_change", "program_change",
                          "channel_aftertouch", "pitch_bend", "meta", "sysex", "escape", "system"],
                "channel_fields": {"note_on": ["note|pitch", "velocity"], "control_change": ["control", "value"],
                                   "program_change": ["program|program_name"], "pitch_bend": ["value|bend|semitones"]},
                "meta": "{\"type\":\"meta\",\"meta_type\":\"track_name|text|lyric|marker|set_tempo|time_signature|key_signature|...\", ...}",
                "sysex": "{\"type\":\"sysex\",\"data_hex\":\"f0 41 ... f7\"} or {\"type\":\"escape\",\"data_hex\":\"...\"}",
            }
        elif item == "note":
            data["note"] = {
                "fields": {"pitch": "60 | 'C4' (required)", "start": "tick (default 0)",
                           "duration": "ticks (or end)", "velocity": "1-127 (default 100)",
                           "channel": "0-15 (default 0)", "track": "track index (default 0)"},
                "string_form": "'C4 0 480 100 0 1' = pitch start duration [velocity] [channel] [track]",
            }
        elif item == "selector":
            data["selector"] = {
                "event": ["track", "index", "type", "channel", "tick_min", "tick_max", "note_min", "note_max",
                          "velocity", "velocity_min", "velocity_max", "control", "program", "value",
                          "value_min", "value_max", "meta_type", "text_contains", "text_regex", "status",
                          "limit", "which", "not_type"],
                "event_notes": "control accepts a number or a name (pan, sustain); value* applies to "
                               "control_change / pitch_bend / program_change / aftertouch, notes use velocity*",
                "note": ["track", "channel", "pitch", "note_min", "note_max", "velocity_min", "velocity_max",
                         "start_min", "start_max", "end_min", "end_max", "duration_min", "duration_max",
                         "limit", "which", "unterminated"],
                "types": sorted(["note", "notes", "channel", "cc", "meta", "sysex", "text", "note_on", "note_off",
                                 "control_change", "program_change", "pitch_bend", "poly_aftertouch", "channel_aftertouch"]),
            }
        elif item == "errors":
            data["errors"] = {
                "E_USAGE": "bad arguments or unknown op",
                "E_SCHEMA": "wrong JSON shape / unknown parameter or selector field",
                "E_PARSE": "not a valid Standard MIDI File",
                "E_OP": "operation could not be applied (e.g. selector matched nothing)",
                "E_VALIDATION": "strict validation failed",
                "E_WRITE": "filesystem error",
                "E_NOT_FOUND": "selector matched nothing where something was required",
                "envelope": {"ok": False, "error": {"code": "E_...", "message": "...", "details": {}}},
            }
        elif item == "exit-codes":
            data["exit-codes"] = {"0": "ok", "1": "usage/schema error", "2": "validation failed",
                                  "3": "operation failed", "4": "parse failed", "5": "write failed", "70": "internal error"}
    output.set(data=data)
    return output.emit([json.dumps(data, ensure_ascii=False, indent=2)])


def cmd_selftest(args: argparse.Namespace, output: Output) -> int:
    from .events import ChannelEvent, MetaEvent

    checks: List[Dict[str, Any]] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": bool(condition), "detail": detail})

    from .bytestream import vlq_bytes

    check("vlq 0", vlq_bytes(0) == b"\x00")
    check("vlq 127", vlq_bytes(127) == b"\x7f")
    check("vlq 128", vlq_bytes(128) == b"\x81\x00")
    check("vlq max", vlq_bytes(0x0FFFFFFF) == b"\xff\xff\xff\x7f")
    midi = MidiFile(format=1, division=480)
    track = midi.new_track("T")
    track.add(MetaEvent.make_tempo(0, bpm=120))
    track.add(ChannelEvent("note_on", 0, 60, 100, 0))
    track.add(ChannelEvent("note_off", 0, 60, 64, 480))
    data = midi.to_bytes()
    reread = MidiFile.read_bytes(data)
    check("round trip events", [e.fingerprint() for _t, _i, e in reread.iter_events()] ==
          [e.fingerprint() for _t, _i, e in midi.iter_events()])
    check("idempotent bytes", reread.to_bytes() == data)
    check("running status round trip", MidiFile.read_bytes(midi.to_bytes(running_status=True)).to_bytes() == data)
    check("format inspection", reread.format == 1 and reread.division.ppq == 480)
    result = apply_plan(midi, [{"op": "transpose", "semitones": 12}])
    check("apply plan", result["ok"])
    notes = extract_notes(midi).notes
    check("transposed note", notes and notes[0].pitch == 72, str([n.pitch for n in notes]))
    report = validate_midi(midi, normalize=True)
    check("validates clean", report["ok"], json.dumps(report["counts"]))
    from .smf import Division as _Division

    smpte = MidiFile(format=0, division=_Division(smpte_fps=25, smpte_ticks_per_frame=40))
    smpte.new_track("S")
    check("smpte round trip", MidiFile.read_bytes(smpte.to_bytes()).division.is_smpte)
    ok = all(item["ok"] for item in checks)
    output.set(data={"ok": ok, "checks": checks, "version": __version__})
    lines = ["%-28s %s" % (item["check"], "ok" if item["ok"] else "FAIL: " + item["detail"]) for item in checks]
    lines.append("%d/%d checks passed" % (sum(1 for item in checks if item["ok"]), len(checks)))
    return output.emit(lines, exit_code=EXIT_OK if ok else EXIT_OP)


def cmd_version(args: argparse.Namespace, output: Output) -> int:
    output.set(data={"version": __version__, "python": sys.version.split()[0], "command": "version"})
    return output.emit(["%s %s" % (PROG, __version__)])


# ---------------------------------------------------------------------------
# small parsers
# ---------------------------------------------------------------------------


def _parse_fraction(text: str) -> Tuple[int, int]:
    parts = str(text).split("/")
    if len(parts) != 2:
        raise UsageError("time signature must look like 4/4 (got %r)" % (text,))
    return int(parts[0]), int(parts[1])


def _parse_track_spec(spec: str) -> Tuple[Optional[str], int, Optional[int]]:
    """'Piano:0:0' -> (name, channel, program)."""
    parts = str(spec).split(":")
    name = parts[0] or None
    channel = int(parts[1]) if len(parts) > 1 and parts[1] != "" else 0
    program = int(parts[2]) if len(parts) > 2 and parts[2] != "" else None
    return name, channel, program


def parse_time_arg(value: Optional[str], tempo_map=None, default_unit: str = "tick") -> Optional[int]:
    if value is None:
        return None
    return parse_time(value, tempo_map, default_unit)


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # pragma: no cover - argparse plumbing
        raise UsageError(message)


def _add_option(parser: argparse.ArgumentParser, *flags: str, **kwargs: Any) -> None:
    """Add an option unless an op parameter already claimed the same flag."""
    existing = getattr(parser, "_option_string_actions", {})
    if any(flag in existing for flag in flags):
        return
    parser.add_argument(*flags, **kwargs)


def add_output_options(parser: argparse.ArgumentParser, write: bool = False) -> None:
    _add_option(parser, "--format", choices=["json", "text", "jsonl"], default="json",
                help="output format (json is the default so agents can parse it)")
    _add_option(parser, "--json", dest="format", action="store_const", const="json", help="shorthand for --format json")
    _add_option(parser, "--text", dest="format", action="store_const", const="text", help="human readable output")
    _add_option(parser, "--compact", action="store_true", help="single line JSON")
    _add_option(parser, "--quiet", action="store_true")
    _add_option(parser, "--report", help="also write the JSON envelope to this file")
    _add_option(parser, "--encoding", default="auto", help="text encoding for meta text (auto/utf-8/latin-1/...)")
    _add_option(parser, "--strict", action="store_true", help="strict parsing / stricter validation")
    if write:
        _add_option(parser, "-o", "--output", help="output MIDI path")
        _add_option(parser, "--in-place", action="store_true", help="overwrite the input file")
        _add_option(parser, "--backup", action="store_true", help="keep a .bak.mid copy when overwriting in place")
        _add_option(parser, "--dry-run", action="store_true", help="report what would happen without writing")
        _add_option(parser, "--running-status", action="store_true", help="write channel messages with running status")
        _add_option(parser, "--note-off-as-note-on-0", action="store_true",
                    help="write note-offs as note-on with velocity 0")


def add_input_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--track", type=int, action="append", help="restrict to a track (repeatable)")
    parser.add_argument("--pairing", choices=["lifo", "fifo"], default="lifo", help="same-pitch note pairing")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog=PROG, description="Standard MIDI File 1.1 toolkit for AI agents")
    parser.add_argument("--version", action="version", version="%s %s" % (PROG, __version__))
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    p = sub.add_parser("info", help="file header, tracks, tempo, notes overview")
    p.add_argument("input")
    p.add_argument("--notes", action="store_true", help="include the full note list")
    add_output_options(p)
    p.set_defaults(func=cmd_info, command="info")

    p = sub.add_parser("tracks", help="list tracks")
    p.add_argument("input")
    add_output_options(p)
    p.set_defaults(func=cmd_tracks, command="tracks")

    p = sub.add_parser("dump", help="dump events with track/index addressing")
    p.add_argument("input")
    add_input_options(p)
    p.add_argument("--type", help="event type filter")
    p.add_argument("--channel", type=int)
    p.add_argument("--tick-min", type=int, dest="tick_min")
    p.add_argument("--tick-max", type=int, dest="tick_max")
    p.add_argument("--note-min", type=int, dest="note_min")
    p.add_argument("--note-max", type=int, dest="note_max")
    p.add_argument("--meta-type", dest="meta_type")
    p.add_argument("--text-contains", dest="text_contains")
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    p.add_argument("--raw", action="store_true", help="include raw payload bytes")
    p.add_argument("--no-names", action="store_true", help="omit pitch/cc/program names")
    add_output_options(p)
    p.set_defaults(func=cmd_dump, command="dump")

    p = sub.add_parser("notes", help="list notes with bar/beat and seconds")
    p.add_argument("input")
    add_input_options(p)
    p.add_argument("--unit", choices=["ticks", "beats", "seconds"], default="ticks")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--no-names", action="store_true")
    add_output_options(p)
    p.set_defaults(func=cmd_notes, command="notes")

    p = sub.add_parser("stats", help="musical statistics")
    p.add_argument("input")
    add_output_options(p)
    p.set_defaults(func=cmd_stats, command="stats")

    p = sub.add_parser("pianoroll", help="text piano roll view")
    p.add_argument("input")
    add_input_options(p)
    p.add_argument("--start", default=0)
    p.add_argument("--end")
    p.add_argument("--width", type=int, default=96)
    p.add_argument("--height", type=int, default=24)
    p.add_argument("--grid", type=float, help="grid ticks for the background lines")
    p.add_argument("--velocity", action="store_true", help="show velocity digits at note starts")
    p.add_argument("--pitch-min", dest="pitch_min", type=int, help="lowest pitch row to show")
    p.add_argument("--pitch-max", dest="pitch_max", type=int, help="highest pitch row to show")
    add_output_options(p)
    p.set_defaults(func=cmd_pianoroll, command="pianoroll")

    p = sub.add_parser("validate", help="check SMF 1.1 conformance")
    p.add_argument("input")
    add_input_options(p)
    p.add_argument("--no-notes", action="store_true", help="skip note pairing checks")
    add_output_options(p)
    p.set_defaults(func=cmd_validate, command="validate")

    p = sub.add_parser("diff", help="semantic diff between two files")
    p.add_argument("file_a")
    p.add_argument("file_b")
    add_output_options(p)
    p.set_defaults(func=cmd_diff, command="diff")

    p = sub.add_parser("split", help="split into one standalone file per channel (or per track)")
    p.add_argument("input")
    p.add_argument("--out-dir", dest="out_dir", required=True, help="directory for the produced files")
    p.add_argument("--by", choices=["channel", "track"], default="channel", help="split axis (default channel)")
    p.add_argument("--channels", help="only these channels, e.g. 0,1,9")
    p.add_argument("--track", type=int, action="append", help="track index when --by track (repeatable)")
    p.add_argument("--midi-format", dest="midi_format", type=int, choices=[0, 1],
                   help="format of the produced files (default: 1 with a conductor track, 0 without)")
    p.add_argument("--with-conductor", action="store_true", help="keep tempo/time signature/key in each file")
    p.add_argument("--no-conductor", action="store_true", help="drop the tempo map from the produced files")
    p.add_argument("--no-markers", action="store_true", help="do not copy markers/cue points")
    p.add_argument("--drop-extra-chunks", action="store_true", help="do not copy unknown chunks")
    p.add_argument("--name-template", dest="name_template",
                   help="file name template with {stem} {index} {channel} {channel1} {ch2} {name}")
    p.add_argument("--min-events", dest="min_events", type=int, default=1, help="skip parts with fewer events")
    _add_option(p, "--dry-run", action="store_true", help="list the files that would be written")
    _add_option(p, "--running-status", action="store_true", help="write channel messages with running status")
    _add_option(p, "--note-off-as-note-on-0", action="store_true",
                help="write note-offs as note-on with velocity 0")
    add_output_options(p)
    p.set_defaults(func=cmd_split, command="split")

    p = sub.add_parser("to-json", help="export the whole file as JSON")
    p.add_argument("input")
    p.add_argument("--mode", choices=["events", "notes", "both"], default="events")
    p.add_argument("--summary", action="store_true")
    p.add_argument("--raw", action="store_true")
    p.add_argument("--no-names", action="store_true")
    add_output_options(p, write=True)
    p.set_defaults(func=cmd_to_json, command="to-json")

    p = sub.add_parser("from-json", help="build a MIDI file from a JSON document")
    p.add_argument("input", help="JSON document path")
    add_output_options(p, write=True)
    p.set_defaults(func=cmd_from_json, command="from-json")

    p = sub.add_parser("create", help="create a new MIDI file from notes/spec")
    p.add_argument("output_positional", nargs="?", help="output path (same as -o)")
    p.add_argument("--ppq", type=int, default=DEFAULT_PPQ)
    p.add_argument("--midi-format", type=int, default=1, dest="midi_format", choices=[0, 1, 2],
                   help="SMF format of the created file (0 single track / 1 multi track)")
    p.add_argument("--tempo", type=float, help="BPM")
    p.add_argument("--time-signature", dest="time_signature", help="e.g. 4/4")
    p.add_argument("--key", help="key signature, e.g. C, Am, F#m")
    p.add_argument("--name", help="track 0 name / song title")
    p.add_argument("--track", action="append", help="track spec Name:channel:program (repeatable)")
    p.add_argument("--note", action="append", help="note spec 'pitch start duration [velocity] [channel] [track]'")
    p.add_argument("--unit", choices=["ticks", "beats", "seconds", "bars"], default="beats")
    p.add_argument("--channel", type=int, default=0)
    p.add_argument("--note-track", dest="note_track", type=int, default=0)
    p.add_argument("--notes-json", dest="notes_json", help="inline JSON array of notes")
    p.add_argument("--notes-file", dest="notes_file", help="JSON file with notes")
    p.add_argument("--meta", action="append", help="raw event JSON to insert (repeatable)")
    p.add_argument("--spec", help="score JSON ({format, division, tracks:[{name, notes:[...]}]})")
    add_output_options(p, write=True)
    p.set_defaults(func=cmd_create, command="create")

    p = sub.add_parser("apply", help="run a JSON edit plan")
    p.add_argument("input", nargs="?", help="MIDI file (may come from the plan)")
    p.add_argument("--plan", help="plan JSON file")
    p.add_argument("--ops", help="inline plan JSON (or @file)")
    p.add_argument("--stdin", action="store_true", help="read the plan from stdin")
    p.add_argument("--pairing", choices=["lifo", "fifo"], default="lifo")
    add_output_options(p, write=True)
    p.set_defaults(func=cmd_apply, command="apply")

    p = sub.add_parser("op", help="run a single operation (see 'ops')")
    op_sub = p.add_subparsers(dest="op_name", metavar="OP")
    for name in sorted(OPS):
        op = OPS[name]
        child = op_sub.add_parser(name, help=op.summary, description=op.summary)
        child.add_argument("input", help="input MIDI file")
        _add_op_arguments(child, op)
        child.add_argument("--pairing", choices=["lifo", "fifo"], default="lifo")
        add_output_options(child, write=True)
        child.set_defaults(func=cmd_op, command="op " + name)
    p.set_defaults(func=cmd_ops, command="op")

    p = sub.add_parser("ops", help="list operations / show one in detail")
    p.add_argument("--describe", help="op name to describe")
    p.add_argument("--category", help="filter by category")
    add_output_options(p)
    p.set_defaults(func=cmd_ops, command="ops")

    p = sub.add_parser("schema", help="JSON shapes used by this tool")
    p.add_argument("topic", nargs="?", choices=list(SCHEMA_TOPICS) + ["all"], default="all")
    add_output_options(p)
    p.set_defaults(func=cmd_schema, command="schema")

    p = sub.add_parser("selftest", help="run built-in checks")
    add_output_options(p)
    p.set_defaults(func=cmd_selftest, command="selftest")

    p = sub.add_parser("version", help="print the version")
    add_output_options(p)
    p.set_defaults(func=cmd_version, command="version")
    return parser


def _add_op_arguments(parser: argparse.ArgumentParser, op) -> None:
    for param in op.params:
        flag = "--" + param.name.replace("_", "-")
        dest = "p_" + param.name
        help_text = param.help or ""
        if flag in getattr(parser, "_option_string_actions", {}):
            continue
        if param.choices:
            help_text += " (%s)" % "|".join(param.choices)
        metavar = param.name.upper()
        if param.type == "bool":
            parser.add_argument(flag, dest=dest, action="store_true", default=None, help=help_text)
        elif param.type == "note_list":
            parser.add_argument(flag, dest=dest, action="append", default=None, metavar=metavar,
                                help=help_text + " (repeatable)")
        elif param.type == "int_list":
            parser.add_argument(flag, dest=dest, default=None, metavar=metavar, help=help_text + " (e.g. 0,1,2)")
        elif param.type == "choice":
            parser.add_argument(flag, dest=dest, default=None, choices=param.choices, help=help_text)
        else:
            parser.add_argument(flag, dest=dest, default=None, metavar=metavar, help=help_text)


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except MidiKitError as exc:
        sys.stdout.write(
            json.dumps({"ok": False, "command": "cli", "error": exc.to_dict()}, ensure_ascii=False, indent=2) + "\n"
        )
        return exit_code_for(exc)
    if not getattr(args, "command", None):
        parser.print_help()
        return EXIT_USAGE
    output = Output(args)
    report_path = getattr(args, "report", None)
    if report_path:
        output.report_path = report_path
    if getattr(args, "output_positional", None) and not getattr(args, "output", None):
        args.output = args.output_positional
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return EXIT_USAGE
    try:
        return func(args, output)
    except MidiKitError as exc:
        return fail(output, exc)
    except BrokenPipeError:
        return EXIT_OK
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # pragma: no cover - last resort
        if os.environ.get("MIDIKIT_DEBUG"):
            raise
        return fail(output, exc)
