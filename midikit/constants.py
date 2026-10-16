"""Standard MIDI File / General MIDI constant tables and lookups."""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from .errors import SchemaError

# ---------------------------------------------------------------------------
# Header / structural constants
# ---------------------------------------------------------------------------

HEADER_CHUNK = b"MThd"
TRACK_CHUNK = b"MTrk"

FORMAT_SINGLE_TRACK = 0
FORMAT_MULTI_TRACK = 1
FORMAT_MULTI_SONG = 2

DEFAULT_TEMPO = 500000  # microseconds per quarter note == 120 BPM
DEFAULT_PPQ = 480
DEFAULT_TIMESIG: Tuple[int, int, int, int] = (4, 4, 24, 8)

#: Valid SMPTE frame rates encoded in the high byte of a division field.
SMPTE_FRAME_RATES = (-24, -25, -29, -30)
SMPTE_RATE_INDEX = {0: 24, 1: 25, 2: 29.97, 3: 30}  # SMPTE offset hr field

# ---------------------------------------------------------------------------
# Channel voice messages
# ---------------------------------------------------------------------------

#: status nibble -> (canonical type name, number of data bytes)
CHANNEL_MESSAGES: Dict[int, Tuple[str, int]] = {
    0x80: ("note_off", 2),
    0x90: ("note_on", 2),
    0xA0: ("poly_aftertouch", 2),
    0xB0: ("control_change", 2),
    0xC0: ("program_change", 1),
    0xD0: ("channel_aftertouch", 1),
    0xE0: ("pitch_bend", 2),
}

#: canonical message name -> base status byte (0x80 .. 0xE0)
TYPE_TO_STATUS: Dict[str, int] = {name: base for base, (name, _n) in CHANNEL_MESSAGES.items()}

#: Accept the vocabulary other libraries use, so agents can be sloppy.
TYPE_ALIASES: Dict[str, str] = {
    "noteoff": "note_off",
    "note-on": "note_on",
    "noteon": "note_on",
    "polytouch": "poly_aftertouch",
    "poly_aftertouch": "poly_aftertouch",
    "polyphonic_aftertouch": "poly_aftertouch",
    "aftertouch": "channel_aftertouch",
    "channel_pressure": "channel_aftertouch",
    "cc": "control_change",
    "control": "control_change",
    "controller": "control_change",
    "program": "program_change",
    "patch": "program_change",
    "pitchwheel": "pitch_bend",
    "pitch_wheel": "pitch_bend",
    "bend": "pitch_bend",
    "meta": "meta",
    "meta_event": "meta",
    "sysex": "sysex",
    "system_exclusive": "sysex",
    "escape": "escape",
    "system": "system",
    "raw": "system",
    "note": "note_on",
}

#: Non-standard (but occasionally present) system messages inside a track.
SYSTEM_MESSAGE_DATA_LEN: Dict[int, int] = {
    0xF1: 1,
    0xF2: 2,
    0xF3: 1,
    0xF4: 0,
    0xF5: 0,
    0xF6: 0,
    0xF8: 0,
    0xF9: 0,
    0xFA: 0,
    0xFB: 0,
    0xFC: 0,
    0xFD: 0,
    0xFE: 0,
}

SYSTEM_STATUS_NAMES: Dict[int, str] = {
    0xF1: "mtc_quarter_frame",
    0xF2: "song_position",
    0xF3: "song_select",
    0xF4: "undefined_f4",
    0xF5: "undefined_f5",
    0xF6: "tune_request",
    0xF8: "timing_clock",
    0xF9: "undefined_f9",
    0xFA: "start",
    0xFB: "continue",
    0xFC: "stop",
    0xFD: "undefined_fd",
    0xFE: "active_sensing",
}

# ---------------------------------------------------------------------------
# Meta events
# ---------------------------------------------------------------------------

META_TYPE_NAMES: Dict[int, str] = {
    0x00: "sequence_number",
    0x01: "text",
    0x02: "copyright",
    0x03: "track_name",
    0x04: "instrument_name",
    0x05: "lyric",
    0x06: "marker",
    0x07: "cue_point",
    0x08: "program_name",
    0x09: "device_name",
    0x20: "channel_prefix",
    0x21: "port",
    0x2F: "end_of_track",
    0x51: "set_tempo",
    0x54: "smpte_offset",
    0x58: "time_signature",
    0x59: "key_signature",
    0x7F: "sequencer_specific",
}

META_NAME_TO_TYPE: Dict[str, int] = {v: k for k, v in META_TYPE_NAMES.items()}
META_NAME_TO_TYPE.update(
    {
        "sequence": 0x00,
        "seq_number": 0x00,
        "trackname": 0x03,
        "track_title": 0x03,
        "instrument": 0x04,
        "instrumentname": 0x04,
        "lyrics": 0x05,
        "cue": 0x07,
        "cuepoint": 0x07,
        "patch_name": 0x08,
        "port_name": 0x09,
        "midi_channel_prefix": 0x20,
        "midi_port": 0x21,
        "eot": 0x2F,
        "end_of_track": 0x2F,
        "tempo": 0x51,
        "smpte": 0x54,
        "time_signature": 0x58,
        "timesig": 0x58,
        "key_signature": 0x59,
        "keysig": 0x59,
        "sequencer_specific": 0x7F,
        "seq_specific": 0x7F,
    }
)

#: Meta types whose payload is human readable text.
TEXT_META_TYPES = frozenset([0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09])

#: Expected payload length for fixed-size meta events (None == variable).
META_FIXED_LENGTH: Dict[int, Optional[int]] = {
    0x00: 2,
    0x20: 1,
    0x21: 1,
    0x2F: 0,
    0x51: 3,
    0x54: 5,
    0x58: 4,
    0x59: 2,
}

# ---------------------------------------------------------------------------
# Control change names
# ---------------------------------------------------------------------------

_CC_UNDEFINED = "undefined"
CC_NAMES: Dict[int, str] = {}
for _i in range(128):
    CC_NAMES[_i] = "cc_%d" % _i
CC_NAMES.update(
    {
        0: "bank_select_msb",
        1: "modulation_wheel",
        2: "breath_controller",
        4: "foot_controller",
        5: "portamento_time",
        6: "data_entry_msb",
        7: "channel_volume",
        8: "balance",
        10: "pan",
        11: "expression",
        12: "effect_control_1",
        13: "effect_control_2",
        16: "general_purpose_1",
        17: "general_purpose_2",
        18: "general_purpose_3",
        19: "general_purpose_4",
        32: "bank_select_lsb",
        33: "modulation_wheel_lsb",
        64: "damper_pedal",
        65: "portamento_on_off",
        66: "sostenuto",
        67: "soft_pedal",
        68: "legato_footswitch",
        69: "hold_2",
        70: "sound_variation",
        71: "timbre",
        72: "release_time",
        73: "attack_time",
        74: "brightness",
        75: "decay_time",
        76: "vibrato_rate",
        77: "vibrato_depth",
        78: "vibrato_delay",
        79: "sound_controller_10",
        80: "general_purpose_5",
        81: "general_purpose_6",
        82: "general_purpose_7",
        83: "general_purpose_8",
        84: "portamento_control",
        91: "reverb_send",
        92: "tremolo_depth",
        93: "chorus_send",
        94: "detune_depth",
        95: "phaser_depth",
        96: "data_increment",
        97: "data_decrement",
        98: "nrpn_lsb",
        99: "nrpn_msb",
        100: "rpn_lsb",
        101: "rpn_msb",
        120: "all_sound_off",
        121: "reset_all_controllers",
        122: "local_control_on_off",
        123: "all_notes_off",
        124: "omni_mode_off",
        125: "omni_mode_on",
        126: "mono_mode_on",
        127: "poly_mode_on",
    }
)
for _i in range(32, 64):
    if _i not in CC_NAMES or CC_NAMES[_i].startswith("cc_"):
        CC_NAMES[_i] = CC_NAMES[_i - 32] + "_lsb"
CC_NAME_TO_NUMBER: Dict[str, int] = {}
for _num, _name in CC_NAMES.items():
    CC_NAME_TO_NUMBER.setdefault(_name, _num)
CC_NAME_TO_NUMBER.update(
    {
        "volume": 7,
        "pan": 10,
        "expression": 11,
        "sustain": 64,
        "sustain_pedal": 64,
        "hold": 64,
        "reverb": 91,
        "chorus": 93,
        "modulation": 1,
        "cutoff": 74,
        "resonance": 71,
        "all_notes_off": 123,
        "reset_all_controllers": 121,
    }
)

# ---------------------------------------------------------------------------
# General MIDI programs and drum kit
# ---------------------------------------------------------------------------

GM_PROGRAMS: List[str] = [
    "Acoustic Grand Piano", "Bright Acoustic Piano", "Electric Grand Piano", "Honky-tonk Piano",
    "Electric Piano 1", "Electric Piano 2", "Harpsichord", "Clavi",
    "Celesta", "Glockenspiel", "Music Box", "Vibraphone",
    "Marimba", "Xylophone", "Tubular Bells", "Dulcimer",
    "Drawbar Organ", "Percussive Organ", "Rock Organ", "Church Organ",
    "Reed Organ", "Accordion", "Harmonica", "Tango Accordion",
    "Acoustic Guitar (nylon)", "Acoustic Guitar (steel)", "Electric Guitar (jazz)", "Electric Guitar (clean)",
    "Electric Guitar (muted)", "Overdriven Guitar", "Distortion Guitar", "Guitar Harmonics",
    "Acoustic Bass", "Electric Bass (finger)", "Electric Bass (pick)", "Fretless Bass",
    "Slap Bass 1", "Slap Bass 2", "Synth Bass 1", "Synth Bass 2",
    "Violin", "Viola", "Cello", "Contrabass",
    "Tremolo Strings", "Pizzicato Strings", "Orchestral Harp", "Timpani",
    "String Ensemble 1", "String Ensemble 2", "SynthStrings 1", "SynthStrings 2",
    "Choir Aahs", "Voice Oohs", "Synth Voice", "Orchestra Hit",
    "Trumpet", "Trombone", "Tuba", "Muted Trumpet",
    "French Horn", "Brass Section", "SynthBrass 1", "SynthBrass 2",
    "Soprano Sax", "Alto Sax", "Tenor Sax", "Baritone Sax",
    "Oboe", "English Horn", "Bassoon", "Clarinet",
    "Piccolo", "Flute", "Recorder", "Pan Flute",
    "Blown Bottle", "Shakuhachi", "Whistle", "Ocarina",
    "Lead 1 (square)", "Lead 2 (sawtooth)", "Lead 3 (calliope)", "Lead 4 (chiff)",
    "Lead 5 (charang)", "Lead 6 (voice)", "Lead 7 (fifths)", "Lead 8 (bass + lead)",
    "Pad 1 (new age)", "Pad 2 (warm)", "Pad 3 (polysynth)", "Pad 4 (choir)",
    "Pad 5 (bowed)", "Pad 6 (metallic)", "Pad 7 (halo)", "Pad 8 (sweep)",
    "FX 1 (rain)", "FX 2 (soundtrack)", "FX 3 (crystal)", "FX 4 (atmosphere)",
    "FX 5 (brightness)", "FX 6 (goblins)", "FX 7 (echoes)", "FX 8 (sci-fi)",
    "Sitar", "Banjo", "Shamisen", "Koto",
    "Kalimba", "Bag pipe", "Fiddle", "Shanai",
    "Tinkle Bell", "Agogo", "Steel Drums", "Woodblock",
    "Taiko Drum", "Melodic Tom", "Synth Drum", "Reverse Cymbal",
    "Guitar Fret Noise", "Breath Noise", "Seashore", "Bird Tweet",
    "Telephone Ring", "Helicopter", "Applause", "Gunshot",
]
GM_PROGRAM_ALIASES: Dict[str, int] = {}
for _i, _name in enumerate(GM_PROGRAMS):
    GM_PROGRAM_ALIASES[_name.lower()] = _i
GM_PROGRAM_ALIASES.update(
    {
        "piano": 0,
        "acoustic_grand": 0,
        "epiano": 4,
        "organ": 16,
        "guitar": 24,
        "nylon_guitar": 24,
        "steel_guitar": 25,
        "bass": 33,
        "strings": 48,
        "choir": 52,
        "trumpet": 56,
        "sax": 65,
        "flute": 73,
        "square_lead": 80,
        "saw_lead": 81,
        "warm_pad": 89,
        "sweep_pad": 95,
        "sitar": 104,
        "drums": None,
    }
)
GM_PROGRAM_ALIASES.pop("drums", None)

GM_DRUM_NOTES: Dict[int, str] = {
    35: "Acoustic Bass Drum", 36: "Bass Drum 1", 37: "Side Stick", 38: "Acoustic Snare",
    39: "Hand Clap", 40: "Electric Snare", 41: "Low Floor Tom", 42: "Closed Hi-Hat",
    43: "High Floor Tom", 44: "Pedal Hi-Hat", 45: "Low Tom", 46: "Open Hi-Hat",
    47: "Low-Mid Tom", 48: "Hi-Mid Tom", 49: "Crash Cymbal 1", 50: "High Tom",
    51: "Ride Cymbal 1", 52: "Chinese Cymbal", 53: "Ride Bell", 54: "Tambourine",
    55: "Splash Cymbal", 56: "Cowbell", 57: "Crash Cymbal 2", 58: "Vibraslap",
    59: "Ride Cymbal 2", 60: "Hi Bongo", 61: "Low Bongo", 62: "Mute Hi Conga",
    63: "Open Hi Conga", 64: "Low Conga", 65: "High Timbale", 66: "Low Timbale",
    67: "High Agogo", 68: "Low Agogo", 69: "Cabasa", 70: "Maracas",
    71: "Short Whistle", 72: "Long Whistle", 73: "Short Guiro", 74: "Long Guiro",
    75: "Claves", 76: "Hi Wood Block", 77: "Low Wood Block", 78: "Mute Cuica",
    79: "Open Cuica", 80: "Mute Triangle", 81: "Open Triangle",
}

# ---------------------------------------------------------------------------
# Key signatures and note names
# ---------------------------------------------------------------------------

#: sf value -> (major key, relative minor key)
KEY_SIGNATURES: Dict[int, Tuple[str, str]] = {
    -7: ("Cb", "Abm"), -6: ("Gb", "Ebm"), -5: ("Db", "Bbm"), -4: ("Ab", "Fm"),
    -3: ("Eb", "Cm"), -2: ("Bb", "Gm"), -1: ("F", "Dm"), 0: ("C", "Am"),
    1: ("G", "Em"), 2: ("D", "Bm"), 3: ("A", "F#m"), 4: ("E", "C#m"),
    5: ("B", "G#m"), 6: ("F#", "D#m"), 7: ("C#", "A#m"),
}
KEY_NAME_TO_SF: Dict[str, Tuple[int, int]] = {}
for _sf, (_maj, _min) in KEY_SIGNATURES.items():
    KEY_NAME_TO_SF[_maj.lower()] = (_sf, 0)
    KEY_NAME_TO_SF[_maj.lower() + "maj"] = (_sf, 0)
    KEY_NAME_TO_SF[_maj.lower() + " major"] = (_sf, 0)
    KEY_NAME_TO_SF[_min.lower()] = (_sf, 1)
    KEY_NAME_TO_SF[_min.lower() + "m"] = (_sf, 1)
    KEY_NAME_TO_SF[_min.lower() + "min"] = (_sf, 1)
    KEY_NAME_TO_SF[_min.lower() + " minor"] = (_sf, 1)
KEY_NAME_TO_SF.update({"c": (0, 0), "am": (0, 1), "a minor": (0, 1)})

NOTE_NAMES: List[str] = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
NOTE_NAME_TO_PC: Dict[str, int] = {
    "c": 0, "c#": 1, "db": 1, "d": 2, "d#": 3, "eb": 3, "e": 4, "fb": 4,
    "e#": 5, "f": 5, "f#": 6, "gb": 6, "g": 7, "g#": 8, "ab": 8, "a": 9,
    "a#": 10, "bb": 10, "b": 11, "cb": 11, "b#": 0,
}

_PITCH_RE = re.compile(r"^\s*([A-Ga-g])\s*([#b\u266f\u266d]?)\s*(-?\d+)\s*$")

#: Semitone offsets of the major scale, used by scale-aware transposition.
MAJOR_SCALE_STEPS = (0, 2, 4, 5, 7, 9, 11)
NATURAL_MINOR_SCALE_STEPS = (0, 2, 3, 5, 7, 8, 10)
HARMONIC_MINOR_SCALE_STEPS = (0, 2, 3, 5, 7, 8, 11)
MELODIC_MINOR_SCALE_STEPS = (0, 2, 3, 5, 7, 9, 11)
PENTATONIC_MAJOR_STEPS = (0, 2, 4, 7, 9)
PENTATONIC_MINOR_STEPS = (0, 3, 5, 7, 10)
BLUES_SCALE_STEPS = (0, 3, 5, 6, 7, 10)
DORIAN_STEPS = (0, 2, 3, 5, 7, 9, 10)
MIXOLYDIAN_STEPS = (0, 2, 4, 5, 7, 9, 10)
CHROMATIC_STEPS = tuple(range(12))

SCALES: Dict[str, Tuple[int, ...]] = {
    "chromatic": CHROMATIC_STEPS,
    "major": MAJOR_SCALE_STEPS,
    "ionian": MAJOR_SCALE_STEPS,
    "minor": NATURAL_MINOR_SCALE_STEPS,
    "natural_minor": NATURAL_MINOR_SCALE_STEPS,
    "aeolian": NATURAL_MINOR_SCALE_STEPS,
    "harmonic_minor": HARMONIC_MINOR_SCALE_STEPS,
    "melodic_minor": MELODIC_MINOR_SCALE_STEPS,
    "dorian": DORIAN_STEPS,
    "mixolydian": MIXOLYDIAN_STEPS,
    "pentatonic": PENTATONIC_MAJOR_STEPS,
    "pentatonic_major": PENTATONIC_MAJOR_STEPS,
    "pentatonic_minor": PENTATONIC_MINOR_STEPS,
    "blues": BLUES_SCALE_STEPS,
}

# ---------------------------------------------------------------------------
# SysEx manufacturer ids (for readable dumps)
# ---------------------------------------------------------------------------

MANUFACTURER_IDS: Dict[int, str] = {
    0x01: "Sequential Circuits", 0x02: "IDP", 0x03: "Voyetra/Octave-Plateau",
    0x04: "Moog", 0x05: "Passport Designs", 0x06: "Lexicon", 0x07: "Kurzweil",
    0x08: "Fender", 0x09: "MIDI9", 0x0A: "AKG", 0x0B: "Voyce Music",
    0x0C: "WaveFrame", 0x0D: "ADA Signal Processors", 0x0E: "Garfield Electronics",
    0x0F: "Ensoniq", 0x10: "Oberheim", 0x11: "Apple", 0x12: "Grey Matter Response",
    0x13: "Digidesign", 0x14: "Palmtree Instruments", 0x15: "JLCooper",
    0x16: "Lowrey", 0x17: "Adams-Smith", 0x18: "E-mu", 0x19: "Harmony Systems",
    0x1A: "ART", 0x1B: "Baldwin", 0x1C: "Eventide", 0x1D: "Inventronics",
    0x1E: "Key Concepts", 0x1F: "Clarity", 0x20: "Passac", 0x21: "SIEL",
    0x22: "Synthaxe", 0x24: "Hohner", 0x25: "Twister", 0x26: "Solton",
    0x27: "Jellinghaus MS", 0x28: "Southworth Music Systems", 0x29: "PPG",
    0x2A: "JEN", 0x2B: "Solid State Logic", 0x2C: "Audio Veritrieb",
    0x2F: "Elka", 0x30: "Dynacord", 0x31: "Viscount", 0x33: "Clavia (Nord)",
    0x36: "Cheetah", 0x39: "Soundcraft", 0x3E: "Waldorf", 0x40: "Kawai",
    0x41: "Roland", 0x42: "Korg", 0x43: "Yamaha", 0x44: "Casio",
    0x45: "Akai", 0x47: "DOD", 0x4C: "Sony", 0x4E: "Matsushita",
    0x52: "Zoom", 0x54: "Matsushita Electric", 0x55: "Suzuki",
    0x56: "Fuji Sound", 0x57: "Acoustic Technical Laboratory",
    0x5A: "Seekers", 0x5C: "SDK", 0x5E: "Yamaha (XG)",
    0x60: "Korg (Kanon)", 0x62: "Waldorf (microQ)", 0x64: "Crystalake",
    0x7D: "Non-commercial / educational", 0x7E: "Universal Non-Real Time",
    0x7F: "Universal Real Time",
}

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def note_name(pitch: int, with_octave: bool = True) -> str:
    """60 -> 'C4' (middle C = C4 = MIDI 60)."""
    pitch = int(pitch)
    if not with_octave:
        return NOTE_NAMES[pitch % 12]
    octave = pitch // 12 - 1
    return "%s%d" % (NOTE_NAMES[pitch % 12], octave)


def note_number(value) -> int:
    """Parse 60 / 'C4' / 'c#4' / 'Db4' / 'C' into a MIDI note number.

    A name without an octave (C, F#, Bb) yields the pitch class 0-11, which is
    what scale roots and key centres want.
    """
    if isinstance(value, bool):
        raise SchemaError("bad pitch: %r" % (value,))
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(round(value))
    if not isinstance(value, str):
        raise SchemaError("bad pitch: %r" % (value,))
    text = value.strip().replace("\u266f", "#").replace("\u266d", "b")
    try:
        return int(text)
    except ValueError:
        pass
    m = _PITCH_RE.match(text)
    if not m:
        bare = text.lower()
        if bare in NOTE_NAME_TO_PC:
            return NOTE_NAME_TO_PC[bare]
        raise SchemaError("bad pitch name: %r (expected e.g. C4, F#3, C, 60)" % (value,))
    letter, accidental, octave = m.group(1).lower(), m.group(2), int(m.group(3))
    pc = NOTE_NAME_TO_PC[letter + accidental] if accidental else NOTE_NAME_TO_PC[letter]
    return (octave + 1) * 12 + pc


def cc_name(number: int) -> str:
    return CC_NAMES.get(int(number) % 128, "cc_%d" % number)


def cc_number(value) -> int:
    if isinstance(value, int):
        return value
    key = str(value).strip().lower().replace(" ", "_")
    if key in CC_NAME_TO_NUMBER:
        return CC_NAME_TO_NUMBER[key]
    if key.startswith("cc") and key[2:].lstrip("_").isdigit():
        return int(key[2:].lstrip("_"))
    raise SchemaError("unknown control change name: %r" % (value,))


def program_name(program: int) -> str:
    return GM_PROGRAMS[int(program) % 128]


def program_number(value) -> int:
    if isinstance(value, bool):
        raise SchemaError("bad program: %r" % (value,))
    if isinstance(value, int):
        return value
    key = str(value).strip().lower()
    if key.isdigit():
        number = int(key)
        # a bare numeric string is read as a General MIDI program number (1-128)
        return number - 1 if number > 127 or number >= 1 else 0
    if key in GM_PROGRAM_ALIASES:
        return GM_PROGRAM_ALIASES[key]
    raise SchemaError("unknown GM program name: %r" % (value,))


def key_signature_names(sf: int, mi: int) -> str:
    major, minor = KEY_SIGNATURES.get(int(sf), ("?", "?"))
    return minor if mi else major


def scale_steps(scale) -> Tuple[int, ...]:
    if scale is None:
        return CHROMATIC_STEPS
    if isinstance(scale, (list, tuple)):
        return tuple(int(s) % 12 for s in scale)
    key = str(scale).strip().lower().replace(" ", "_").replace("-", "_")
    if key not in SCALES:
        raise SchemaError("unknown scale: %r (known: %s)" % (scale, ", ".join(sorted(SCALES))))
    return SCALES[key]
