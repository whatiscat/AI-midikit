"""Standard MIDI File 1.1 conformance checking."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from .constants import META_FIXED_LENGTH, META_TYPE_NAMES
from .events import ChannelEvent, MetaEvent, SystemEvent, decode_text
from .notes import extract_notes
from .selectors import resolve_track_indices
from .smf import MidiFile

SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}


def _issue(severity: str, code: str, message: str, **extra: Any) -> Dict[str, Any]:
    item: Dict[str, Any] = {"severity": severity, "code": code, "message": message}
    item.update(extra)
    return item


def validate(
    midi: MidiFile,
    tracks: Optional[Sequence[int]] = None,
    check_notes: bool = True,
    check_text: bool = True,
    normalize: bool = False,
) -> Dict[str, Any]:
    """Run every conformance check and return a structured report.

    With normalize=True the in-memory file is first brought to its write state
    (end-of-track events inserted), which is what an edit command should report.
    """
    if normalize:
        midi.prepare_for_write()
    issues: List[Dict[str, Any]] = []
    indices = resolve_track_indices(midi, tracks) if tracks else list(range(len(midi.tracks)))

    # --- header ---------------------------------------------------------
    if midi.format == 0 and len(midi.tracks) != 1:
        issues.append(
            _issue(
                "error",
                "V_FORMAT0_TRACK_COUNT",
                "format 0 files must contain exactly one MTrk chunk (found %d)" % len(midi.tracks),
            )
        )
    if midi.format == 1 and len(midi.tracks) == 0:
        issues.append(_issue("error", "V_NO_TRACKS", "format 1 file without any track chunk"))
    if midi.declared_track_count is not None and midi.declared_track_count != len(midi.tracks):
        issues.append(
            _issue(
                "error",
                "V_TRACK_COUNT",
                "header declares %d track(s) but the file contains %d"
                % (midi.declared_track_count, len(midi.tracks)),
            )
        )
    if not midi.division.is_smpte:
        ppq = midi.division.ppq or 0
        if ppq % 24 != 0:
            issues.append(
                _issue(
                    "info",
                    "V_PPQ_NOT_DIVISIBLE",
                    "ticks per quarter note (%d) is not divisible by 24; some devices expect 96/192/384/480/960" % ppq,
                )
            )
    for warning in midi.warnings:
        severity = "error" if warning.get("code") == "W_EVENTS_AFTER_EOT" else "warning"
        issues.append(_issue(severity, "P_" + str(warning.get("code", "PARSE")), str(warning.get("message")), **{
            key: value for key, value in warning.items() if key not in ("code", "message")
        }))

    # --- tracks ---------------------------------------------------------
    tempo_seen_outside_first = False
    for track_index in indices:
        track = midi.tracks[track_index]
        previous_tick = None
        eot_indexes = []
        for event_index, event in enumerate(track.events):
            if previous_tick is not None and event.tick < previous_tick:
                issues.append(
                    _issue(
                        "error",
                        "V_UNSORTED",
                        "event %d is at tick %d, before the previous event at tick %d (delta times are unsigned)"
                        % (event_index, event.tick, previous_tick),
                        track=track_index,
                        tick=event.tick,
                    )
                )
            previous_tick = event.tick
            if event.tick > 0x0FFFFFFF:
                issues.append(
                    _issue(
                        "error",
                        "V_TICK_OVERFLOW",
                        "tick %d exceeds the maximum encodable delta (0x0FFFFFFF)" % event.tick,
                        track=track_index,
                        tick=event.tick,
                    )
                )
            if isinstance(event, MetaEvent):
                expected = META_FIXED_LENGTH.get(event.type_byte)
                if expected is not None and len(event.data) != expected:
                    issues.append(
                        _issue(
                            "error",
                            "V_META_LENGTH",
                            "%s meta event must carry %d byte(s), found %d"
                            % (event.name, expected, len(event.data)),
                            track=track_index,
                            tick=event.tick,
                        )
                    )
                if event.type_byte == 0x2F:
                    eot_indexes.append(event_index)
                if event.type_byte == 0x51:
                    tempo = event.tempo
                    if tempo == 0:
                        issues.append(
                            _issue("error", "V_TEMPO_ZERO", "set_tempo value of 0 is invalid", track=track_index, tick=event.tick)
                        )
                    elif track_index != 0 and midi.format == 1:
                        tempo_seen_outside_first = True
                if event.type_byte == 0x58 and len(event.data) == 4:
                    numerator = event.data[0]
                    power = event.data[1]
                    if numerator == 0:
                        issues.append(
                            _issue("warning", "V_TIMESIG_ZERO", "time signature numerator is 0", track=track_index, tick=event.tick)
                        )
                    if power > 7:
                        issues.append(
                            _issue(
                                "warning",
                                "V_TIMESIG_DENOM",
                                "time signature denominator 2^%d is unusual" % power,
                                track=track_index,
                                tick=event.tick,
                            )
                        )
                if event.type_byte == 0x59 and len(event.data) == 2:
                    sf = event.data[0] - 256 if event.data[0] > 127 else event.data[0]
                    if not -7 <= sf <= 7:
                        issues.append(
                            _issue(
                                "error",
                                "V_KEYSIG_RANGE",
                                "key signature sf=%d is outside -7..7" % sf,
                                track=track_index,
                                tick=event.tick,
                            )
                        )
                if event.type_byte == 0x54 and len(event.data) == 5:
                    offset = event.smpte_offset
                    if offset:
                        frame_rate = offset["frame_rate"]
                        max_frame = 23 if frame_rate == 24 else (24 if frame_rate == 25 else 29)
                        for field, limit in (
                            ("hour", 23), ("minute", 59), ("second", 59),
                            ("frame", max_frame), ("subframe", 99),
                        ):
                            if offset[field] > limit:
                                issues.append(
                                    _issue(
                                        "error",
                                        "V_SMPTE_RANGE",
                                        "smpte_offset %s=%d is out of range 0-%d for %g fps"
                                        % (field, offset[field], limit, frame_rate),
                                        track=track_index,
                                        tick=event.tick,
                                    )
                                )
                if event.type_byte == 0x20:
                    if event_index != 0:
                        issues.append(
                            _issue(
                                "warning",
                                "V_CHANNEL_PREFIX_POSITION",
                                "channel prefix meta event should be the first event of the track",
                                track=track_index,
                                tick=event.tick,
                            )
                        )
                if check_text and event.type_byte in (0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09):
                    text, used, clean = decode_text(event.data)
                    if not clean and event.data:
                        issues.append(
                            _issue(
                                "info",
                                "V_TEXT_ENCODING",
                                "%s text is not valid UTF-8 (decoded as %s); the spec expects ASCII"
                                % (event.name, used),
                                track=track_index,
                                tick=event.tick,
                            )
                        )
                if event.type_byte not in META_TYPE_NAMES:
                    issues.append(
                        _issue(
                            "info",
                            "V_UNKNOWN_META",
                            "unknown meta event type 0x%02X preserved as-is" % event.type_byte,
                            track=track_index,
                            tick=event.tick,
                        )
                    )
            elif event.is_sysex:
                if not event.escape and (not event.data or event.data[-1] != 0xF7):
                    issues.append(
                        _issue(
                            "warning",
                            "V_SYSEX_TERMINATOR",
                            "F0 sysex event should end with F7",
                            track=track_index,
                            tick=event.tick,
                        )
                    )
            elif isinstance(event, SystemEvent):
                issues.append(
                    _issue(
                        "error",
                        "V_SYSTEM_MESSAGE",
                        "%s (0x%02X) is not permitted inside a track chunk by the SMF specification"
                        % (event.name, event.status_byte),
                        track=track_index,
                        tick=event.tick,
                    )
                )
        if not eot_indexes:
            issues.append(
                _issue(
                    "error",
                    "V_MISSING_EOT",
                    "track has no end-of-track meta event (0x2F)",
                    track=track_index,
                )
            )
        elif eot_indexes[-1] != len(track.events) - 1:
            issues.append(
                _issue(
                    "error",
                    "V_EOT_NOT_LAST",
                    "end-of-track meta event is not the last event of the track",
                    track=track_index,
                )
            )
        if len(eot_indexes) > 1:
            issues.append(
                _issue(
                    "warning",
                    "V_MULTIPLE_EOT",
                    "track contains %d end-of-track meta events" % len(eot_indexes),
                    track=track_index,
                )
            )
    if tempo_seen_outside_first and midi.format == 1:
        issues.append(
            _issue(
                "info",
                "V_TEMPO_TRACK",
                "tempo events outside track 0: format 1 convention is to keep the tempo map in the first track",
            )
        )

    # --- note pairing ---------------------------------------------------
    if check_notes:
        extraction = extract_notes(midi, indices)
        for item in extraction.unterminated_note_ons[:50]:
            issues.append(
                _issue(
                    "warning",
                    "V_UNTERMINATED_NOTE",
                    "note_on %s on channel %d has no matching note_off"
                    % (item["pitch"], item["channel"] + 1),
                    track=item["track"],
                    tick=item["tick"],
                )
            )
        for item in extraction.orphan_note_offs[:50]:
            issues.append(
                _issue(
                    "warning",
                    "V_ORPHAN_NOTE_OFF",
                    "note_off %s on channel %d has no matching note_on"
                    % (item["pitch"], item["channel"] + 1),
                    track=item["track"],
                    tick=item["tick"],
                )
            )

    issues.sort(key=lambda item: (SEVERITY_ORDER.get(item["severity"], 3), item.get("track", -1), item.get("tick", -1)))
    counts = {"error": 0, "warning": 0, "info": 0}
    for item in issues:
        counts[item["severity"]] = counts.get(item["severity"], 0) + 1
    return {
        "ok": counts["error"] == 0,
        "valid": counts["error"] == 0 and counts["warning"] == 0,
        "counts": counts,
        "issues": issues,
        "tracks_checked": list(indices),
        "format": midi.format,
        "division": midi.division.to_dict(),
        "track_count": len(midi.tracks),
    }
