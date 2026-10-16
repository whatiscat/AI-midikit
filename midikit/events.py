"""Event model: channel voice messages, meta events, sysex, stray system bytes.

Every event stores an absolute tick position.  Delta times are computed by the
serializer in smf.py.  Payloads keep their raw bytes so that unrelated edits
never corrupt a file.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from .bytestream import bytes_from_hex, hex_bytes
from .constants import (
    CC_NAMES,
    CHANNEL_MESSAGES,
    DEFAULT_TEMPO,
    GM_DRUM_NOTES,
    GM_PROGRAMS,
    META_FIXED_LENGTH,
    META_NAME_TO_TYPE,
    META_TYPE_NAMES,
    SMPTE_RATE_INDEX,
    SYSTEM_MESSAGE_DATA_LEN,
    SYSTEM_STATUS_NAMES,
    TEXT_META_TYPES,
    TYPE_ALIASES,
    TYPE_TO_STATUS,
    KEY_SIGNATURES,
    cc_name,
    note_name,
    note_number,
    program_name,
    program_number,
)
from .errors import SchemaError

SORT_META = 0
SORT_SYSEX = 0
SORT_NOTE_OFF = 1
SORT_CHANNEL = 2
SORT_NOTE_ON = 3
SORT_SYSTEM = 4


class DecodeOptions:
    """Controls how events are rendered into dictionaries / text."""

    __slots__ = ("encoding", "names", "full_data", "drum_channel", "note_range", "bend_range")

    def __init__(
        self,
        encoding: str = "auto",
        names: bool = True,
        full_data: bool = False,
        drum_channel: int = 9,
        bend_range: float = 2.0,
    ) -> None:
        self.encoding = encoding
        self.names = names
        self.full_data = full_data
        self.drum_channel = drum_channel
        self.bend_range = bend_range


DEFAULT_OPTS = DecodeOptions()


# ---------------------------------------------------------------------------
# text helpers
# ---------------------------------------------------------------------------


def decode_text(data: bytes, encoding: str = "auto") -> Tuple[str, str, bool]:
    """Decode meta text.  Returns (text, encoding_used, exactly_round_trips)."""
    if not data:
        return "", encoding if encoding != "auto" else "utf-8", True
    if encoding in ("auto", "", None):
        try:
            return data.decode("utf-8"), "utf-8", True
        except UnicodeDecodeError:
            pass
        try:
            return data.decode("cp1252"), "cp1252", False
        except UnicodeDecodeError:
            pass
        return data.decode("latin-1"), "latin-1", False
    try:
        return data.decode(encoding), encoding, True
    except LookupError:
        raise SchemaError("unknown text encoding: %r" % (encoding,))
    except UnicodeDecodeError:
        return data.decode("latin-1"), "latin-1", False


def encode_text(text: str, encoding: str = "utf-8") -> bytes:
    """Encode meta text, raising a helpful error when the target codec cannot."""
    enc = "utf-8" if encoding in ("auto", "", None) else encoding
    try:
        return text.encode(enc)
    except LookupError:
        raise SchemaError("unknown text encoding: %r" % (encoding,))
    except UnicodeEncodeError as exc:
        raise SchemaError(
            "text cannot be encoded as %s: %s (use --text-encoding utf-8 or latin-1)" % (enc, exc.reason),
            text=text,
            encoding=enc,
        )


# ---------------------------------------------------------------------------
# base event
# ---------------------------------------------------------------------------


class Event:
    """Base class.  Subclasses: ChannelEvent, MetaEvent, SysExEvent, SystemEvent."""

    __slots__ = ("tick",)

    kind = "event"

    def __init__(self, tick: int = 0) -> None:
        self.tick = int(tick)

    # -- ordering ------------------------------------------------------
    def __lt__(self, other: Any):
        if isinstance(other, Event):
            return self.tick < other.tick
        return NotImplemented

    def sort_priority(self) -> int:
        return SORT_CHANNEL

    def sort_key(self, seq: int = 0) -> Tuple[int, int, int]:
        return (self.tick, self.sort_priority(), seq)

    # -- copying -------------------------------------------------------
    def clone(self) -> "Event":
        raise NotImplementedError

    # -- classification ------------------------------------------------
    @property
    def is_meta(self) -> bool:
        return False

    @property
    def is_channel(self) -> bool:
        return False

    @property
    def is_sysex(self) -> bool:
        return False

    @property
    def is_note_on(self) -> bool:
        return False

    @property
    def is_note_off(self) -> bool:
        return False

    @property
    def channel(self) -> Optional[int]:
        return None

    # -- serialization -------------------------------------------------
    def to_dict(self, opts: Optional[DecodeOptions] = None) -> Dict[str, Any]:
        raise NotImplementedError

    def describe(self, opts: Optional[DecodeOptions] = None) -> str:
        d = self.to_dict(opts)
        parts = ["@%d" % self.tick]
        for key, value in d.items():
            if key == "tick":
                continue
            parts.append("%s=%s" % (key, value))
        return " ".join(parts)

    def to_bytes(self, running_status: Optional[int] = None) -> Tuple[bytes, Optional[int]]:
        raise NotImplementedError

    def fingerprint(self, include_tick: bool = True) -> Tuple:
        """Identity of the event for dedupe / diff (ignores decorative names)."""
        raise NotImplementedError

    # -- json ----------------------------------------------------------
    def to_json(self, opts: Optional[DecodeOptions] = None) -> Dict[str, Any]:
        return self.to_dict(opts)


# ---------------------------------------------------------------------------
# channel voice messages
# ---------------------------------------------------------------------------


class ChannelEvent(Event):
    """note_on / note_off / poly_aftertouch / control_change / program_change /
    channel_aftertouch / pitch_bend."""

    __slots__ = ("type", "channel", "value1", "value2")

    kind = "channel"

    def __init__(
        self,
        type: str,
        channel: int = 0,
        value1: int = 0,
        value2: Optional[int] = None,
        tick: int = 0,
    ) -> None:
        super().__init__(tick)
        canonical = canonical_type(type)
        if canonical not in TYPE_TO_STATUS:
            raise SchemaError("not a channel message type: %r" % (type,))
        self.type = canonical
        if not 0 <= int(channel) <= 15:
            raise SchemaError("channel out of range 0-15: %r" % (channel,))
        self.channel = int(channel)
        expected = CHANNEL_MESSAGES[TYPE_TO_STATUS[canonical]][1]
        self.value1 = _check_data_byte(value1, "value1")
        if expected == 2:
            self.value2 = _check_data_byte(0 if value2 is None else value2, "value2")
        else:
            self.value2 = None

    # -- generic -------------------------------------------------------
    @property
    def status(self) -> int:
        return TYPE_TO_STATUS[self.type] | self.channel

    @property
    def data_bytes(self) -> bytes:
        if self.value2 is None:
            return bytes([self.value1])
        return bytes([self.value1, self.value2])

    def clone(self) -> "ChannelEvent":
        return ChannelEvent(self.type, self.channel, self.value1, self.value2, self.tick)

    @property
    def is_channel(self) -> bool:
        return True

    @property
    def is_note_on(self) -> bool:
        return self.type == "note_on" and (self.value2 or 0) > 0

    @property
    def is_note_off(self) -> bool:
        return self.type == "note_off" or (self.type == "note_on" and (self.value2 or 0) == 0)

    def sort_priority(self) -> int:
        if self.is_note_off:
            return SORT_NOTE_OFF
        if self.type == "note_on":
            return SORT_NOTE_ON
        return SORT_CHANNEL

    # -- semantic accessors -------------------------------------------
    @property
    def note(self) -> Optional[int]:
        if self.type in ("note_on", "note_off", "poly_aftertouch"):
            return self.value1
        return None

    @note.setter
    def note(self, value: int) -> None:
        self.value1 = _check_data_byte(value, "note")

    @property
    def velocity(self) -> Optional[int]:
        if self.type in ("note_on", "note_off"):
            return self.value2
        return None

    @velocity.setter
    def velocity(self, value: int) -> None:
        if self.type not in ("note_on", "note_off"):
            raise SchemaError("velocity only applies to note_on / note_off")
        self.value2 = _check_data_byte(value, "velocity")

    @property
    def control(self) -> Optional[int]:
        return self.value1 if self.type == "control_change" else None

    @control.setter
    def control(self, value: int) -> None:
        self.value1 = _check_data_byte(value, "control")

    @property
    def value(self) -> Optional[int]:
        if self.type == "control_change":
            return self.value2
        if self.type == "pitch_bend":
            return (self.value1 or 0) | ((self.value2 or 0) << 7)
        return None

    @value.setter
    def value(self, raw: int) -> None:
        if self.type == "pitch_bend":
            raw = int(raw)
            if not 0 <= raw <= 16383:
                raise SchemaError("pitch bend value out of range 0-16383: %r" % (raw,))
            self.value1 = raw & 0x7F
            self.value2 = (raw >> 7) & 0x7F
        else:
            self.value2 = _check_data_byte(raw, "value")

    @property
    def pitch(self) -> Optional[int]:
        return self.value if self.type == "pitch_bend" else None

    @pitch.setter
    def pitch(self, raw: int) -> None:
        self.value = raw

    @property
    def bend(self) -> Optional[int]:
        """Signed pitch bend in the range -8192..8191."""
        return None if self.type != "pitch_bend" else self.value - 8192

    @bend.setter
    def bend(self, signed: int) -> None:
        signed = int(signed)
        if not -8192 <= signed <= 8191:
            raise SchemaError("pitch bend out of range -8192..8191: %r" % (signed,))
        self.value = signed + 8192

    def semitones(self, bend_range: float = 2.0) -> Optional[float]:
        if self.type != "pitch_bend":
            return None
        return (self.bend / 8192.0) * float(bend_range)

    @property
    def program(self) -> Optional[int]:
        return self.value1 if self.type == "program_change" else None

    @program.setter
    def program(self, value: int) -> None:
        self.value1 = _check_data_byte(value, "program")

    @property
    def pressure(self) -> Optional[int]:
        if self.type == "channel_aftertouch":
            return self.value1
        if self.type == "poly_aftertouch":
            return self.value2
        return None

    # -- serialization -------------------------------------------------
    def to_dict(self, opts: Optional[DecodeOptions] = None) -> Dict[str, Any]:
        o = opts or DEFAULT_OPTS
        d: Dict[str, Any] = {"tick": self.tick, "type": self.type, "channel": self.channel}
        if o.names:
            d["channel_display"] = self.channel + 1
        if self.type in ("note_on", "note_off"):
            d["note"] = self.value1
            d["velocity"] = self.value2
            if o.names:
                d["pitch"] = note_name(self.value1)
                if self.channel == o.drum_channel:
                    d["drum"] = GM_DRUM_NOTES.get(self.value1)
        elif self.type == "poly_aftertouch":
            d["note"] = self.value1
            d["pressure"] = self.value2
            if o.names:
                d["pitch"] = note_name(self.value1)
        elif self.type == "control_change":
            d["control"] = self.value1
            d["value"] = self.value2
            if o.names:
                d["control_name"] = cc_name(self.value1)
        elif self.type == "program_change":
            d["program"] = self.value1
            if o.names:
                d["program_number"] = self.value1 + 1
                d["program_name"] = program_name(self.value1)
        elif self.type == "channel_aftertouch":
            d["pressure"] = self.value1
        elif self.type == "pitch_bend":
            d["value"] = self.value
            d["bend"] = self.bend
            if o.names:
                d["semitones"] = round(self.semitones(o.bend_range), 4)
        return d

    def describe(self, opts: Optional[DecodeOptions] = None) -> str:
        o = opts or DEFAULT_OPTS
        head = "ch%-2d" % (self.channel + 1)
        if self.type in ("note_on", "note_off"):
            return "%s %-9s %-6s vel=%-3d" % (head, self.type, note_name(self.value1), self.value2)
        if self.type == "control_change":
            return "%s %-9s cc=%-3d %-20s value=%d" % (
                head, "control_change", self.value1, cc_name(self.value1), self.value2,
            )
        if self.type == "program_change":
            return "%s %-9s %d (%s)" % (head, "program", self.value1, program_name(self.value1))
        if self.type == "poly_aftertouch":
            return "%s %-9s %-6s pressure=%d" % (head, "poly_at", note_name(self.value1), self.value2)
        if self.type == "channel_aftertouch":
            return "%s %-9s pressure=%d" % (head, "chan_at", self.value1)
        if self.type == "pitch_bend":
            return "%s %-9s %d (%+.2f semitones)" % (head, "bend", self.value, self.semitones(o.bend_range))
        return "%s %s" % (head, self.type)

    def to_bytes(self, running_status: Optional[int] = None) -> Tuple[bytes, Optional[int]]:
        status = self.status
        data = self.data_bytes
        if running_status == status:
            return data, status
        return bytes([status]) + data, status

    def fingerprint(self, include_tick: bool = True) -> Tuple:
        base: Tuple = ("channel", self.type, self.channel, self.value1, self.value2)
        return (self.tick,) + base if include_tick else base


# ---------------------------------------------------------------------------
# meta events
# ---------------------------------------------------------------------------


class MetaEvent(Event):
    """Any 0xFF meta event; payload kept as raw bytes."""

    __slots__ = ("type_byte", "data")

    kind = "meta"

    def __init__(self, meta_type, data: bytes = b"", tick: int = 0) -> None:
        super().__init__(tick)
        self.type_byte = meta_type_number(meta_type)
        if isinstance(data, str):
            data = data.encode("latin-1")
        self.data = bytes(data)

    # -- factories -----------------------------------------------------
    @classmethod
    def make_text(cls, tick: int, meta_type, text: str, encoding: str = "utf-8") -> "MetaEvent":
        return cls(meta_type, encode_text(text, encoding), tick)

    @classmethod
    def make_track_name(cls, tick: int, name: str, encoding: str = "utf-8") -> "MetaEvent":
        return cls(0x03, encode_text(name, encoding), tick)

    @classmethod
    def make_lyric(cls, tick: int, text: str, encoding: str = "utf-8") -> "MetaEvent":
        return cls(0x05, encode_text(text, encoding), tick)

    @classmethod
    def make_marker(cls, tick: int, text: str, encoding: str = "utf-8") -> "MetaEvent":
        return cls(0x06, encode_text(text, encoding), tick)

    @classmethod
    def make_tempo(cls, tick: int = 0, tempo: Optional[int] = None, bpm: Optional[float] = None) -> "MetaEvent":
        if tempo is None:
            if bpm is None:
                tempo = DEFAULT_TEMPO
            else:
                tempo = bpm_to_tempo(bpm)
        tempo = int(round(tempo))
        if not 1 <= tempo <= 0xFFFFFF:
            raise SchemaError("tempo out of range 1..16777215 microseconds per quarter note", tempo=tempo)
        return cls(0x51, bytes([(tempo >> 16) & 0xFF, (tempo >> 8) & 0xFF, tempo & 0xFF]), tick)

    @classmethod
    def make_time_signature(
        cls,
        tick: int = 0,
        numerator: int = 4,
        denominator: int = 4,
        clocks_per_click: int = 24,
        notated_32nd_per_beat: int = 8,
    ) -> "MetaEvent":
        dd = denominator_to_power(denominator)
        for label, value, limit in (
            ("numerator", numerator, 255),
            ("clocks_per_click", clocks_per_click, 255),
            ("notated_32nd_per_beat", notated_32nd_per_beat, 255),
        ):
            if not 0 <= int(value) <= limit:
                raise SchemaError("%s out of range 0-%d: %r" % (label, limit, value))
        return cls(0x58, bytes([int(numerator), dd, int(clocks_per_click), int(notated_32nd_per_beat)]), tick)

    @classmethod
    def make_key_signature(cls, tick: int = 0, sf: int = 0, mi: int = 0, key: Optional[str] = None) -> "MetaEvent":
        if key is not None:
            sf, mi = key_signature_from_name(key)
        if not -7 <= int(sf) <= 7:
            raise SchemaError("key signature sf out of range -7..7: %r" % (sf,))
        return cls(0x59, bytes([int(sf) & 0xFF, 1 if mi else 0]), tick)

    @classmethod
    def make_end_of_track(cls, tick: int = 0) -> "MetaEvent":
        return cls(0x2F, b"", tick)

    @classmethod
    def make_sequence_number(cls, tick: int, number: int) -> "MetaEvent":
        return cls(0x00, bytes([(int(number) >> 8) & 0xFF, int(number) & 0xFF]), tick)

    @classmethod
    def make_channel_prefix(cls, tick: int, channel: int) -> "MetaEvent":
        return cls(0x20, bytes([int(channel) & 0x0F]), tick)

    @classmethod
    def make_port(cls, tick: int, port: int) -> "MetaEvent":
        return cls(0x21, bytes([int(port) & 0xFF]), tick)

    @classmethod
    def make_smpte_offset(
        cls,
        tick: int = 0,
        hour: int = 0,
        minute: int = 0,
        second: int = 0,
        frame: int = 0,
        subframe: int = 0,
        frame_rate: int = 24,
    ) -> "MetaEvent":
        rate_bits = {24: 0, 25: 1, 30: 3}.get(int(frame_rate), 2 if int(frame_rate) == 29 else 0)
        hr = ((rate_bits & 0x03) << 5) | (int(hour) & 0x1F)
        return cls(0x54, bytes([hr, int(minute) & 0x3F, int(second) & 0x3F, int(frame) & 0x1F, int(subframe) & 0x63]), tick)

    @classmethod
    def make_sequencer_specific(cls, tick: int, data: bytes) -> "MetaEvent":
        return cls(0x7F, data, tick)

    # -- introspection -------------------------------------------------
    @property
    def is_meta(self) -> bool:
        return True

    @property
    def meta_type(self) -> int:
        return self.type_byte

    @property
    def name(self) -> str:
        return META_TYPE_NAMES.get(self.type_byte, "unknown_0x%02X" % self.type_byte)

    def sort_priority(self) -> int:
        return SORT_META

    def clone(self) -> "MetaEvent":
        return MetaEvent(self.type_byte, self.data, self.tick)

    # -- decoded payload ----------------------------------------------
    @property
    def text(self) -> Optional[str]:
        if self.type_byte in TEXT_META_TYPES:
            return decode_text(self.data)[0]
        return None

    @text.setter
    def text(self, value: str) -> None:
        self.data = encode_text(value, "utf-8")

    @property
    def tempo(self) -> Optional[int]:
        if self.type_byte == 0x51 and len(self.data) == 3:
            return (self.data[0] << 16) | (self.data[1] << 8) | self.data[2]
        return None

    @property
    def bpm(self) -> Optional[float]:
        t = self.tempo
        return None if not t else 60000000.0 / t

    @property
    def time_signature(self) -> Optional[Tuple[int, int, int, int]]:
        if self.type_byte == 0x58 and len(self.data) == 4:
            return (self.data[0], 1 << self.data[1], self.data[2], self.data[3])
        return None

    @property
    def key_signature(self) -> Optional[Tuple[int, int]]:
        if self.type_byte == 0x59 and len(self.data) == 2:
            sf = self.data[0] - 256 if self.data[0] > 127 else self.data[0]
            return (sf, self.data[1])
        return None

    @property
    def sequence_number(self) -> Optional[int]:
        if self.type_byte == 0x00 and len(self.data) == 2:
            return (self.data[0] << 8) | self.data[1]
        return None

    @property
    def smpte_offset(self) -> Optional[Dict[str, Any]]:
        if self.type_byte == 0x54 and len(self.data) == 5:
            hr, mn, se, fr, ff = self.data
            rate_bits = (hr >> 5) & 0x03
            return {
                "frame_rate": SMPTE_RATE_INDEX[rate_bits],
                "hour": hr & 0x1F,
                "minute": mn,
                "second": se,
                "frame": fr,
                "subframe": ff,
            }
        return None

    @property
    def channel(self) -> Optional[int]:
        if self.type_byte == 0x20 and self.data:
            return self.data[0]
        return None

    @property
    def port(self) -> Optional[int]:
        if self.type_byte == 0x21 and self.data:
            return self.data[0]
        return None

    def is_end_of_track(self) -> bool:
        return self.type_byte == 0x2F

    # -- serialization -------------------------------------------------
    def to_dict(self, opts: Optional[DecodeOptions] = None) -> Dict[str, Any]:
        o = opts or DEFAULT_OPTS
        name = META_TYPE_NAMES.get(self.type_byte)
        d: Dict[str, Any] = {"tick": self.tick, "type": "meta"}
        if name:
            d["meta_type"] = name
        else:
            d["meta_type"] = "unknown"
            d["meta_type_byte"] = self.type_byte
        if self.type_byte in TEXT_META_TYPES:
            text, used, clean = decode_text(self.data, o.encoding)
            d["text"] = text
            if not clean and self.data:
                d["text_hex"] = hex_bytes(self.data)
                d["text_encoding"] = used
        elif self.type_byte == 0x51:
            d["tempo"] = self.tempo
            if o.names:
                d["bpm"] = round(self.bpm, 4)
        elif self.type_byte == 0x58:
            ts = self.time_signature
            if ts:
                d["numerator"], d["denominator"], d["clocks_per_click"], d["notated_32nd"] = ts
            else:
                d["data_hex"] = hex_bytes(self.data)
        elif self.type_byte == 0x59:
            ks = self.key_signature
            if ks:
                d["sf"], d["mi"] = ks
                if o.names:
                    d["key"] = KEY_SIGNATURES.get(ks[0], ("?", "?"))[1 if ks[1] else 0]
            else:
                d["data_hex"] = hex_bytes(self.data)
        elif self.type_byte == 0x54:
            so = self.smpte_offset
            if so:
                d.update(so)
            else:
                d["data_hex"] = hex_bytes(self.data)
        elif self.type_byte == 0x00:
            d["number"] = self.sequence_number
            if d["number"] is None:
                d["data_hex"] = hex_bytes(self.data)
        elif self.type_byte in (0x20, 0x21):
            key = "channel" if self.type_byte == 0x20 else "port"
            d[key] = self.data[0] if self.data else None
        elif self.type_byte == 0x2F:
            pass
        else:
            d["data_hex"] = hex_bytes(self.data)
            d["length"] = len(self.data)
        return d

    def describe(self, opts: Optional[DecodeOptions] = None) -> str:
        o = opts or DEFAULT_OPTS
        if self.type_byte in TEXT_META_TYPES:
            text = self.text or ""
            return "%-14s %s" % (self.name, _clip(text))
        if self.type_byte == 0x51:
            return "%-14s %d us/qn (%.3f bpm)" % (self.name, self.tempo or 0, self.bpm or 0.0)
        if self.type_byte == 0x58:
            ts = self.time_signature
            if ts:
                return "%-14s %d/%d" % (self.name, ts[0], ts[1])
        if self.type_byte == 0x59:
            ks = self.key_signature
            if ks:
                return "%-14s %s (sf=%d mi=%d)" % (
                    self.name, KEY_SIGNATURES.get(ks[0], ("?", "?"))[1 if ks[1] else 0], ks[0], ks[1],
                )
        if self.type_byte == 0x7F:
            return "%-14s %d bytes %s" % (self.name, len(self.data), _clip(hex_bytes(self.data)))
        return "%-14s %s" % (self.name, _clip(hex_bytes(self.data)))

    def to_bytes(self, running_status: Optional[int] = None) -> Tuple[bytes, Optional[int]]:
        out = bytes([0xFF, self.type_byte]) + _vlq(len(self.data)) + self.data
        return out, None  # meta events cancel running status

    def fingerprint(self, include_tick: bool = True) -> Tuple:
        base = ("meta", self.type_byte, self.data)
        return (self.tick,) + base if include_tick else base


# ---------------------------------------------------------------------------
# sysex / escape
# ---------------------------------------------------------------------------


class SysExEvent(Event):
    """0xF0 (normal sysex) or 0xF7 (escape / continuation) event."""

    __slots__ = ("escape", "data")

    kind = "sysex"

    def __init__(self, data: bytes = b"", escape: bool = False, tick: int = 0) -> None:
        super().__init__(tick)
        self.escape = bool(escape)
        self.data = bytes(data)

    @property
    def status(self) -> int:
        return 0xF7 if self.escape else 0xF0

    @property
    def is_sysex(self) -> bool:
        return True

    @property
    def type(self) -> str:
        return "escape" if self.escape else "sysex"

    def sort_priority(self) -> int:
        return SORT_SYSEX

    def clone(self) -> "SysExEvent":
        return SysExEvent(self.data, self.escape, self.tick)

    @property
    def manufacturer_id(self) -> Optional[int]:
        if self.escape or not self.data:
            return None
        return self.data[0]

    @property
    def manufacturer(self) -> Optional[str]:
        from .constants import MANUFACTURER_IDS

        mid = self.manufacturer_id
        return None if mid is None else MANUFACTURER_IDS.get(mid)

    def to_dict(self, opts: Optional[DecodeOptions] = None) -> Dict[str, Any]:
        o = opts or DEFAULT_OPTS
        d: Dict[str, Any] = {
            "tick": self.tick,
            "type": "sysex",
            "escape": self.escape,
            "data_hex": hex_bytes(self.data),
            "length": len(self.data),
        }
        if o.full_data:
            d["data"] = list(self.data)
        if o.names and not self.escape and self.data:
            from .constants import MANUFACTURER_IDS

            name = MANUFACTURER_IDS.get(self.data[0])
            if name:
                d["manufacturer"] = name
        return d

    def describe(self, opts: Optional[DecodeOptions] = None) -> str:
        return "%-7s %d bytes %s" % (self.type, len(self.data), _clip(hex_bytes(self.data)))

    def to_bytes(self, running_status: Optional[int] = None) -> Tuple[bytes, Optional[int]]:
        out = bytes([self.status]) + _vlq(len(self.data)) + self.data
        return out, None

    def fingerprint(self, include_tick: bool = True) -> Tuple:
        base = ("sysex", self.escape, self.data)
        return (self.tick,) + base if include_tick else base


class SystemEvent(Event):
    """Non-standard system common / real-time message found inside a track."""

    __slots__ = ("status_byte", "data")

    kind = "system"

    def __init__(self, status: int, data: bytes = b"", tick: int = 0) -> None:
        super().__init__(tick)
        self.status_byte = int(status)
        self.data = bytes(data)

    @property
    def status(self) -> int:
        return self.status_byte

    @property
    def name(self) -> str:
        return SYSTEM_STATUS_NAMES.get(self.status_byte, "system_0x%02X" % self.status_byte)

    def sort_priority(self) -> int:
        return SORT_SYSTEM

    def clone(self) -> "SystemEvent":
        return SystemEvent(self.status_byte, self.data, self.tick)

    def to_dict(self, opts: Optional[DecodeOptions] = None) -> Dict[str, Any]:
        o = opts or DEFAULT_OPTS
        d: Dict[str, Any] = {
            "tick": self.tick,
            "type": "system",
            "status": self.status_byte,
            "status_hex": "0x%02X" % self.status_byte,
            "name": self.name,
            "data_hex": hex_bytes(self.data),
        }
        if o.full_data:
            d["data"] = list(self.data)
        return d

    def describe(self, opts: Optional[DecodeOptions] = None) -> str:
        return "%-16s %s" % (self.name, _clip(hex_bytes(self.data)))

    def to_bytes(self, running_status: Optional[int] = None) -> Tuple[bytes, Optional[int]]:
        return bytes([self.status_byte]) + self.data, None

    def fingerprint(self, include_tick: bool = True) -> Tuple:
        base = ("system", self.status_byte, self.data)
        return (self.tick,) + base if include_tick else base


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _check_data_byte(value: int, label: str) -> int:
    value = int(value)
    if not 0 <= value <= 127:
        raise SchemaError("%s out of range 0-127: %r" % (label, value))
    return value


def _vlq(value: int) -> bytes:
    from .bytestream import vlq_bytes

    return vlq_bytes(value)


def _clip(text: str, width: int = 48) -> str:
    text = text.replace("\r", " ").replace("\n", " ")
    return text if len(text) <= width else text[: width - 3] + "..."


def canonical_type(name: Any) -> str:
    """Map aliases / status bytes / type names to a canonical event type."""
    if isinstance(name, int):
        if name >= 0xF0:
            return {0xF0: "sysex", 0xF7: "escape", 0xFF: "meta"}.get(name, "system")
        nib = name & 0xF0
        if nib in CHANNEL_MESSAGES:
            return CHANNEL_MESSAGES[nib][0]
        raise SchemaError("unknown status byte: 0x%02X" % name)
    if name is None:
        raise SchemaError("event type is required")
    key = str(name).strip().lower().replace(" ", "_")
    key = TYPE_ALIASES.get(key, key)
    if key in TYPE_TO_STATUS or key in ("meta", "sysex", "escape", "system"):
        return key
    raise SchemaError("unknown event type: %r" % (name,))


def meta_type_number(meta_type: Any) -> int:
    if isinstance(meta_type, bool):
        raise SchemaError("bad meta type: %r" % (meta_type,))
    if isinstance(meta_type, int):
        if not 0 <= meta_type <= 0x7F:
            raise SchemaError("meta type byte out of range 0-127: %r" % (meta_type,))
        return int(meta_type)
    if isinstance(meta_type, str):
        key = meta_type.strip().lower().replace(" ", "_").replace("-", "_")
        if key in META_NAME_TO_TYPE:
            return META_NAME_TO_TYPE[key]
        if key.startswith("0x"):
            return int(key, 16)
        if key.isdigit():
            return int(key)
        if key.startswith("unknown_"):
            return int(key.split("_")[-1], 16)
    raise SchemaError("unknown meta event type: %r" % (meta_type,))


def bpm_to_tempo(bpm: float) -> int:
    bpm = float(bpm)
    if bpm <= 0:
        raise SchemaError("bpm must be > 0: %r" % (bpm,))
    return int(round(60000000.0 / bpm))


def tempo_to_bpm(tempo: int) -> float:
    return 60000000.0 / float(tempo) if tempo else 0.0


def denominator_to_power(denominator: int) -> int:
    denominator = int(denominator)
    if denominator <= 0 or (denominator & (denominator - 1)) != 0:
        raise SchemaError("time signature denominator must be a power of two: %r" % (denominator,))
    power = 0
    while (1 << power) < denominator:
        power += 1
    if power > 255:
        raise SchemaError("time signature denominator too large: %r" % (denominator,))
    return power


def key_signature_from_name(key: str) -> Tuple[int, int]:
    from .constants import KEY_NAME_TO_SF

    k = str(key).strip().lower()
    if k in KEY_NAME_TO_SF:
        return KEY_NAME_TO_SF[k]
    k2 = k.replace(" ", "")
    if k2 in KEY_NAME_TO_SF:
        return KEY_NAME_TO_SF[k2]
    raise SchemaError("unknown key signature: %r" % (key,))


def event_from_dict(d: Dict[str, Any], opts: Optional[DecodeOptions] = None) -> Event:
    """Build an Event from its JSON representation (inverse of Event.to_dict)."""
    if not isinstance(d, dict):
        raise SchemaError("event must be an object, got %s" % type(d).__name__)
    o = opts or DEFAULT_OPTS
    tick = int(d.get("tick", d.get("at", d.get("time", 0))) or 0)
    raw_type = d.get("type", d.get("kind"))
    etype = canonical_type(raw_type)

    if etype == "meta":
        return _meta_from_dict(d, tick, o)
    if etype in ("sysex", "escape"):
        data = _payload_bytes(d, allow_missing=True)
        escape = etype == "escape" or bool(d.get("escape", False))
        return SysExEvent(data, escape, tick)
    if etype == "system":
        status = d.get("status")
        if status is None:
            raise SchemaError("system event needs a status byte")
        return SystemEvent(int(status), _payload_bytes(d, allow_missing=True), tick)

    # channel voice
    channel = _channel_from(d)
    if etype in ("note_on", "note_off"):
        note = d.get("note", d.get("pitch", d.get("key", d.get("data1"))))
        if note is None:
            raise SchemaError("%s event needs a note" % etype)
        velocity = d.get("velocity", d.get("vel", d.get("data2", 64)))
        return ChannelEvent(etype, channel, note_number(note), int(velocity), tick)
    if etype == "poly_aftertouch":
        note = d.get("note", d.get("pitch", d.get("data1")))
        if note is None:
            raise SchemaError("poly_aftertouch event needs a note")
        pressure = d.get("pressure", d.get("value", d.get("data2", 0)))
        return ChannelEvent(etype, channel, note_number(note), int(pressure), tick)
    if etype == "control_change":
        control = d.get("control", d.get("cc", d.get("controller", d.get("data1"))))
        if control is None and d.get("control_name") is not None:
            control = _cc(d["control_name"])
        if control is None:
            raise SchemaError("control_change event needs a control number")
        value = d.get("value", d.get("data2", 0))
        return ChannelEvent(etype, channel, control if isinstance(control, int) else _cc(control), int(value), tick)
    if etype == "program_change":
        program = d.get("program", d.get("patch", d.get("data1")))
        if program is None:
            program = _program(d.get("program_name", d.get("program_number")))
        return ChannelEvent(etype, channel, int(program), None, tick)
    if etype == "channel_aftertouch":
        return ChannelEvent(etype, channel, int(d.get("pressure", d.get("value", d.get("data1", 0)))), None, tick)
    if etype == "pitch_bend":
        if "value" in d and d["value"] is not None:
            ev = ChannelEvent(etype, channel, 0, 0, tick)
            ev.value = int(d["value"])
            return ev
        if "bend" in d and d["bend"] is not None:
            ev = ChannelEvent(etype, channel, 0, 0, tick)
            ev.bend = int(d["bend"])
            return ev
        if "semitones" in d and d["semitones"] is not None:
            ev = ChannelEvent(etype, channel, 0, 0, tick)
            ev.bend = int(round(float(d["semitones"]) / float(o.bend_range) * 8192.0))
            return ev
        if "data1" in d:
            return ChannelEvent(etype, channel, int(d["data1"]), int(d.get("data2", 0)), tick)
        raise SchemaError("pitch_bend event needs value, bend or semitones")
    raise SchemaError("unsupported event type: %r" % (etype,))


def _meta_from_dict(d: Dict[str, Any], tick: int, o: DecodeOptions) -> MetaEvent:
    meta_type = d.get("meta_type", d.get("meta", d.get("subtype")))
    if meta_type is None or (isinstance(meta_type, str) and meta_type.strip().lower() == "unknown"):
        meta_type = d.get("meta_type_byte", d.get("meta_byte"))
    if meta_type is None:
        raise SchemaError("meta event needs meta_type")
    type_byte = meta_type_number(meta_type)

    if "data" in d and d["data"] is not None:
        data = _payload_bytes(d, allow_missing=False)
        return MetaEvent(type_byte, data, tick)
    if "data_hex" in d and d["data_hex"] is not None:
        return MetaEvent(type_byte, bytes_from_hex(str(d["data_hex"])), tick)
    if "hex" in d and d["hex"] is not None:
        return MetaEvent(type_byte, bytes_from_hex(str(d["hex"])), tick)

    if type_byte in TEXT_META_TYPES:
        if "text" in d and d["text"] is not None:
            return MetaEvent(type_byte, encode_text(str(d["text"]), d.get("encoding") or o.encoding), tick)
        return MetaEvent(type_byte, b"", tick)
    if type_byte == 0x51:
        return MetaEvent.make_tempo(tick, tempo=d.get("tempo"), bpm=d.get("bpm"))
    if type_byte == 0x58:
        return MetaEvent.make_time_signature(
            tick,
            numerator=int(d.get("numerator", d.get("nn", 4))),
            denominator=int(d.get("denominator", 4)),
            clocks_per_click=int(d.get("clocks_per_click", d.get("cc", 24))),
            notated_32nd_per_beat=int(d.get("notated_32nd", d.get("bb", 8))),
        )
    if type_byte == 0x59:
        return MetaEvent.make_key_signature(tick, sf=int(d.get("sf", 0)), mi=int(d.get("mi", 0)), key=d.get("key"))
    if type_byte == 0x00:
        number = d.get("number", d.get("sequence_number", 0))
        return MetaEvent.make_sequence_number(tick, int(number or 0))
    if type_byte == 0x20:
        return MetaEvent.make_channel_prefix(tick, int(d.get("channel", 0)))
    if type_byte == 0x21:
        return MetaEvent.make_port(tick, int(d.get("port", 0)))
    if type_byte == 0x54:
        return MetaEvent.make_smpte_offset(
            tick,
            hour=int(d.get("hour", 0)),
            minute=int(d.get("minute", 0)),
            second=int(d.get("second", 0)),
            frame=int(d.get("frame", 0)),
            subframe=int(d.get("subframe", 0)),
            frame_rate=int(d.get("frame_rate", 24)),
        )
    if type_byte == 0x2F:
        return MetaEvent.make_end_of_track(tick)
    return MetaEvent(type_byte, b"", tick)


def _payload_bytes(d: Dict[str, Any], allow_missing: bool = True) -> bytes:
    if "data" in d and d["data"] is not None:
        data = d["data"]
        if isinstance(data, str):
            return bytes_from_hex(data)
        return bytes(bytearray(int(b) & 0xFF for b in data))
    if "data_hex" in d and d["data_hex"] is not None:
        return bytes_from_hex(str(d["data_hex"]))
    if "hex" in d and d["hex"] is not None:
        return bytes_from_hex(str(d["hex"]))
    if allow_missing:
        return b""
    raise SchemaError("event needs data or data_hex")


def _channel_from(d: Dict[str, Any]) -> int:
    if "channel" in d and d["channel"] is not None:
        return int(d["channel"])
    if "channel_display" in d and d["channel_display"] is not None:
        return int(d["channel_display"]) - 1
    if "ch" in d and d["ch"] is not None:
        return int(d["ch"])
    return 0


def _cc(value: Any) -> int:
    from .constants import cc_number

    return cc_number(value)


def _program(value: Any) -> int:
    if value is None:
        return 0
    return program_number(value)
