"""Exception hierarchy for midikit.

Every error carries a stable machine-readable 'code' plus optional
'details' so that an AI agent can branch on it without parsing prose.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


class MidiKitError(Exception):
    """Base class for all midikit errors."""

    code = "E_MIDIKIT"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details: Dict[str, Any] = details

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            out["details"] = self.details
        return out


class UsageError(MidiKitError):
    """Bad arguments / missing input / unknown op."""

    code = "E_USAGE"


class ParseError(MidiKitError):
    """The byte stream is not a valid Standard MIDI File."""

    code = "E_PARSE"


class SchemaError(MidiKitError):
    """A JSON document / plan / op argument has the wrong shape."""

    code = "E_SCHEMA"


class OpError(MidiKitError):
    """An edit operation could not be applied."""

    code = "E_OP"


class NotFoundError(MidiKitError):
    """A selector matched nothing where something was required."""

    code = "E_NOT_FOUND"


class ValidationFailed(MidiKitError):
    """Strict validation rejected the file."""

    code = "E_VALIDATION"

    def __init__(self, message: str, issues: Optional[List[Dict[str, Any]]] = None, **details: Any) -> None:
        super().__init__(message, **details)
        self.issues = issues or []
        if self.issues:
            self.details.setdefault("issues", self.issues)


class WriteError(MidiKitError):
    """Filesystem problem while writing."""

    code = "E_WRITE"


#: Process exit codes (stable contract for agents).
EXIT_OK = 0
EXIT_USAGE = 1
EXIT_VALIDATION = 2
EXIT_OP = 3
EXIT_PARSE = 4
EXIT_WRITE = 5
EXIT_INTERNAL = 70

_CODE_TO_EXIT = {
    "E_USAGE": EXIT_USAGE,
    "E_SCHEMA": EXIT_USAGE,
    "E_NOT_FOUND": EXIT_OP,
    "E_OP": EXIT_OP,
    "E_VALIDATION": EXIT_VALIDATION,
    "E_PARSE": EXIT_PARSE,
    "E_WRITE": EXIT_WRITE,
}


def exit_code_for(exc: BaseException) -> int:
    code = getattr(exc, "code", None)
    if code in _CODE_TO_EXIT:
        return _CODE_TO_EXIT[code]
    if isinstance(exc, (OSError, IOError)):
        return EXIT_WRITE
    return EXIT_INTERNAL
