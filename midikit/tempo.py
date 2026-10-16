"""Tick / beat / bar / second conversion driven by the file tempo map."""

from __future__ import annotations

import bisect
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .constants import DEFAULT_TEMPO, DEFAULT_TIMESIG
from .smf import Division, MidiFile


class TempoMap:
    """Resolves absolute ticks to seconds, beats and bar:beat positions.

    Tempo changes are gathered from every track (as most sequencers do),
    duplicate ticks keep the last value seen.
    """

    def __init__(
        self,
        division: Division,
        tempos: Optional[Sequence[Tuple[int, int]]] = None,
        time_signatures: Optional[Sequence[Tuple[int, int, int]]] = None,
    ) -> None:
        self.division = division
        self._entries: List[Tuple[int, int]] = []
        self._times: List[float] = []
        self._time_sigs: List[Tuple[int, int, int]] = []
        self._build_tempos(tempos or [])
        self._build_time_signatures(time_signatures or [])

    # -- construction ----------------------------------------------------
    @classmethod
    def from_midi(cls, midi: MidiFile) -> "TempoMap":
        tempos: List[Tuple[int, int]] = []
        sigs: List[Tuple[int, int, int]] = []
        for track in midi.tracks:
            for ev in track.events:
                if not ev.is_meta:
                    continue
                if ev.type_byte == 0x51 and len(ev.data) == 3:
                    value = (ev.data[0] << 16) | (ev.data[1] << 8) | ev.data[2]
                    if value > 0:
                        tempos.append((ev.tick, value))
                elif ev.type_byte == 0x58 and len(ev.data) == 4:
                    sigs.append((ev.tick, ev.data[0], 1 << ev.data[1]))
        return cls(midi.division, tempos, sigs)

    def _build_tempos(self, tempos: Sequence[Tuple[int, int]]) -> None:
        seen: Dict[int, int] = {}
        for tick, tempo in tempos:
            seen[int(tick)] = int(tempo)
        if 0 not in seen:
            seen[0] = DEFAULT_TEMPO
        self._entries = sorted(seen.items())
        ppq = self.division.ticks_per_beat
        self._times = []
        cumulative = 0.0
        for index, (tick, tempo) in enumerate(self._entries):
            if index > 0:
                prev_tick, prev_tempo = self._entries[index - 1]
                cumulative += (tick - prev_tick) * prev_tempo / (ppq * 1_000_000.0)
            self._times.append(cumulative)

    def _build_time_signatures(self, sigs: Sequence[Tuple[int, int, int]]) -> None:
        seen: Dict[int, Tuple[int, int]] = {}
        for tick, numerator, denominator in sigs:
            if numerator > 0 and denominator > 0:
                seen[int(tick)] = (int(numerator), int(denominator))
        if 0 not in seen:
            seen[0] = (DEFAULT_TIMESIG[0], DEFAULT_TIMESIG[1])
        self._time_sigs = sorted((tick, num, den) for tick, (num, den) in seen.items())

    # -- tempo -----------------------------------------------------------
    @property
    def tempos(self) -> List[Tuple[int, int]]:
        return list(self._entries)

    def tempo_at(self, tick: int) -> int:
        index = bisect.bisect_right([e[0] for e in self._entries], int(tick)) - 1
        return self._entries[max(index, 0)][1]

    def bpm_at(self, tick: int) -> float:
        tempo = self.tempo_at(tick)
        return 60000000.0 / tempo if tempo else 0.0

    @property
    def initial_tempo(self) -> int:
        return self._entries[0][1]

    @property
    def initial_bpm(self) -> float:
        return 60000000.0 / self._entries[0][1]

    # -- time ------------------------------------------------------------
    def tick_to_seconds(self, tick: int) -> float:
        tick = int(tick)
        if self.division.is_smpte:
            return tick / float(self.division.ticks_per_second or 1.0)
        ppq = self.division.ticks_per_beat
        index = bisect.bisect_right([e[0] for e in self._entries], tick) - 1
        index = max(index, 0)
        base_tick, tempo = self._entries[index]
        return self._times[index] + (tick - base_tick) * tempo / (ppq * 1_000_000.0)

    def seconds_to_tick(self, seconds: float) -> int:
        seconds = float(seconds)
        if self.division.is_smpte:
            return int(round(seconds * float(self.division.ticks_per_second or 1.0)))
        ppq = self.division.ticks_per_beat
        index = bisect.bisect_right(self._times, seconds) - 1
        index = max(index, 0)
        base_tick, tempo = self._entries[index]
        return int(round(base_tick + (seconds - self._times[index]) * ppq * 1_000_000.0 / tempo))

    def ticks_to_seconds(self, ticks: int) -> float:
        return self.tick_to_seconds(ticks)

    # -- beats / bars ----------------------------------------------------
    def tick_to_beat(self, tick: int) -> float:
        return float(tick) / self.division.ticks_per_beat

    def beat_to_tick(self, beat: float) -> int:
        return int(round(float(beat) * self.division.ticks_per_beat))

    def bar_ticks(self, numerator: int, denominator: int) -> float:
        return float(numerator) * 4.0 * self.division.ticks_per_beat / float(denominator)

    def time_signature_at(self, tick: int) -> Tuple[int, int]:
        index = bisect.bisect_right([s[0] for s in self._time_sigs], int(tick)) - 1
        entry = self._time_sigs[max(index, 0)]
        return (entry[1], entry[2])

    @property
    def time_signatures(self) -> List[Tuple[int, int, int]]:
        return list(self._time_sigs)

    def bar_beat_tick(self, tick: int) -> Dict[str, Any]:
        """1-based bar and beat plus the tick offset inside the beat."""
        tick = int(tick)
        ppq = self.division.ticks_per_beat
        bar = 1
        carry = 0.0
        numerator, denominator = self._time_sigs[0][1], self._time_sigs[0][2]
        within = float(tick)
        for index, (start, num, den) in enumerate(self._time_sigs):
            end = self._time_sigs[index + 1][0] if index + 1 < len(self._time_sigs) else None
            numerator, denominator = num, den
            bar_ticks = self.bar_ticks(num, den)
            if bar_ticks <= 0:
                continue
            if end is None or tick < end:
                offset = (tick - start) + carry
                completed = int(offset // bar_ticks)
                bar += completed
                within = offset - completed * bar_ticks
                break
            span = (end - start) + carry
            completed = int(span // bar_ticks)
            carry = span - completed * bar_ticks
            bar += completed
        else:
            within = 0.0
        beat_ticks = 4.0 * ppq / float(denominator or 4)
        beat = int(within // beat_ticks) + 1
        offset_in_beat = within - (beat - 1) * beat_ticks
        return {
            "bar": bar,
            "beat": beat,
            "tick_in_beat": int(round(offset_in_beat)),
            "numerator": numerator,
            "denominator": denominator,
            "bar_start_tick": int(tick - within),
        }

    def bar_start_tick(self, bar: int) -> int:
        target = max(1, int(bar))
        tick = 0
        carry = 0.0
        current_bar = 1
        index = 0
        while True:
            if index < len(self._time_sigs):
                start, num, den = self._time_sigs[index]
                end = self._time_sigs[index + 1][0] if index + 1 < len(self._time_sigs) else None
            else:
                start, num, den = self._time_sigs[-1]
                end = None
            bar_ticks = self.bar_ticks(num, den)
            segment_start = start - carry
            if end is None or (current_bar + int(((end - segment_start) // bar_ticks) or 0)) >= target:
                return int(round(segment_start + (target - current_bar) * bar_ticks))
            span = end - segment_start
            completed = int(span // bar_ticks)
            carry = span - completed * bar_ticks
            current_bar += completed
            index += 1
            if index > len(self._time_sigs) + 1:
                return int(round(start + (target - current_bar) * bar_ticks))

    # -- formatting ------------------------------------------------------
    @staticmethod
    def format_seconds(seconds: float) -> str:
        negative = seconds < 0
        seconds = abs(float(seconds))
        minutes = int(seconds // 60)
        rest = seconds - minutes * 60
        text = "%d:%06.3f" % (minutes, rest)
        return ("-" + text) if negative else text

    def describe(self) -> str:
        parts = [self.division.describe(), "%.3f bpm at tick 0" % self.initial_bpm]
        if len(self._entries) > 1:
            parts.append("%d tempo change(s)" % (len(self._entries) - 1))
        if len(self._time_sigs) > 1:
            parts.append("%d time signature change(s)" % (len(self._time_sigs) - 1))
        return ", ".join(parts)

    def __repr__(self) -> str:
        return "TempoMap(%s)" % self.describe()


UNIT_ALIASES = {
    "tick": "tick",
    "ticks": "tick",
    "t": "tick",
    "beat": "beat",
    "beats": "beat",
    "b": "beat",
    "quarter": "beat",
    "quarters": "beat",
    "bar": "bar",
    "bars": "bar",
    "measure": "bar",
    "measures": "bar",
    "second": "second",
    "seconds": "second",
    "sec": "second",
    "s": "second",
    "ms": "ms",
    "millisecond": "ms",
    "milliseconds": "ms",
}


def normalize_unit(unit: str) -> str:
    key = str(unit or "tick").strip().lower()
    if key not in UNIT_ALIASES:
        raise ValueError("unknown time unit: %r" % (unit,))
    return UNIT_ALIASES[key]


def parse_time(value, tempo_map: Optional[TempoMap] = None, default_unit: str = "tick") -> int:
    """Parse a time value into ticks.

    Accepts ints/floats, and strings with a unit suffix:
    480t / 480ticks, 2b / 2beats / 0.5bar, 250ms, 1.5s, or '2:3:120'.
    """
    if value is None:
        raise ValueError("time value is required")
    if isinstance(value, bool):
        raise ValueError("bad time value: %r" % (value,))
    default_unit = normalize_unit(default_unit)
    if isinstance(value, (int, float)):
        number = float(value)
        unit = default_unit
    else:
        text = str(value).strip().lower().replace(" ", "")
        if not text:
            raise ValueError("empty time value")
        if text.count(":") == 2:
            bar, beat, tick = (int(part) for part in text.split(":"))
            if tempo_map is None:
                raise ValueError("bar:beat:tick needs a tempo map")
            base = tempo_map.bar_start_tick(bar)
            num, den = tempo_map.time_signature_at(base)
            beat_ticks = 4.0 * tempo_map.division.ticks_per_beat / float(den)
            return int(round(base + (beat - 1) * beat_ticks + tick))
        match = re.match(r"^([-+]?(?:\d+\.?\d*|\.\d+))([a-zA-Z]*)$", text)
        if not match:
            raise ValueError(
                "bad time value: %r (use a number with an optional unit: 480t, 2b, 1bar, 250ms, 1.5s)"
                % (value,)
            )
        number = float(match.group(1))
        suffix = match.group(2)
        unit = normalize_unit(suffix) if suffix else default_unit
    if tempo_map is None:
        if unit in ("second", "ms"):
            raise ValueError("seconds need a tempo map")
        if unit == "bar":
            raise ValueError("bars need a tempo map")
        if unit == "beat":
            raise ValueError("beats need a tempo map (ticks per quarter note)")
        return int(round(number))
    if unit == "tick":
        return int(round(number))
    if unit == "beat":
        return tempo_map.beat_to_tick(number)
    if unit == "bar":
        num, den = tempo_map.time_signature_at(0)
        return int(round(number * tempo_map.bar_ticks(num, den)))
    if unit == "second":
        return tempo_map.seconds_to_tick(number)
    if unit == "ms":
        return tempo_map.seconds_to_tick(number / 1000.0)
    raise ValueError("unknown time unit: %r" % (unit,))
