"""Standard MIDI File 1.1 reader / writer.

Coverage of the specification:

* MThd header, format 0 / 1 / 2, PPQ and SMPTE division
* MTrk chunks with delta times (variable-length quantities)
* All channel voice messages, running status on read and write
* Meta events 0x00-0x7F (known types decoded, unknown types preserved)
* F0 sysex and F7 escape / continuation events
* Unknown / foreign chunks are preserved verbatim, RIFF (RMID) wrappers are
  unwrapped on read
"""

from __future__ import annotations

import io
import os
import tempfile
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from .bytestream import ByteReader, ByteWriter, hex_bytes
from .constants import (
    CHANNEL_MESSAGES,
    DEFAULT_PPQ,
    HEADER_CHUNK,
    META_FIXED_LENGTH,
    META_TYPE_NAMES,
    SYSTEM_MESSAGE_DATA_LEN,
    TRACK_CHUNK,
)
from .errors import ParseError, SchemaError, WriteError
from .events import (
    DEFAULT_OPTS,
    ChannelEvent,
    DecodeOptions,
    Event,
    MetaEvent,
    SysExEvent,
    SystemEvent,
    decode_text,
)


# ---------------------------------------------------------------------------
# division
# ---------------------------------------------------------------------------


class Division:
    """Time division of a MIDI file: metrical (PPQ) or time-code based (SMPTE)."""

    __slots__ = ("ppq", "smpte_fps", "smpte_ticks_per_frame")

    def __init__(
        self,
        ppq: Optional[int] = None,
        smpte_fps: Optional[float] = None,
        smpte_ticks_per_frame: Optional[int] = None,
    ) -> None:
        if smpte_fps is not None:
            rate = float(smpte_fps)
            if rate not in (24.0, 25.0, 29.97, 30.0):
                raise SchemaError("SMPTE frame rate must be 24, 25, 29.97 or 30: %r" % (smpte_fps,))
            tpf = int(smpte_ticks_per_frame if smpte_ticks_per_frame is not None else 80)
            if not 1 <= tpf <= 255:
                raise SchemaError("SMPTE ticks per frame must be 1-255: %r" % (tpf,))
            self.smpte_fps = rate
            self.smpte_ticks_per_frame = tpf
            self.ppq = None
        else:
            value = int(ppq if ppq is not None else DEFAULT_PPQ)
            if not 1 <= value <= 0x7FFF:
                raise SchemaError("ticks per quarter note must be 1-32767: %r" % (value,))
            self.ppq = value
            self.smpte_fps = None
            self.smpte_ticks_per_frame = None

    @property
    def is_smpte(self) -> bool:
        return self.smpte_fps is not None

    @property
    def ticks_per_beat(self) -> float:
        """Quarter notes are the tempo reference even for SMPTE files."""
        if self.is_smpte:
            return float(self.smpte_ticks_per_frame) * 4.0
        return float(self.ppq)

    @property
    def ticks_per_second(self) -> Optional[float]:
        if not self.is_smpte:
            return None
        return float(self.smpte_fps) * float(self.smpte_ticks_per_frame)

    @classmethod
    def from_int(cls, value: int) -> "Division":
        value = int(value) & 0xFFFF
        if value & 0x8000:
            high = (value >> 8) & 0xFF
            fps = high - 256
            tpf = value & 0xFF
            if fps == -29:
                rate = 29.97
            elif fps in (-24, -25, -30):
                rate = float(-fps)
            else:
                raise ParseError("invalid SMPTE division frame rate: %d" % fps, division=value)
            if tpf == 0:
                raise ParseError("SMPTE division with zero ticks per frame", division=value)
            return cls(smpte_fps=rate, smpte_ticks_per_frame=tpf)
        if value == 0:
            raise ParseError("division of 0 is invalid (no timing resolution)")
        return cls(ppq=value)

    def to_int(self) -> int:
        if self.is_smpte:
            fps = self.smpte_fps
            code = -30 if fps == 30.0 else (-29 if fps == 29.97 else -int(fps))
            return ((code & 0xFF) << 8) | (self.smpte_ticks_per_frame & 0xFF)
        return int(self.ppq)

    def to_dict(self) -> Dict[str, Any]:
        if self.is_smpte:
            return {
                "type": "smpte",
                "smpte_fps": self.smpte_fps,
                "ticks_per_frame": self.smpte_ticks_per_frame,
                "ticks_per_second": self.ticks_per_second,
            }
        return {"type": "ppq", "ticks_per_beat": self.ppq, "ppq": self.ppq}

    @classmethod
    def from_dict(cls, data: Any) -> "Division":
        if isinstance(data, bool):
            raise SchemaError("division must be an int or object: %r" % (data,))
        if isinstance(data, int):
            return cls(ppq=data)
        if isinstance(data, str):
            return cls(ppq=int(data))
        if not isinstance(data, dict):
            raise SchemaError("division must be an int or object: %r" % (data,))
        if data.get("type") == "smpte" or data.get("smpte_fps") is not None:
            return cls(smpte_fps=float(data["smpte_fps"]), smpte_ticks_per_frame=int(data.get("ticks_per_frame", 80)))
        ppq = data.get("ppq", data.get("ticks_per_beat", data.get("resolution", DEFAULT_PPQ)))
        return cls(ppq=int(ppq))

    def describe(self) -> str:
        if self.is_smpte:
            return "SMPTE %g fps x %d ticks/frame (%g ticks/s)" % (
                self.smpte_fps,
                self.smpte_ticks_per_frame,
                self.ticks_per_second,
            )
        return "%d PPQ" % self.ppq

    def __repr__(self) -> str:
        return "Division(%s)" % self.describe()

    def __eq__(self, other: Any) -> bool:
        return isinstance(other, Division) and self.to_int() == other.to_int()


# ---------------------------------------------------------------------------
# track
# ---------------------------------------------------------------------------


class MidiTrack:
    """An ordered list of events with absolute tick positions."""

    def __init__(
        self,
        events: Optional[Iterable[Event]] = None,
        name: Optional[str] = None,
        encoding: str = "utf-8",
    ) -> None:
        self.events: List[Event] = list(events) if events else []
        if name is not None:
            self.set_name(name, encoding)

    # -- basic list handling -------------------------------------------
    def __len__(self) -> int:
        return len(self.events)

    def __iter__(self):
        return iter(self.events)

    def __getitem__(self, index):
        return self.events[index]

    def append(self, event: Event) -> Event:
        self.events.append(event)
        return event

    def add(self, event: Event) -> Event:
        """Insert keeping the event list sorted by tick (stable)."""
        from bisect import insort_right

        insort_right(self.events, event)
        return event

    def extend(self, events: Iterable[Event]) -> None:
        for ev in events:
            self.add(ev)

    def remove_at(self, index: int) -> Event:
        return self.events.pop(index)

    def sort_events(self) -> None:
        """Stable, deterministic ordering: tick, then role, then original order."""
        self.events = self.sorted_events()

    def sorted_events(self) -> List[Event]:
        return [ev for _key, ev in sorted((ev.sort_key(i), ev) for i, ev in enumerate(self.events))]

    def sort(self) -> None:
        self.events = self.sorted_events()

    def clone(self) -> "MidiTrack":
        return MidiTrack([ev.clone() for ev in self.events])

    # -- queries --------------------------------------------------------
    @property
    def duration_ticks(self) -> int:
        return max((ev.tick for ev in self.events), default=0)

    @property
    def name(self) -> Optional[str]:
        for ev in self.events:
            if ev.is_meta and ev.type_byte == 0x03:
                return ev.text
        return None

    def set_name(self, name: str, encoding: str = "utf-8") -> None:
        """Rename the track, writing a track_name meta event.

        The name goes to the front of the track (after a MIDI channel prefix,
        which the specification requires to be the very first event).
        """
        for ev in self.events:
            if ev.is_meta and ev.type_byte == 0x03:
                ev.data = name.encode(encoding)
                return
        index = 1 if (self.events and self.events[0].is_meta and self.events[0].type_byte == 0x20) else 0
        self.events.insert(index, MetaEvent.make_track_name(0, name, encoding))

    def instrument_name(self) -> Optional[str]:
        for ev in self.events:
            if ev.is_meta and ev.type_byte == 0x04:
                return ev.text
        return None

    def channels(self) -> List[int]:
        found = set()
        for ev in self.events:
            if ev.is_channel:
                found.add(ev.channel)
        return sorted(found)

    def programs(self) -> List[int]:
        found = set()
        for ev in self.events:
            if ev.is_channel and ev.type == "program_change":
                found.add(ev.value1)
        return sorted(found)

    def count_by_type(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for ev in self.events:
            if ev.is_meta:
                key = "meta:" + ev.name
            elif ev.is_channel:
                key = ev.type
            elif ev.is_sysex:
                key = "sysex"
            else:
                key = "system"
            counts[key] = counts.get(key, 0) + 1
        return counts

    def end_of_track_event(self) -> Optional[MetaEvent]:
        for ev in reversed(self.events):
            if ev.is_meta and ev.type_byte == 0x2F:
                return ev
        return None

    # -- maintenance ----------------------------------------------------
    def normalize_end_of_track(self) -> bool:
        """Guarantee exactly one end-of-track meta event, placed last.  True if changed."""
        eots = [ev for ev in self.events if ev.is_meta and ev.type_byte == 0x2F]
        changed = False
        if eots:
            last = eots[-1]
            if len(eots) > 1 or self.events[-1] is not last:
                changed = True
            self.events = [ev for ev in self.events if not (ev.is_meta and ev.type_byte == 0x2F)]
        tick = max((ev.tick for ev in self.events), default=0)
        self.events.append(MetaEvent.make_end_of_track(tick))
        return changed or not eots

    def to_bytes(
        self,
        running_status: bool = False,
        note_off_as_note_on_0: bool = False,
    ) -> bytes:
        writer = ByteWriter()
        last_tick = 0
        status: Optional[int] = None
        for ev in self.events:
            delta = ev.tick - last_tick
            if delta < 0:
                raise SchemaError("events are not sorted by tick (delta %d)" % delta, tick=ev.tick)
            writer.vlq(delta)
            last_tick = ev.tick
            current = ev
            if note_off_as_note_on_0 and isinstance(ev, ChannelEvent) and ev.type == "note_off":
                current = ChannelEvent("note_on", ev.channel, ev.value1, 0, ev.tick)
            payload, status = current.to_bytes(status if running_status else None)
            writer.write_bytes(payload)
            if not running_status:
                status = None
        return writer.getvalue()

    def to_dict(self, opts: Optional[DecodeOptions] = None, include_events: bool = True) -> Dict[str, Any]:
        o = opts or DEFAULT_OPTS
        d: Dict[str, Any] = {
            "name": self.name,
            "events": len(self.events),
            "duration_ticks": self.duration_ticks,
            "channels": self.channels(),
            "programs": self.programs(),
            "event_counts": self.count_by_type(),
        }
        if include_events:
            d["event_list"] = [ev.to_dict(o) for ev in self.events]
        return d

    def __repr__(self) -> str:
        return "MidiTrack(events=%d, name=%r)" % (len(self.events), self.name)


# ---------------------------------------------------------------------------
# file
# ---------------------------------------------------------------------------


class MidiFile:
    """A Standard MIDI File: header, tracks and any foreign chunks."""

    #: number of tracks announced in the header of the file we read (if any)
    declared_track_count: Optional[int] = None

    def __init__(
        self,
        tracks: Optional[Sequence[MidiTrack]] = None,
        format: int = 1,
        division: Union[int, Division, None] = None,
        extra_chunks: Optional[Sequence[Tuple[bytes, bytes]]] = None,
    ) -> None:
        self.format = int(format)
        if self.format not in (0, 1, 2):
            raise SchemaError("MIDI file format must be 0, 1 or 2: %r" % (format,))
        self.division = division if isinstance(division, Division) else Division(ppq=division)
        self.tracks: List[MidiTrack] = list(tracks) if tracks else []
        self.extra_chunks: List[Tuple[bytes, bytes]] = list(extra_chunks) if extra_chunks else []
        self.warnings: List[Dict[str, Any]] = []
        self.source_name: Optional[str] = None
        self.header_extra: bytes = b""

    # -- convenience ----------------------------------------------------
    def __len__(self) -> int:
        return len(self.tracks)

    def __iter__(self):
        return iter(self.tracks)

    def __getitem__(self, index: int) -> MidiTrack:
        return self.tracks[index]

    @property
    def ppq(self) -> Optional[int]:
        return self.division.ppq

    @property
    def ticks_per_beat(self) -> float:
        return self.division.ticks_per_beat

    def new_track(self, name: Optional[str] = None, index: Optional[int] = None, encoding: str = "utf-8") -> MidiTrack:
        track = MidiTrack(name=name, encoding=encoding)
        if index is None:
            self.tracks.append(track)
        else:
            self.tracks.insert(index, track)
        return track

    def track(self, index: int) -> MidiTrack:
        try:
            return self.tracks[index]
        except IndexError:
            raise SchemaError("track index %d out of range (file has %d track(s))" % (index, len(self.tracks)))

    def iter_events(self) -> Iterable[Tuple[int, int, Event]]:
        for ti, track in enumerate(self.tracks):
            for ei, ev in enumerate(track.events):
                yield ti, ei, ev

    def all_events(self) -> List[Tuple[int, int, Event]]:
        return list(self.iter_events())

    def duration_ticks(self) -> int:
        return max((t.duration_ticks for t in self.tracks), default=0)

    def warn(self, code: str, message: str, **details: Any) -> None:
        item = {"code": code, "message": message}
        item.update(details)
        self.warnings.append(item)

    def prepare_for_write(self, normalize_eot: bool = True, sort_tracks: bool = False) -> Dict[str, Any]:
        """Apply the invariants required for a spec-conformant file."""
        report: Dict[str, Any] = {"end_of_track_fixed": [], "sorted_tracks": [], "track_count_updated": False}
        for index, track in enumerate(self.tracks):
            if sort_tracks and not _is_sorted(track.events):
                track.sort()
                report["sorted_tracks"].append(index)
            if normalize_eot and track.normalize_end_of_track():
                report["end_of_track_fixed"].append(index)
        if self.declared_track_count is not None and self.declared_track_count != len(self.tracks):
            report["track_count_updated"] = True
        return report

    # -- serialization ---------------------------------------------------
    def to_bytes(
        self,
        running_status: bool = False,
        note_off_as_note_on_0: bool = False,
        normalize_eot: bool = True,
        sort_tracks: bool = False,
    ) -> bytes:
        self.prepare_for_write(normalize_eot=normalize_eot, sort_tracks=sort_tracks)
        writer = ByteWriter()
        header = ByteWriter()
        header.u16(self.format)
        header.u16(len(self.tracks))
        header.u16(self.division.to_int())
        writer.chunk(HEADER_CHUNK, header.getvalue() + self.header_extra)
        for track in self.tracks:
            payload = track.to_bytes(running_status=running_status, note_off_as_note_on_0=note_off_as_note_on_0)
            writer.chunk(TRACK_CHUNK, payload)
        for chunk_type, payload in self.extra_chunks:
            writer.chunk(chunk_type, payload)
        return writer.getvalue()

    def write(
        self,
        path: str,
        running_status: bool = False,
        note_off_as_note_on_0: bool = False,
        normalize_eot: bool = True,
        atomic: bool = True,
    ) -> str:
        data = self.to_bytes(
            running_status=running_status,
            note_off_as_note_on_0=note_off_as_note_on_0,
            normalize_eot=normalize_eot,
        )
        directory = os.path.dirname(os.path.abspath(path)) or "."
        try:
            if not os.path.isdir(directory):
                os.makedirs(directory, exist_ok=True)
            if atomic:
                fd, tmp = tempfile.mkstemp(prefix=".midikit-", suffix=".tmp", dir=directory)
                try:
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(data)
                    os.replace(tmp, path)
                except BaseException:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                    raise
            else:
                with open(path, "wb") as handle:
                    handle.write(data)
        except OSError as exc:
            raise WriteError("cannot write %s: %s" % (path, exc), path=path)
        return path

    # -- parsing ---------------------------------------------------------
    @classmethod
    def read(
        cls,
        source: Union[str, bytes, bytearray, io.IOBase],
        strict: bool = False,
        encoding: str = "auto",
    ) -> "MidiFile":
        if isinstance(source, (bytes, bytearray)):
            data = bytes(source)
            name = "<bytes>"
        elif hasattr(source, "read"):
            data = source.read()
            name = getattr(source, "name", "<stream>")
        else:
            name = str(source)
            try:
                with open(source, "rb") as handle:
                    data = handle.read()
            except OSError as exc:
                raise ParseError("cannot read %s: %s" % (source, exc), path=str(source))
        midi = cls._parse(data, strict=strict, encoding=encoding)
        midi.source_name = name
        return midi

    @classmethod
    def read_bytes(cls, data: bytes, strict: bool = False, encoding: str = "auto") -> "MidiFile":
        return cls._parse(bytes(data), strict=strict, encoding=encoding)

    @classmethod
    def _parse(cls, data: bytes, strict: bool = False, encoding: str = "auto") -> "MidiFile":
        if len(data) < 14:
            raise ParseError("file is too short to be a Standard MIDI File (%d bytes)" % len(data))
        file_warnings: List[Dict[str, Any]] = []

        if data[:4] == b"RIFF":
            payload = _unwrap_riff(data, file_warnings)
            if payload is not None:
                data = payload

        reader = ByteReader(data, name="file")
        chunk_type = reader.read_bytes(4)
        if chunk_type != HEADER_CHUNK:
            if strict:
                raise ParseError("expected MThd header chunk, found %r" % chunk_type)
            idx = data.find(HEADER_CHUNK)
            if idx < 0:
                raise ParseError("no MThd header chunk found (not a Standard MIDI File)")
            file_warnings.append(
                {"code": "W_LEADING_GARBAGE", "message": "skipped %d leading byte(s) before the MThd chunk" % idx}
            )
            reader = ByteReader(data, pos=idx, name="file")
            reader.read_bytes(4)
        header_length = reader.u32()
        if header_length < 6:
            raise ParseError("MThd chunk length must be at least 6, got %d" % header_length, length=header_length)
        header = reader.read_bytes(header_length)
        header_reader = ByteReader(header, name="MThd")
        format_ = header_reader.u16()
        declared_tracks = header_reader.u16()
        division_raw = header_reader.u16()
        header_extra = header[6:]
        if format_ not in (0, 1, 2):
            if strict:
                raise ParseError("invalid MIDI file format %d (must be 0, 1 or 2)" % format_, format=format_)
            file_warnings.append({"code": "W_BAD_FORMAT", "message": "header format %d is not 0, 1 or 2" % format_})
        division = Division.from_int(division_raw) if division_raw else Division(ppq=DEFAULT_PPQ)
        if division_raw == 0:
            file_warnings.append({"code": "W_BAD_DIVISION", "message": "division is 0; assuming 480 PPQ"})

        midi = cls(format=format_, division=division, extra_chunks=[])
        midi.header_extra = header_extra
        midi.declared_track_count = declared_tracks

        track_index = 0
        vendor = _header_vendor(header_extra, file_warnings)
        while reader.remaining > 0:
            if reader.remaining < 8:
                file_warnings.append(
                    {
                        "code": "W_TRAILING_BYTES",
                        "message": "%d trailing byte(s) after the last chunk were ignored" % reader.remaining,
                        "offset": reader.tell(),
                    }
                )
                break
            ctype = reader.read_bytes(4)
            length = reader.u32()
            if length > reader.remaining:
                if strict:
                    raise ParseError(
                        "chunk %r declares %d bytes but only %d remain" % (ctype, length, reader.remaining),
                        chunk=ctype.decode("latin-1"),
                    )
                file_warnings.append(
                    {
                        "code": "W_TRUNCATED_CHUNK",
                        "message": "chunk %r is truncated (%d bytes declared, %d present)"
                        % (ctype.decode("latin-1", "replace"), length, reader.remaining),
                        "chunk": ctype.decode("latin-1", "replace"),
                    }
                )
                length = reader.remaining
            payload = reader.read_bytes(length)
            if ctype == TRACK_CHUNK:
                track = MidiTrack()
                _parse_track(payload, track, track_index, strict, file_warnings, encoding)
                midi.tracks.append(track)
                track_index += 1
            else:
                midi.extra_chunks.append((ctype, payload))
                detail = _decode_vendor_chunk(ctype, payload, vendor)
                file_warnings.append(
                    {
                        "code": "W_UNKNOWN_CHUNK",
                        "message": "preserved unknown chunk %r (%d bytes)%s"
                        % (ctype.decode("latin-1", "replace"), len(payload), detail),
                        "chunk": ctype.decode("latin-1", "replace"),
                        "length": len(payload),
                    }
                )
        if declared_tracks != len(midi.tracks):
            file_warnings.append(
                {
                    "code": "W_TRACK_COUNT",
                    "message": "header declares %d track(s) but %d MTrk chunk(s) found"
                    % (declared_tracks, len(midi.tracks)),
                    "declared": declared_tracks,
                    "found": len(midi.tracks),
                }
            )
        if format_ == 0 and len(midi.tracks) != 1:
            file_warnings.append(
                {
                    "code": "W_FORMAT0_TRACKS",
                    "message": "format 0 file must contain exactly one track, found %d" % len(midi.tracks),
                }
            )
        midi.warnings.extend(file_warnings)
        return midi

    # -- JSON interchange -------------------------------------------------
    def to_dict(self, opts: Optional[DecodeOptions] = None) -> Dict[str, Any]:
        from .jsonio import to_document

        return to_document(self, opts)

    @classmethod
    def from_dict(cls, doc: Dict[str, Any]) -> "MidiFile":
        from .jsonio import from_document

        return from_document(doc)

    def __repr__(self) -> str:
        return "MidiFile(format=%d, division=%s, tracks=%d)" % (
            self.format,
            self.division.describe(),
            len(self.tracks),
        )


# ---------------------------------------------------------------------------
# parsing helpers
# ---------------------------------------------------------------------------


def _is_sorted(events: Sequence[Event]) -> bool:
    return all(events[i].tick <= events[i + 1].tick for i in range(len(events) - 1))


def sort_track_if_unsorted(track: MidiTrack) -> bool:
    """Sort only when the tick order is broken.

    A track whose ticks are already non-decreasing is left untouched so that a
    document -> file round trip reproduces the exact byte order (the role based
    ordering of sort_events would otherwise move sysex/meta events around inside
    the same tick).  Returns True when the track was reordered.
    """
    if _is_sorted(track.events):
        return False
    track.sort_events()
    return True


def _parse_track(
    payload: bytes,
    track: MidiTrack,
    track_index: int,
    strict: bool,
    warnings: List[Dict[str, Any]],
    encoding: str,
) -> None:
    reader = ByteReader(payload, name="MTrk[%d]" % track_index)
    events = track.events
    tick = 0
    running_status: Optional[int] = None
    saw_eot = False

    def warn(code: str, message: str, **extra: Any) -> None:
        item = {"code": code, "message": message, "track": track_index, "offset": reader.tell()}
        item.update(extra)
        warnings.append(item)

    while reader.remaining > 0:
        start = reader.tell()
        try:
            delta = reader.read_vlq(strict=strict)
        except ParseError as exc:
            warn("W_TRUNCATED_TRACK", "track data ended inside a delta time: %s" % exc.message)
            break
        tick += delta
        status_byte = reader.peek_u8()
        if status_byte is None:
            warn("W_TRUNCATED_TRACK", "track ends with a delta time but no event")
            break

        if status_byte < 0x80:
            if running_status is None:
                if strict:
                    raise ParseError(
                        "running status used before any status byte in track %d" % track_index,
                        track=track_index,
                        offset=reader.tell(),
                    )
                warn("W_RUNNING_STATUS", "data byte without preceding status byte; event skipped")
                reader.u8()
                continue
            status = running_status
            first_data: Optional[int] = None
        else:
            reader.u8()
            status = status_byte
            first_data = None

        if status < 0xF0:
            nibble = status & 0xF0
            name, ndata = CHANNEL_MESSAGES[nibble]
            channel = status & 0x0F
            if first_data is None:
                value = reader.u8()
                if value > 0x7F and not strict:
                    reader.pos -= 1
                    warn("W_BAD_DATA_BYTE", "data byte 0x%02X has the high bit set; event dropped" % value)
                    running_status = None
                    continue
                first_data = value
            if first_data > 0x7F:
                raise ParseError(
                    "data byte out of range in track %d: 0x%02X" % (track_index, first_data),
                    track=track_index,
                    offset=reader.tell(),
                )
            if ndata == 2:
                second = reader.u8()
                if second > 0x7F:
                    if strict:
                        raise ParseError(
                            "data byte out of range in track %d: 0x%02X" % (track_index, second),
                            track=track_index,
                            offset=reader.tell(),
                        )
                    reader.pos -= 1
                    warn("W_BAD_DATA_BYTE", "data byte 0x%02X has the high bit set; event dropped" % second)
                    running_status = status
                    continue
            else:
                second = None
            events.append(ChannelEvent(name, channel, first_data, second, tick))
            running_status = status
        elif status == 0xFF:
            meta_type = reader.u8()
            length = reader.read_vlq(strict=strict)
            data = reader.read_bytes(length)
            event = MetaEvent(meta_type, data, tick)
            expected = META_FIXED_LENGTH.get(meta_type, None)
            if meta_type in META_FIXED_LENGTH and expected is not None and len(data) != expected:
                warn(
                    "W_META_LENGTH",
                    "meta %s should carry %d byte(s), found %d"
                    % (META_TYPE_NAMES.get(meta_type, hex(meta_type)), expected, len(data)),
                    meta_type=META_TYPE_NAMES.get(meta_type, hex(meta_type)),
                )
            if meta_type not in META_TYPE_NAMES:
                warn(
                    "W_UNKNOWN_META",
                    "unknown meta event type 0x%02X preserved verbatim" % meta_type,
                    meta_type=meta_type,
                )
            if meta_type == 0x2F:
                saw_eot = True
                if reader.remaining > 0:
                    warn("W_EVENTS_AFTER_EOT", "data continues after the end-of-track meta event")
            elif saw_eot:
                warn("W_EVENTS_AFTER_EOT", "event after the end-of-track meta event")
            events.append(event)
            running_status = None
        elif status in (0xF0, 0xF7):
            length = reader.read_vlq(strict=strict)
            data = reader.read_bytes(length)
            if status == 0xF0 and (not data or data[-1] != 0xF7):
                warn("W_SYSEX_TERMINATOR", "F0 sysex event does not end with F7")
            events.append(SysExEvent(data, escape=(status == 0xF7), tick=tick))
            running_status = None
        else:
            length = SYSTEM_MESSAGE_DATA_LEN.get(status)
            if length is None:
                if strict:
                    raise ParseError("unsupported status byte 0x%02X in track %d" % (status, track_index))
                warn("W_SYSTEM_MESSAGE", "unsupported status byte 0x%02X treated as a system event" % status)
                length = 0
            data = reader.read_bytes(length)
            events.append(SystemEvent(status, data, tick))
            running_status = None

        if reader.tell() == start:
            warn("W_STALL", "parser made no progress; aborting track")
            break

    if not saw_eot:
        warnings.append(
            {
                "code": "W_MISSING_EOT",
                "message": "track %d has no end-of-track meta event" % track_index,
                "track": track_index,
            }
        )


def _unwrap_riff(data: bytes, warnings: List[Dict[str, Any]]) -> Optional[bytes]:
    if len(data) < 12 or data[8:12] not in (b"RMID", b"MIDI"):
        return None
    reader = ByteReader(data, pos=12, name="RIFF")
    try:
        while reader.remaining >= 8:
            ctype = reader.read_bytes(4)
            length = reader.u32()
            payload = reader.read_bytes(min(length, reader.remaining))
            if ctype == b"data":
                warnings.append(
                    {"code": "W_RIFF_WRAPPER", "message": "unwrapped RIFF/RMID container and used its data chunk"}
                )
                return payload
    except ParseError:
        return None
    return None


def _header_vendor(header_extra: bytes, warnings: List[Dict[str, Any]]) -> Optional[str]:
    if not header_extra:
        return None
    if header_extra[:2] == b"XS":
        name = header_extra[2:].split(b"\x00")[0]
        warnings.append(
            {
                "code": "W_VENDOR_HEADER",
                "message": "Yamaha XF header extension present (%r)" % name.decode("latin-1", "replace"),
            }
        )
        return "xf"
    warnings.append(
        {
            "code": "W_HEADER_EXTRA",
            "message": "MThd chunk has %d extra byte(s); preserved verbatim" % len(header_extra),
        }
    )
    return None


def _decode_vendor_chunk(ctype: bytes, payload: bytes, vendor: Optional[str]) -> str:
    if ctype == b"XFIH":
        return " (Yamaha XF information header)"
    if ctype == b"XFKM":
        return " (Yamaha XF karaoke)"
    text, _enc, _clean = decode_text(payload[:80])
    printable = "".join(ch if ch.isprintable() else "." for ch in text)
    return (" (starts with %r)" % printable) if printable.strip(".") else ""


def read_midi(source, strict: bool = False, encoding: str = "auto") -> MidiFile:
    """Module level shortcut for MidiFile.read."""
    return MidiFile.read(source, strict=strict, encoding=encoding)
