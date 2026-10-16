"""Edit plan execution: an ordered list of validated operations."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from .errors import MidiKitError, SchemaError
from .ops import OPS, OpContext, get_op, validate_params
from .smf import MidiFile


def normalize_plan(plan: Any) -> List[Dict[str, Any]]:
    """Accept {'ops': [...]}, a bare list, or a single op object."""
    if plan is None:
        return []
    if isinstance(plan, dict):
        if "ops" in plan:
            plan = plan["ops"]
        elif "op" in plan:
            plan = [plan]
        else:
            raise SchemaError("plan object needs an 'ops' array (or a single 'op' entry)")
    if not isinstance(plan, (list, tuple)):
        raise SchemaError("plan must be an array of operations")
    out: List[Dict[str, Any]] = []
    for index, item in enumerate(plan):
        if not isinstance(item, dict):
            raise SchemaError("operation #%d must be an object" % index)
        if "op" not in item:
            raise SchemaError("operation #%d needs an 'op' name" % index, item=item)
        entry = dict(item)
        nested = entry.pop("params", None)
        if nested is not None:
            if not isinstance(nested, dict):
                raise SchemaError("operation #%d 'params' must be an object" % index)
            merged = dict(nested)
            merged.update({k: v for k, v in entry.items() if k != "op"})
            entry = merged
            entry["op"] = item["op"]
        out.append(entry)
    return out


def describe_plan(plan: Any) -> List[Dict[str, Any]]:
    """Summarise a plan without executing it (op names and parameter keys)."""
    out = []
    for item in normalize_plan(plan):
        name = item.get("op")
        op = OPS.get(str(name))
        params = {k: v for k, v in item.items() if k != "op"}
        info: Dict[str, Any] = {
            "op": name,
            "known": op is not None,
            "params": params,
        }
        if op is not None:
            info["summary"] = op.summary
            allowed = set(p.name for p in op.params)
            for param in op.params:
                allowed.update(param.aliases)
            info["unknown_params"] = sorted(k for k in params if k not in allowed)
            info["missing_required"] = sorted(
                p.name for p in op.params if p.required and p.name not in params
            )
        out.append(info)
    return out


def apply_plan(
    midi: MidiFile,
    plan: Any,
    encoding: str = "utf-8",
    pairing: str = "lifo",
    stop_on_error: bool = True,
    strict: bool = False,
) -> Dict[str, Any]:
    """Run every operation in order against an in-memory MidiFile.

    The file is only mutated in memory; nothing is written here, so a failing
    plan can be discarded without touching the file on disk.
    """
    operations = normalize_plan(plan)
    ctx = OpContext(midi, encoding=encoding, pairing=pairing, strict=strict)
    reports: List[Dict[str, Any]] = []
    for index, item in enumerate(operations):
        name = str(item.get("op"))
        params = {k: v for k, v in item.items() if k != "op"}
        try:
            op = get_op(name)
            values = validate_params(op, params, ctx)
            report = op.func(ctx, values) or {}
            report = dict(report)
            report["op"] = name
            report["index"] = index
            reports.append(report)
            ctx.changes.append({"op": name, "index": index, "result": report})
        except MidiKitError as exc:
            failure = {
                "op": name,
                "index": index,
                "params": params,
                "error": exc.to_dict(),
            }
            reports.append(failure)
            result = {
                "ok": False,
                "applied": index,
                "total": len(operations),
                "ops": reports,
                "failed": failure,
            }
            if stop_on_error:
                return result
    return {"ok": True, "applied": len(operations), "total": len(operations), "ops": reports, "failed": None}
