"""Real world conformance harness.

Parses every MIDI file it can find, cross-checks the result against mido,
re-serialises and validates.  Usage:

    python tools/realworld_check.py [DIR ...] [--limit N] [--verbose]
"""

from __future__ import annotations

import os
import sys
import traceback
import warnings
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from midikit import MidiFile, apply_plan, extract_notes, validate  # noqa: E402
from midikit.analysis import summarize  # noqa: E402

try:
    import mido  # type: ignore

    HAVE_MIDO = True
except Exception:
    HAVE_MIDO = False

DEFAULT_DIRS = [
    os.path.join(os.path.expanduser("~"), "Music"),
    os.path.join(os.path.expanduser("~"), "Downloads"),
    os.path.join(os.path.expanduser("~"), "Documents"),
]
EXTENSIONS = (".mid", ".midi", ".rmi", ".kar")


def find_files(dirs: List[str], limit: int = 0) -> List[str]:
    out: List[str] = []
    for base in dirs:
        if not os.path.isdir(base):
            continue
        for root, _dirnames, filenames in os.walk(base):
            for name in filenames:
                if name.lower().endswith(EXTENSIONS):
                    out.append(os.path.join(root, name))
            if limit and len(out) >= limit:
                return out[:limit]
    return out[:limit] if limit else out


def mido_notes(path: str) -> List[Any]:
    """Pair notes the same way mido sees the file (LIFO for repeated pitches)."""
    midi = mido.MidiFile(path)
    notes: List[Any] = []
    for track_index, track in enumerate(midi.tracks):
        tick = 0
        open_notes: Dict[Any, List[Any]] = {}
        for msg in track:
            tick += msg.time
            if msg.type == "note_on" and msg.velocity > 0:
                open_notes.setdefault((msg.channel, msg.note), []).append((tick, msg.velocity))
            elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
                stack = open_notes.get((msg.channel, msg.note))
                if not stack:
                    continue
                start, velocity = stack.pop()
                notes.append((track_index, msg.channel, msg.note, start, tick, velocity))
        # notes still open at the end of the track are dropped by mido's view
    return notes


def mido_facts(path: str) -> Dict[str, Any]:
    midi = mido.MidiFile(path)
    notes = len(mido_notes(path))
    return {
        "type": midi.type,
        "ticks_per_beat": midi.ticks_per_beat,
        "tracks": len(midi.tracks),
        "notes": notes,
        "length": round(midi.length, 4),
    }


def check(path: str, verbose: bool = False) -> Dict[str, Any]:
    row: Dict[str, Any] = {"file": path, "ok": False, "problems": []}
    try:
        midi = MidiFile.read(path)
        row.update(
            {
                "format": midi.format,
                "division": midi.division.describe(),
                "tracks": len(midi.tracks),
                "warnings": len(midi.warnings),
            }
        )
        data1 = midi.to_bytes()
        again = MidiFile.read_bytes(data1)
        fps_a = [e.fingerprint() for _t, _i, e in midi.iter_events()]
        fps_b = [e.fingerprint() for _t, _i, e in again.iter_events()]
        if fps_a != fps_b:
            row["problems"].append("reparse changed the event stream")
        if again.to_bytes() != data1:
            row["problems"].append("re-serialisation is not idempotent")
        notes = extract_notes(midi)
        row["notes"] = len(notes.notes)
        row["unterminated"] = len(notes.unterminated_note_ons)
        row["orphan_offs"] = len(notes.orphan_note_offs)
        report = validate(midi)
        row["errors"] = report["counts"]["error"]
        row["warnings_v"] = report["counts"]["warning"]
        # a musical edit must be reversible
        original = [(n.track, n.channel, n.pitch, n.start, n.end, n.velocity) for n in notes.sorted()]
        result = apply_plan(midi, [{"op": "transpose", "semitones": 3}, {"op": "transpose", "semitones": -3}])
        if not result["ok"]:
            row["problems"].append("edit plan failed: %s" % result["failed"]["error"]["message"])
        else:
            after = [(n.track, n.channel, n.pitch, n.start, n.end, n.velocity)
                     for n in extract_notes(midi).sorted()]
            if after != original:
                row["problems"].append("transpose +3/-3 did not restore the notes")
        summary = summarize(midi)
        row["seconds"] = round(summary["duration_seconds"], 2)
        if HAVE_MIDO:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    facts = mido_facts(path)
                    theirs = sorted(tuple(item) for item in mido_notes(path))
                row["mido"] = facts
                if facts["type"] != midi.format:
                    row["problems"].append("format differs from mido (%s vs %s)" % (facts["type"], midi.format))
                if facts["tracks"] != len(midi.tracks):
                    row["problems"].append("track count differs from mido (%s vs %s)"
                                           % (facts["tracks"], len(midi.tracks)))
                if abs(facts["ticks_per_beat"] - midi.division.ticks_per_beat) > 0.001:
                    row["problems"].append("ticks_per_beat differs from mido")
                # note level comparison, computed independently by mido
                ours = sorted(
                    (n.track, n.channel, n.pitch, n.start, n.end, n.velocity)
                    for n in extract_notes(MidiFile.read_bytes(data1)).notes
                    if not n.unterminated
                )
                theirs = sorted(tuple(item) for item in mido_notes(path))
                if ours != theirs:
                    only_ours = [n for n in ours if n not in theirs][:3]
                    only_theirs = [n for n in theirs if n not in ours][:3]
                    row["problems"].append(
                        "note mismatch vs mido (ours %d, mido %d; e.g. ours-only %s, mido-only %s)"
                        % (len(ours), len(theirs), only_ours, only_theirs)
                    )
                row["mido_notes"] = len(theirs)
            except Exception as exc:  # mido itself may fail on exotic files
                row["mido"] = {"error": str(exc)}
        row["ok"] = not [p for p in row["problems"] if "mido" not in p or True]
        row["ok"] = not row["problems"]
    except Exception as exc:
        row["problems"].append("%s: %s" % (type(exc).__name__, exc))
        if verbose:
            traceback.print_exc()
    return row


def main(argv: List[str]) -> int:
    dirs = [a for a in argv if not a.startswith("-")] or DEFAULT_DIRS
    limit = 0
    verbose = "--verbose" in argv
    if "--limit" in argv:
        limit = int(argv[argv.index("--limit") + 1])
    files = find_files(dirs, limit)
    if not files:
        print("no MIDI files found in: %s" % ", ".join(dirs))
        return 1
    print("checking %d file(s); mido cross-check: %s" % (len(files), "on" if HAVE_MIDO else "off"))
    rows = []
    for index, path in enumerate(files, 1):
        row = check(path, verbose)
        rows.append(row)
        flag = "ok  " if row["ok"] else "FAIL"
        print("%s [%3d/%3d] %-52s fmt=%s trk=%-3s notes=%-6s sec=%-8s %s"
              % (flag, index, len(files), os.path.basename(path)[:52], row.get("format"),
                 row.get("tracks"), row.get("notes"), row.get("seconds"),
                 "; ".join(row["problems"])[:70]))
    failed = [r for r in rows if not r["ok"]]
    print("")
    print("files: %d, clean: %d, problems: %d" % (len(rows), len(rows) - len(failed), len(failed)))
    for row in failed:
        print("  %s -> %s" % (os.path.basename(row["file"]), "; ".join(row["problems"])))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
