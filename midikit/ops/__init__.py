"""Edit operation package.

Importing this package registers every built-in operation.
"""

from __future__ import annotations

from .registry import (  # noqa: F401
    OPS,
    GRID_NAMES,
    Op,
    OpContext,
    Param,
    apply_note_transform,
    clamp_pitch,
    clamp_velocity,
    coerce_param,
    delete_matched,
    describe_ops,
    diatonic_transpose,
    get_op,
    grid_to_ticks,
    insert_events_into,
    match_events,
    match_notes,
    register,
    validate_params,
)
from . import event_ops  # noqa: F401
from . import note_ops  # noqa: F401
from . import structure_ops  # noqa: F401

__all__ = [
    "OPS",
    "Op",
    "OpContext",
    "Param",
    "register",
    "get_op",
    "describe_ops",
    "validate_params",
    "apply_note_transform",
    "match_events",
    "match_notes",
    "grid_to_ticks",
    "diatonic_transpose",
]
