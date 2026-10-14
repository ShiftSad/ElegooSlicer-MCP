from __future__ import annotations
import asyncio
import os
import re
import datetime
import sys
import uuid
from pathlib import Path
from typing import Annotated, Literal
from mcp.server.mcpserver import MCPServer, Image, Context
from pydantic import Field
from mcp.types import ToolAnnotations
from .config import load_config
from .client import OrcaClient
from .errors import ApiError, Validation, NotFound, Conflict, ConfigError, UiTimeout
from .models import summarize_slice
from . import settings_schema
from . import placement
from .knowledge_index import load_knowledge, search_knowledge
from .physics_check import run_checks, CHECK_LAYERS
from .breakdown import build_breakdown, purge_summary
from .compare import compute_comparison
from .guard import allow_override_warnings
from . import notes as _notes
from . import outcomes as _outcomes
import hashlib
import time
from . import plate_describe as _plate
from .printer import target as _ptarget
from .printer import wait as _pwait
from .printer import history as _phistory
from .printer import match as _pmatch
from .printer.errors import PrinterError
from .printer.snapshot import take_snapshot
from .printer.status import _dur as _duration_text

try:
    import importlib.metadata as _md
    _VERSION = _md.version("orcaslicer-mcp")
except Exception:  # source checkout without an installed dist
    _VERSION = "0.0.0"

mcp = MCPServer("orcaslicer", version=_VERSION)


def _client() -> OrcaClient:
    try:
        return OrcaClient(load_config())
    except (RuntimeError, ValueError) as e:
        raise ConfigError(str(e))


# The fork's terminal slice states: done, error, and idle (= cancelled).
# Anything else ("slicing", or transitional states like "starting") means
# keep polling — returning early hands the caller a stale result (F1).
_TERMINAL_SLICE_STATES = frozenset({"done", "error", "idle"})


async def _wait_for_slice(c, timeout: int) -> dict:
    """Poll slice_status until it reaches a terminal state (done/error/idle) or timeout. Returns the final status dict."""
    deadline = asyncio.get_running_loop().time() + timeout
    s = await c.slice_status()
    while s.get("state") not in _TERMINAL_SLICE_STATES and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(1.0)
        s = await c.slice_status()
    return s


async def _start_slice(c) -> dict:
    """POST /slice with one retry: right after a slice completes, the fork's
    background process can transiently decline to restart (422 slice_not_started,
    surfaced by the F2 wedge detection). One short pause clears it."""
    try:
        return await c.slice()
    except Validation as e:
        if "slice_not_started" not in str(e):
            raise
        await asyncio.sleep(1.5)
        return await c.slice()


# What to do about the per-key reasons a refused config write (422 config_rejected) gives most
# often. Other reasons are the slicer's own words (a bad value, say) and speak for themselves.
_CONFIG_REASON_HINTS = {
    "unknown_key": ("unknown_key means there is no such setting: look the key up with "
                    "search_settings or find_config_keys."),
    "not_editable_in_current_config": ("not_editable_in_current_config means the setting exists "
                                       "but this write cannot change it (it is computed, or "
                                       "belongs to a plate)."),
    "readonly_key": ("readonly_key means the slicer does not let this setting be edited (its "
                     "window greys it out either); select a preset that has the value you need."),
    "blocked_by_remote_api_policy": ("OrcaSlicer itself refused these keys: Preferences > Remote "
                                     "API > 'Allow script, G-code and connection edits' is off. "
                                     "They stay a change for the user to make in the OrcaSlicer "
                                     "GUI."),
}


def _err(e: ApiError) -> dict:
    out = {"error": str(e)}
    if isinstance(e, Validation):
        out["errors"] = e.errors
        errors = e.errors if isinstance(e.errors, dict) else {}
        if errors:
            # A config write is all-or-nothing: one refused key means nothing was applied, which
            # a model reading only "errors" can miss and then trust the other keys took effect.
            out["message"] = ("Nothing was applied: the slicer refused "
                              f"{', '.join(sorted(errors))} (the reason for each is in errors). "
                              "Settings are written all-or-nothing, so fix or drop those keys "
                              "and send the batch again.")
            reasons = {v for v in errors.values() if isinstance(v, str)}
            hints = [h for reason, h in _CONFIG_REASON_HINTS.items() if reason in reasons]
            if hints:
                out["hint"] = " ".join(hints)
    return out


@mcp.tool()
async def get_status() -> dict:
    """Snapshot of the current OrcaSlicer session: app and project info, the active
    print/filament/printer presets with which of their keys are modified (dirty), whether the
    last slice is still valid, and whether a slice is running. Read-only.

    Call it first to orient before slicing or editing, to see which settings drift from their
    preset, or to check slice_result_valid before trusting earlier stats."""
    try:
        async with _client() as c:
            return await c.get_status()
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def get_config(keys: list[str] | None = None) -> dict:
    """Read merged config values (optionally filtered to `keys`)."""
    try:
        async with _client() as c:
            return {"config": await c.get_config(keys)}
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def set_config(
    changes: Annotated[dict, Field(description=(
        "Map of OrcaSlicer config key to new value, e.g. "
        "{'layer_height': 0.2, 'sparse_infill_density': '15%'}. Values must match each "
        "setting's type; percent settings take strings like '15%'. Discover valid keys "
        "with search_settings, find_config_keys, or describe_setting. Do NOT echo back a "
        "get_config value for a per-filament setting (hot_plate_temp, fan_min_speed, "
        "nozzle_temperature_initial_layer, ...): get_config reports those merged across "
        "every loaded filament ('70,100,70,70,35'), while this writes the one filament "
        "preset open in the Filament tab, so pass that preset's single value instead."))],
) -> dict:
    """Apply config changes to the active project as unsaved overrides, atomically: if any
    key is invalid the whole batch is rejected and nothing changes. Returns {applied, errors};
    a rejected batch returns {"error": "config_rejected", "errors": {key: reason}, "message"}
    (plus a "hint" for common reasons such as unknown_key or readonly_key).

    Overrides show as modified in get_status, are not written to any preset file, and revert
    if the preset is reselected; call save_preset to persist them. Each apply invalidates the
    last slice, so re-slice afterwards. It does not run the physics gate, so for temperature,
    speed, acceleration, or flow keys run check_profile_physics before trusting the result. To
    edit a stored preset rather than the live project, use edit_preset.

    On a multi-filament setup do filament edits before print edits: changing another slot's
    value means select_preset for that slot, which discards unsaved print overrides
    (docs/order-of-operations.md)."""
    try:
        async with _client() as c:
            return await c.put_config(changes)
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def slice() -> dict:
    """Start slicing the current plate in the background and return immediately, without waiting
    for the result. The reply is 'started' (a slice began), 'already_valid' (the plate is
    unchanged and the last result still holds), or a conflict if a slice is already running.

    Fire-and-forget: poll get_slice_status for progress and stats, or cancel_slice to stop it.
    Prefer slice_and_wait when you want the finished stats back in one call."""
    try:
        async with _client() as c:
            return await c.slice()
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def get_slice_status() -> dict:
    """State of the current or most recent slice: state (slicing, done, error, or idle), stats
    (print time and filament use when done), and any warnings or errors. Read-only.

    Poll this after slice to follow progress and read the result; 'idle' means no slice has run
    or it was cancelled. For only the pass/fail warnings use get_slice_warnings; for a
    per-feature time and filament breakdown use get_slice_breakdown."""
    try:
        async with _client() as c:
            return summarize_slice(await c.slice_status())
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def get_slice_warnings() -> dict:
    """Just the warnings/errors from the last (or current) slice, plus validity: the
    fast 'did anything go wrong' check and the way to confirm a fix cleared.

    NOTE: only as complete as the API exposes. On the current fork build this may report
    valid with an empty warnings list even when the GUI shows a plate-boundary toast -
    the fork must populate the plater warning list (tracked as the fork batch). Once it
    does, this reports the real warnings with no change here."""
    try:
        async with _client() as c:
            st = await c.slice_status()
            status = await c.get_status()
        s = summarize_slice(st)
        return {"state": s["state"], "valid": status.get("slice_result_valid"),
                "warnings": s["warnings"], "errors": st.get("errors", []),
                "message": s["message"]}
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def get_slice_breakdown() -> dict:
    """Per-feature breakdown of the last slice + a stateless predicted-vs-observed flow check.

    Returns per-role time/filament + speed/flow ranges, global time-weighted metric
    distributions, per-layer aggregates, and a prediction_check flagging where the profile's
    speed was silently throttled at the flow ceiling ('clamped'). Answers 'which feature is
    the time hog' directly instead of by trial slicing.

    On ElegooSlicer MCP ('breakdown_purge' in get_status capabilities) it also has `purge`:
    {filament_changes, filament_change_time_s, flush_g, flush_mm} for a multi-filament print
    (zeros for one filament), and `purge_summary`, a sentence to relay. That time and filament
    is part of the totals but of no role, so mention it when the roles don't add up.

    Degrades to {"available": false, "reason": ...} on fork builds that don't emit the
    breakdown, or when there is no valid slice."""
    try:
        async with _client() as c:
            status = await c.slice_status()
            cfg = await c.get_config(None)
        return build_breakdown(status, cfg)
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def cancel_slice() -> dict:
    """Abort a running slice, or unwedge a stale 'slicing' state (e.g. after an
    object outside the bed). Safe when idle."""
    try:
        async with _client() as c:
            return await c.cancel_slice()
    except ApiError as e:
        return _m4_err(e, "the F2 fork fix")


# Claude Desktop aborts a tool call after about 60 s, so slice_and_wait gives up waiting a
# little before that and reports the slice as still running, instead of being cut off with
# no answer at all.
_SLICE_WAIT_DEFAULT_S = 50


@mcp.tool()
async def slice_and_wait(
    timeout: Annotated[int, Field(description=(
        "Maximum seconds to wait for the slice to finish before returning the last known "
        "state. Default 50, so the call returns before Claude Desktop's ~60 s tool-call limit; "
        "raise it only for clients that allow longer calls."))] = _SLICE_WAIT_DEFAULT_S,
) -> dict:
    """Slice the current plate and wait up to `timeout` seconds (50 by default) for it to
    finish, then return the final stats and warnings in one call. If the plate is already
    sliced and unchanged, it returns the existing result without re-slicing.

    A big plate can take longer than that: the reply then has timed_out=true, the current state
    and progress, and a hint. Call slice_and_wait again to keep waiting (it waits for the slice
    already running instead of starting a new one), or poll get_slice_status.

    This is the usual way to slice when you want the outcome immediately. For a non-blocking
    start, use slice then poll get_slice_status; to sweep one setting across values, use
    compare_settings."""
    try:
        async with _client() as c:
            try:
                started = await _start_slice(c)
            except Conflict as e:
                # A slice is already running, typically the one an earlier slice_and_wait
                # started before its timeout: wait for that one rather than fail.
                if "already_slicing" not in str(e):
                    raise
                started = {}
            if started.get("already_valid"):
                return summarize_slice(await c.slice_status())
            result = summarize_slice(await _wait_for_slice(c, timeout))
            if result["state"] not in _TERMINAL_SLICE_STATES:
                result["timed_out"] = True
                result["hint"] = (f"Still slicing after {timeout} s. Call slice_and_wait again "
                                  "to keep waiting for this slice, or poll get_slice_status.")
            return result
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def apply_and_slice(
    changes: Annotated[dict, Field(description=(
        "Map of OrcaSlicer config key to new value to apply before slicing, e.g. "
        "{'layer_height': 0.2}. Same format and validation as set_config; discover keys with "
        "search_settings or find_config_keys."))],
) -> dict:
    """Apply config overrides and then slice in one step, returning {applied, errors, result}
    with the resulting stats and warnings. The changes are atomic (any invalid key rejects the
    whole batch) and unsaved, exactly like set_config, so they revert if the preset is reselected.

    Use this to test the effect of a tweak in a single call. Use set_config then slice_and_wait
    to keep the steps separate, or compare_settings to try several values of one key."""
    try:
        async with _client() as c:
            applied = await c.put_config(changes)
            started = await _start_slice(c)
            if not started.get("already_valid"):
                await _wait_for_slice(c, 300)
            result = summarize_slice(await c.slice_status())
            return {"applied": applied.get("applied", []),
                    "errors": applied.get("errors", {}), "result": result}
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def compare_settings(key: str, values: list, extra: dict | None = None) -> dict:
    """For each value of `key`, slice and collect stats/warnings; restore the original when done.

    Non-destructive: the original value of `key` is put back even on error.
    """
    try:
        async with _client() as c:
            snapshot_keys = [key] + list((extra or {}).keys())
            originals = await c.get_config(snapshot_keys)
            rows = []
            restore_error = None
            try:
                for v in values:
                    row = {"value": v, "stats": None, "warnings": [], "error": None}
                    try:
                        await c.put_config({key: v, **(extra or {})})
                        started = await _start_slice(c)
                        if not started.get("already_valid"):
                            await _wait_for_slice(c, 300)
                        s = summarize_slice(await c.slice_status())
                        row["stats"] = s["stats"]
                        row["warnings"] = s["warnings"]
                    except ApiError as e:
                        row["error"] = str(e)
                        if isinstance(e, Validation) and e.errors:
                            row["errors"] = e.errors  # config_rejected: which keys, and why
                    rows.append(row)
            finally:
                if originals:
                    try:
                        await c.restore_config(originals)
                    except ApiError as e:
                        restore_error = str(e)  # preserve collected rows even if restore fails
            result = {"key": key, "rows": rows}
            if restore_error is not None:
                result["restore_error"] = restore_error
            return result
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def compare_slices(variants: list[dict], baseline: str | None = None,
                         detail: bool = False, timeout: int = 300) -> dict:
    """Slice the current plate under several named variants and compare the cost of each.

    Each variant is {"name": str, "changes": {setting: value}}; changes={} means the
    current config as-is (a natural baseline row). Applies each variant over the ORIGINAL
    config (resetting between variants, so they don't stack), slices it, then restores your
    config exactly as it was - nothing is left changed (slice validity is left false, as
    after any un-resliced edit).

    Returns a ready-to-relay `headline` and `table_markdown`, plus structured `variants`.
    All deltas and percentages are ALREADY computed and rounded against `baseline`
    (defaults to the changes={} variant, else the first) - relay them as given rather than
    recomputing. `recommended` names one pick; `recommended_is_dominant` says whether it
    beats every variant on every axis (time, filament, warnings) or is only the fastest
    warning-free option amid a genuine trade-off (`tradeoff` then names the frontier).

    Each variant is a full slice (minutes); capped at 8. Set detail=True only when a
    per-feature (wall/infill/support) split is wanted - it grows the response ~N x. With
    more than ~5 variants, lead with the recommendation and the extremes, not all rows.
    """
    if not isinstance(variants, list) or len(variants) < 2:
        return {"error": "need_at_least_two_variants"}
    if len(variants) > 8:
        return {"error": "too_many_variants", "max": 8, "given": len(variants)}
    names = [v.get("name") for v in variants]
    if any(not n for n in names):
        return {"error": "each_variant_needs_a_name"}
    if len(set(names)) != len(names):
        return {"error": "variant_names_must_be_unique"}
    if baseline is not None and baseline not in names:
        return {"error": "unknown_baseline", "baseline": baseline, "names": names}

    try:
        async with _client() as c:
            union = sorted({k for v in variants for k in (v.get("changes") or {})})
            snapshot = await c.get_config(union) if union else {}
            results: list[dict] = []
            restore_error = None
            try:
                for v in variants:
                    changes = v.get("changes") or {}
                    r = {"name": v["name"], "changes": changes, "time_s": None,
                         "filament_g": None, "warnings": [], "valid": False,
                         "error": None, "roles": None}
                    try:
                        if snapshot:
                            await c.restore_config(snapshot)  # reset to baseline so variants don't stack
                        if changes:
                            applied = await c.put_config(changes)
                            if applied.get("errors"):
                                r["error"] = "invalid_keys"
                                r["errors"] = applied["errors"]
                                results.append(r)
                                continue
                        started = await _start_slice(c)
                        if not started.get("already_valid"):
                            await _wait_for_slice(c, timeout)
                        st = await c.slice_status()
                        s = summarize_slice(st)
                        stats = s.get("stats") or {}
                        status = await c.get_status()
                        r["time_s"] = stats.get("estimated_time_seconds")
                        r["filament_g"] = stats.get("filament_used_g")
                        r["warnings"] = s.get("warnings") or []
                        r["valid"] = bool(status.get("slice_result_valid"))
                        if s.get("state") == "error" or r["time_s"] is None:
                            r["error"] = r["error"] or (s.get("message") or "slice_failed")
                        if detail:
                            r["roles"] = build_breakdown(st, await c.get_config(None)).get("roles")
                    except ApiError as e:
                        r["error"] = str(e)
                        if isinstance(e, Validation) and e.errors:
                            r["errors"] = e.errors  # config_rejected: which keys, and why
                    results.append(r)
            finally:
                if snapshot:
                    try:
                        await c.restore_config(snapshot)
                    except ApiError as e:
                        restore_error = str(e)
    except ApiError as e:
        return _err(e)

    out = compute_comparison(results, baseline, detail)
    out["restored"] = restore_error is None
    if restore_error is not None:
        out["restore_error"] = restore_error
    return out


def _m4_err(e: ApiError, milestone: str) -> dict:
    # Only a route-level 404 means the build lacks the capability; a resource-level
    # 404 (unknown_object / unknown_preset / missing file) must keep its message (F7).
    if isinstance(e, NotFound) and e.route_missing:
        return {"error": f"not available on this OrcaSlicer build (needs {milestone})"}
    return _err(e)


def _m4a_err(e: ApiError) -> dict:
    return _m4_err(e, "M4a")


def _m4b_err(e: ApiError) -> dict:
    return _m4_err(e, "M4b")


def _m4c_err(e: ApiError) -> dict:
    return _m4_err(e, "M4c")


def _plates_err(e: ApiError) -> dict:
    return _m4_err(e, "fork v2.4.2-mcp.13 or later")


def _elegoo_build(capability: str) -> str:
    # ElegooSlicer MCP advertises each feature in get_status "capabilities", so name the
    # capability rather than a version number.
    return f"an ElegooSlicer MCP build that lists '{capability}' in get_status capabilities"


def _elegoo_err(e: ApiError, capability: str) -> dict:
    out = _m4_err(e, _elegoo_build(capability))
    # ElegooSlicer MCP error replies carry details next to "error" (used_by on slot_in_use,
    # detail on bad_params, path on file_exists, ...). They say what to fix, so pass them on.
    for key, value in e.body.items():
        if key not in out and key != "applied":
            out[key] = value
    return out


async def _missing_capability(c, capability: str) -> dict | None:
    """The error to return when the slicer does not advertise `capability`, else None.

    Needed where an older build would not answer 404 but silently do something else: an
    older POST /orient ignores the body and turns every object."""
    caps = (await c.get_status()).get("capabilities") or []
    if capability in caps:
        return None
    return {"error": f"not available on this OrcaSlicer build (needs {_elegoo_build(capability)})"}


@mcp.tool()
async def list_objects() -> dict:
    """List objects in the project: id (stable), name, size_mm, transform (offset/rotation/scale)
    and, on fork v2.4.2-mcp.13 and later, the 0-based plate each one sits on (-1 when it is off
    every plate)."""
    try:
        async with _client() as c:
            return await c.get_objects()
    except ApiError as e:
        return _m4b_err(e)


@mcp.tool()
async def set_object_config(object_id: int, changes: dict) -> dict:
    """Set per-object config overrides on an object by id, e.g. {"wall_loops": 4, "sparse_infill_density": "30%"}. Atomic (nothing applied if any key is invalid: the reply is then config_rejected with the reason per key in errors)."""
    try:
        async with _client() as c:
            return await c.set_object_config(object_id, changes)
    except ApiError as e:
        return _m4c_err(e)


@mcp.tool()
async def duplicate_object(object_id: int) -> dict:
    """Duplicate an object on the plate by id (adds a copy, offset from the original)."""
    try:
        async with _client() as c:
            return await c.duplicate_object(object_id)
    except ApiError as e:
        return _m4b_err(e)


@mcp.tool()
async def delete_object(
    object_id: Annotated[int, Field(description=(
        "Integer id of the object to remove, taken from the 'id' field of list_objects "
        "(not the array index or the file name)."))],
) -> dict:
    """Remove one object from the current plate by id. This is permanent within the session
    and cannot be undone through the API; the other objects keep their ids.

    Call list_objects first to get the id. Deleting leaves the last slice invalid, so re-slice
    afterwards. To drop just one copy made with duplicate_object, pass that copy's id."""
    try:
        async with _client() as c:
            return await c.delete_object(object_id)
    except ApiError as e:
        return _m4b_err(e)


@mcp.tool()
async def transform_object(object_id: int, translate: list[float] | None = None,
                           rotate: list[float] | None = None,
                           scale: list[float] | None = None) -> dict:
    """Move/rotate/scale an object by id. translate=[dx,dy,dz] mm (relative), rotate=[rx,ry,rz] degrees (relative), scale=[sx,sy,sz] absolute factor. Provide at least one.

    On fork v2.4.2-mcp.13 and later the object then belongs to whichever plate it sits on,
    as after a drag in the window; list_objects shows the plate. To send an object to
    another plate, move_object_to_plate is simpler than working out coordinates.

    On ElegooSlicer MCP (mcp_build >= 1 in get_status) rotate turns the object from where
    it is about the world axes, X then Y then Z, like the rotate gizmo: rotate=[0,-90,0]
    followed by rotate=[0,90,0] returns to the start. After a rotate or scale the part is
    dropped back onto the bed (also after a move that left it floating), as the window
    does, so the returned offset Z can change."""
    try:
        async with _client() as c:
            return await c.transform_object(object_id, translate, rotate, scale)
    except ApiError as e:
        return _m4b_err(e)


@mcp.tool()
async def arrange_plate() -> dict:
    """Auto-arrange all objects on the plate (async job; poll get_job_status until idle).

    With several plates this is OrcaSlicer's "Arrange all": it re-packs the objects of every
    unlocked plate together and deletes plates left empty at the end, which undoes a
    one-part-per-plate layout. To shift one object, use transform_object instead."""
    try:
        async with _client() as c:
            return await c.arrange()
    except ApiError as e:
        return _m4b_err(e)


@mcp.tool()
async def auto_orient(
    object_id: Annotated[int | None, Field(description=(
        "Integer id of the one object to orient, from the 'id' field of list_objects. "
        "Leave it out to orient every object."))] = None,
) -> dict:
    """Auto-orient objects for printing (async job; poll get_job_status until idle). With
    object_id only that object is turned, as selecting it and pressing Auto orient does in
    the window; without it, every object.

    Orienting one object needs ElegooSlicer MCP ('orient_object' in get_status capabilities).
    On an older build the call is refused before anything moves, because that build would
    ignore object_id and orient every object. An unknown id returns unknown_object. Orienting
    leaves the object's plate unsliced, so re-slice afterwards."""
    try:
        async with _client() as c:
            if object_id is not None:
                missing = await _missing_capability(c, "orient_object")
                if missing:
                    return missing
            return await c.orient(object_id)
    except ApiError as e:
        return _m4b_err(e)


@mcp.tool()
async def set_view(
    view: Annotated[Literal["prepare", "preview"], Field(description=(
        "'prepare' is the 3D editor where objects are placed and edited; 'preview' shows "
        "the sliced toolpaths of the current plate."))],
) -> dict:
    """Switch the slicer window to the Prepare tab (3D editor) or the Preview tab (sliced
    toolpaths), like clicking the tab at the top. Returns {"view": ...}.

    Use it to show the user the result: preview after a slice, prepare before editing objects.
    As with a click on the tab, opening the preview starts slicing the current plate when it
    holds objects and is not sliced yet; follow it with get_slice_status or slice_and_wait.
    get_status reports the open tab as "view" (prepare, preview or other). Needs ElegooSlicer
    MCP ('view' in get_status capabilities)."""
    try:
        async with _client() as c:
            return await c.set_view(view)
    except ApiError as e:
        return _elegoo_err(e, "view")


# --- Parts of an object (ElegooSlicer MCP, capability "volumes") ---------------
# An object is made of volumes: its model part(s) plus any modifiers, negative parts and
# support enforcers/blockers, the entries under the object in the window's object list.
# The tools call them parts, as the window does; part_id is the volume's numeric id.


def _volumes_err(e: ApiError) -> dict:
    # A bare 404 "not_found" means the route is missing (an older build). The slicer must
    # answer an unknown id with 404 unknown_object / unknown_volume, which pass through.
    return _elegoo_err(e, "volumes")


_PART_ID = Field(description="Numeric id of the part, from the 'id' field of list_parts "
                             "(not its index).")
_OBJECT_ID = Field(description="Integer id of the object, from the 'id' field of list_objects.")


@mcp.tool()
async def list_parts(object_id: Annotated[int, _OBJECT_ID]) -> dict:
    """List the parts of one object: its model part(s) plus any modifiers, negative parts and
    support enforcers or blockers, as the object list in the window shows them. Read-only.

    Each part has id (the part_id the other part tools take), index, name, type (part,
    modifier, negative, support_enforcer or support_blocker), extruder (its filament slot,
    0 when it uses the object's own), size_mm, position_mm (its centre, in the same world
    millimetres as list_objects) and config (its own setting overrides). An unknown object id
    returns unknown_object. Needs ElegooSlicer MCP ('volumes' in get_status capabilities)."""
    try:
        async with _client() as c:
            return await c.get_volumes(object_id)
    except ApiError as e:
        return _volumes_err(e)


@mcp.tool()
async def add_part(
    object_id: Annotated[int, _OBJECT_ID],
    type: Annotated[Literal["modifier", "negative", "support_enforcer", "support_blocker"], Field(
        description=(
            "'modifier': settings given in config apply inside its volume (denser infill in one "
            "spot, say). 'negative': its volume is cut out of the object. 'support_enforcer': "
            "support is generated inside it. 'support_blocker': no support inside it."))],
    shape: Annotated[Literal["box", "cylinder", "sphere"], Field(description=(
        "Shape of the new part, as in the window's right-click menu."))],
    size_mm: Annotated[list[float], Field(description=(
        "[x, y, z] size in mm of the shape's bounding box; each must be above 0."))],
    position_mm: Annotated[list[float], Field(description=(
        "[x, y, z] in mm where the shape's CENTRE goes, in the same world coordinates as "
        "list_objects (the bed surface is z = 0). To cover the bottom 5 mm of a part standing "
        "on the bed, use z = 2.5 with a z size of 5."))],
    name: Annotated[str | None, Field(description=(
        "Name shown in the object list. Leave it out for the window's default name."))] = None,
    config: Annotated[dict | None, Field(description=(
        "Setting overrides for the new part, e.g. {'sparse_infill_density': '50%'} on a "
        "modifier. Same keys and rules as set_part_config."))] = None,
) -> dict:
    """Add a modifier, negative part, support enforcer or support blocker to an object, made
    the way the window's right-click menu makes one (Add modifier, Add negative part, Support
    enforcer or Support blocker, then Box, Cylinder or Sphere), then sized and placed.
    Returns {"volume": {...}} with the new part's id and the fields list_parts shows.

    Errors: bad_type, bad_shape or bad_size for a value it cannot use, unknown_object for an
    id list_objects doesn't show. Sensitive keys in config (custom G-code, post-processing,
    printer connection) are refused, as in set_object_config. The object's plate is left
    unsliced, so re-slice afterwards; render_plate shows where the part landed. Needs
    ElegooSlicer MCP ('volumes' in get_status capabilities)."""
    try:
        async with _client() as c:
            return await c.add_volume(object_id, type, shape, size_mm, position_mm, name, config)
    except ApiError as e:
        return _volumes_err(e)


@mcp.tool()
async def remove_part(
    object_id: Annotated[int, _OBJECT_ID],
    part_id: Annotated[int, _PART_ID],
) -> dict:
    """Remove one part from an object: a modifier, negative part, support enforcer or blocker,
    or one of several model parts. Cannot be undone through the API; returns {"ok": true}.

    An object's last model part cannot be removed (last_part); use delete_object to remove
    the whole object. An unknown id returns unknown_object or unknown_volume. The object's plate is left unsliced, so re-slice afterwards. Needs
    ElegooSlicer MCP ('volumes' in get_status capabilities)."""
    try:
        async with _client() as c:
            return await c.delete_volume(object_id, part_id)
    except ApiError as e:
        return _volumes_err(e)


@mcp.tool()
async def set_part_config(
    object_id: Annotated[int, _OBJECT_ID],
    part_id: Annotated[int, _PART_ID],
    changes: Annotated[dict, Field(description=(
        "Map of config key to new value for this part, e.g. {'sparse_infill_density': '50%', "
        "'wall_loops': 4}."))],
) -> dict:
    """Set config overrides on one part of an object, e.g. what a modifier changes inside its
    volume. Atomic like set_object_config: if any key is invalid nothing is applied.

    A part's override beats its object's, which beats the print preset. Keys that run code or
    drive the printer (custom G-code, post-processing, printer connection) are refused, as for
    set_object_config. To give a part another filament, set_part_filament is the shortcut. The
    object's plate is left unsliced, so re-slice afterwards. An unknown id returns
    unknown_object or unknown_volume. Needs ElegooSlicer MCP ('volumes'
    in get_status capabilities)."""
    try:
        async with _client() as c:
            return await c.set_volume_config(object_id, part_id, changes)
    except ApiError as e:
        return _volumes_err(e)


@mcp.tool()
async def set_part_filament(
    object_id: Annotated[int, _OBJECT_ID],
    part_id: Annotated[int, _PART_ID],
    slot: Annotated[int, Field(description=(
        "1-based filament slot, as numbered in the window's filament list. 0 makes the part "
        "use its object's filament again."))],
) -> dict:
    """Print one part with another filament, like picking it in the object list's filament
    column. Shortcut for set_part_config with {"extruder": slot}.

    Use it for a multi-colour part made of several model parts, or to give a modifier its own
    filament. list_parts shows each part's current slot (0 = the object's own). Needs
    ElegooSlicer MCP ('volumes' in get_status capabilities)."""
    try:
        async with _client() as c:
            return await c.set_volume_config(object_id, part_id, {"extruder": slot})
    except ApiError as e:
        return _volumes_err(e)


@mcp.tool()
async def paint_region(
    object_id: Annotated[int, _OBJECT_ID],
    layer: Annotated[Literal["support", "seam", "color"], Field(description=(
        "Which paint: 'support' (support enforcer/blocker painting), 'seam' (where the seam "
        "may go) or 'color' (which filament prints each face, for multi-material)."))],
    state: Annotated[Literal["enforce", "block", "none"] | None, Field(description=(
        "For support and seam. 'enforce': support here, or put the seam here. 'block': no "
        "support here, or keep the seam away. 'none': erase that paint."))] = None,
    slot: Annotated[int | None, Field(description=(
        "For color: the 1-based filament slot to paint with, as numbered in the window."))] = None,
    box_min: Annotated[list[float] | None, Field(description=(
        "[x, y, z] in mm, the low corner of the box to paint, in the same world coordinates "
        "as list_objects (the bed surface is z = 0). Give box_max with it; leave both out to "
        "paint the whole object."))] = None,
    box_max: Annotated[list[float] | None, Field(description=(
        "[x, y, z] in mm, the high corner of the box to paint."))] = None,
    max_overhang_deg: Annotated[float | None, Field(description=(
        "0 to 90. Only paint faces that point down: a face is painted when its world-space "
        "unit normal n has n.z < 0 and -n.z >= sin(max_overhang_deg), i.e. the normal points "
        "strictly downward and at least this many degrees below horizontal. 90 = only flat "
        "undersides; 45 = overhangs of 45 degrees or more from vertical, the usual rule for "
        "needing support; 0 = every face that points down at all (vertical walls, whose "
        "normal is horizontal, are never included). Leave it out to paint every face in the "
        "box."))] = None,
    clear: Annotated[bool, Field(description=(
        "True: erase this layer's paint on the whole object. Takes no box; to erase only "
        "inside a box, use state='none' with box_min/box_max."))] = False,
) -> dict:
    """Paint support, seam or color onto the faces of one object inside a box, as the paint
    tools in the window do with a brush. A face is painted when its centre lies in the box
    (and, with max_overhang_deg, when it faces down steeply enough). Returns
    {"painted_triangles": n, "layer": ...}.

    Use it to force support under one overhang, block support in a spot that doesn't need it,
    pin the seam to the back of a part, or paint a region in another filament. Find the box
    from list_objects (bounding box) or list_parts and check the result with render_plate.
    painted_triangles 0 means no face matched: widen the box or the angle. Painted support
    is used when support is on (enable_support); with a '(manual)' support_type only painted
    areas get support. The paint is saved with the project, and the object's plate is left
    unsliced, so re-slice. Errors: bad_layer, bad_state, bad_slot or bad_overhang (422), unknown_object.
    Needs ElegooSlicer MCP ('paint' in get_status capabilities)."""
    if (box_min is None) != (box_max is None):
        return {"error": "box_needs_both_corners",
                "hint": "Pass box_min and box_max together, or neither to paint the whole object."}
    # The spec defines clear only for the whole object. Refuse it with a box rather than let
    # the slicer guess: a model that wanted to erase one spot must not wipe every brush
    # stroke on that layer.
    if clear and box_min is not None:
        return {"error": "clear_takes_no_box",
                "hint": "clear=True erases the whole object's paint on this layer. To erase "
                        "only inside the box, pass state='none' with box_min/box_max instead."}
    # An angle below horizontal only means something from 0 to 90 degrees.
    if max_overhang_deg is not None and not 0 <= max_overhang_deg <= 90:
        return {"error": "bad_overhang",
                "hint": "max_overhang_deg is degrees below horizontal, from 0 to 90 "
                        "(45 = the usual overhangs that need support)."}
    body: dict = {"layer": layer}
    if state is not None:
        body["state"] = state
    if slot is not None:
        body["slot"] = slot
    if box_min is not None:
        body["box_mm"] = [box_min, box_max]
    if max_overhang_deg is not None:
        body["max_overhang_deg"] = max_overhang_deg
    if clear:
        body["clear"] = True
    try:
        async with _client() as c:
            return await c.paint(object_id, body)
    except ApiError as e:
        return _elegoo_err(e, "paint")


@mcp.tool()
async def split_object(
    object_id: Annotated[int, _OBJECT_ID],
    to: Annotated[Literal["objects", "parts"], Field(description=(
        "'objects': each separate piece of the mesh becomes an object of its own, as the "
        "window's Split to objects does. 'parts': the pieces stay one object as separate parts "
        "(Split to parts), each of which can then get its own filament."))],
) -> dict:
    """Split an object whose mesh is made of separate pieces (a model exported with loose
    shells, say), as the window's Split to objects and Split to parts buttons do, without their
    dialogs. Returns {"ids": [...]}: the new objects' ids for 'objects', or the object's part
    ids (list_parts) for 'parts'.

    Splitting to parts is the usual first step for a multi-colour model: then give each part
    its slot with set_part_filament. After splitting to objects, use the returned ids (see
    list_objects): the split object is replaced by its pieces. A one-piece object cannot be
    split (not_splittable, 422); an unknown id returns unknown_object. The plate is left
    unsliced, so re-slice afterwards. Needs ElegooSlicer MCP ('split' in get_status
    capabilities)."""
    try:
        async with _client() as c:
            return await c.split_object(object_id, to)
    except ApiError as e:
        return _elegoo_err(e, "split")


# --- Filament slots (ElegooSlicer MCP, capability "filaments") -----------------
# The project's filament list in the window's sidebar: slots 1, 2, ... each holding a filament
# preset and a colour. Objects, parts and painted regions print with a slot number.

_SLOT = Field(description="1-based filament slot, as the window's filament list numbers it "
                          "(see list_filament_slots).")
_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


@mcp.tool()
async def list_filament_slots() -> dict:
    """List the project's filament slots as the sidebar's filament list shows them. Returns
    {"count", "max", "slots": [{slot, preset, type, color, used_on_current_plate}]}: how many
    slots there are, the most this printer allows, and per slot its number (1-based), filament
    preset, filament type (PLA, PETG, ...), colour ('#rrggbb') and whether something on the
    current plate prints with it. Read-only.

    Call it to pick a slot for set_part_filament, paint_region(layer='color') or
    add_layer_change, and before set_filament_count to see which slots are in use. Needs
    ElegooSlicer MCP ('filaments' in get_status capabilities)."""
    try:
        async with _client() as c:
            return await c.get_filaments()
    except ApiError as e:
        return _elegoo_err(e, "filaments")


@mcp.tool()
async def set_filament_slot(
    slot: Annotated[int, _SLOT],
    preset: Annotated[str | None, Field(description=(
        "Exact name of the filament preset to load into the slot, as "
        "list_presets(type='filament', include_system=True) shows it. It must suit the selected "
        "printer (incompatible_preset otherwise)."))] = None,
    color: Annotated[str | None, Field(description=(
        "The slot's colour as '#rrggbb', e.g. '#1e90ff'. It is the colour the preview shows and "
        "the printer's filament mapping compares; it does not change the preset."))] = None,
) -> dict:
    """Change one filament slot: load another filament preset into it, as picking one in the
    sidebar's filament list does, and/or set its colour. Returns the slot as
    list_filament_slots shows it.

    Give preset, color or both. Every plate is left unsliced, so re-slice afterwards. When the
    preference to auto-calculate flushing volumes is on, the slicer recalculates the flushing
    between slots, as the window does. Errors: unknown_slot (404); unknown_preset,
    incompatible_preset or bad_color (422). Needs ElegooSlicer MCP ('filaments' in get_status
    capabilities)."""
    if preset is None and color is None:
        return {"error": "nothing_to_change", "hint": "Pass preset, color or both."}
    if color is not None and not _HEX_COLOR.match(color):
        return {"error": "bad_color", "hint": "Pass the colour as '#rrggbb', e.g. '#1e90ff'."}
    try:
        async with _client() as c:
            return await c.set_filament_slot(slot, preset, color)
    except ApiError as e:
        return _elegoo_err(e, "filaments")


@mcp.tool()
async def set_filament_count(
    count: Annotated[int, Field(description=(
        "How many filament slots the project should have: from 1 up to the max that "
        "list_filament_slots reports."))],
    force: Annotated[bool, Field(description=(
        "Remove slots even when objects or parts print with them. Those then fall back as in "
        "the window: an object to slot 1, a part to its object's slot. Leave it false to get "
        "slot_in_use and the list of what uses them instead."))] = False,
) -> dict:
    """Set how many filament slots the project has, as the plus and minus buttons of the
    sidebar's filament list do: new slots are added at the end, and removing takes slots off the
    end (the highest numbers). Returns the slots as list_filament_slots does.

    Removing a slot that an object or part uses is refused with slot_in_use (409) and used_by,
    the objects and parts that use it, unless force=True. A count outside 1..max is bad_count
    (422). New slots start with the slicer's default filament; set them with set_filament_slot.
    Needs ElegooSlicer MCP ('filaments' in get_status capabilities)."""
    try:
        async with _client() as c:
            return await c.set_filament_count(count, force)
    except ApiError as e:
        return _elegoo_err(e, "filaments")


# --- Undo / redo (ElegooSlicer MCP, capability "undo") --------------------------


@mcp.tool()
async def undo() -> dict:
    """Undo the last change in the slicer window's history, like Ctrl+Z. Returns {"ok",
    "can_undo", "can_redo"}; ok is false when there was nothing to undo.

    The history is the window's own, shared with the user: it holds their edits as well as
    changes made through these tools, so a step may be something the user did by hand. Moves,
    rotations, added or removed objects and parts, painting and splits are recorded as in the
    window; setting changes (set_config, select_preset) are not. Check the result with
    list_objects or render_plate. Refused while slicing (slicing, 409) or while a gizmo is open
    in the window (gizmo_editing, 409). Needs ElegooSlicer MCP ('undo' in get_status
    capabilities)."""
    try:
        async with _client() as c:
            return await c.undo()
    except ApiError as e:
        return _elegoo_err(e, "undo")


@mcp.tool()
async def redo() -> dict:
    """Redo the change undo last reverted, like Ctrl+Y. Returns {"ok", "can_undo",
    "can_redo"}; ok is false when there was nothing to redo (a new change after an undo clears
    what could be redone).

    Uses the window's own history, shared with the user, as undo does. Check the result with
    list_objects or render_plate. Refused while slicing (slicing, 409) or while a gizmo is open
    in the window (gizmo_editing, 409). Needs ElegooSlicer MCP ('undo' in get_status
    capabilities)."""
    try:
        async with _client() as c:
            return await c.redo()
    except ApiError as e:
        return _elegoo_err(e, "undo")


# --- Changes at a layer height (ElegooSlicer MCP, capability "layer_changes") ----
# The marks on the preview's layer slider: switch filament, swap colour by hand, or pause, from
# a given height up. Stored per plate.

_PLATE_OR_CURRENT = Field(description=(
    "0-based plate index from list_plates. Leave it out for the current plate."))


async def _plate_or_current(c, plate: int | None) -> int:
    if plate is not None:
        return plate
    return int((await c.get_plates()).get("current") or 0)


@mcp.tool()
async def list_layer_changes(
    plate: Annotated[int | None, _PLATE_OR_CURRENT] = None,
) -> dict:
    """List the changes set at a layer height on a plate, as the marks on the preview's layer
    slider: filament switches (tool_change), colour-change marks (color_change), pauses (pause)
    and custom G-code (custom, which these tools can list but not add). Returns {"plate",
    "items": [{z, type, slot?, message?}]}. Read-only. An unknown plate returns unknown_plate.
    Needs ElegooSlicer MCP ('layer_changes' in get_status capabilities)."""
    try:
        async with _client() as c:
            return await c.get_layer_changes(await _plate_or_current(c, plate))
    except ApiError as e:
        return _elegoo_err(e, "layer_changes")


@mcp.tool()
async def add_layer_change(
    z: Annotated[float, Field(description=(
        "Height in mm of the layer the change applies from, as the preview's layer slider shows "
        "it: the change happens before that layer prints. It must lie within the height of the "
        "plate's objects (bad_z otherwise)."))],
    type: Annotated[Literal["tool_change", "color_change", "pause"], Field(description=(
        "'tool_change': from this layer up, print with filament slot `slot`; a multi-material "
        "unit (the CANVAS) switches by itself, with the usual flush. 'pause': pause the print "
        "and run the printer's pause G-code (M600 on the Centauri Carbon 2), to drop in a nut or "
        "magnet or to swap filament by hand. 'color_change': the OrcaSlicer colour-change mark; "
        "ElegooSlicer writes no command for it (its M600 insertion is compiled out), so prefer "
        "'pause' for a manual swap."))],
    slot: Annotated[int | None, Field(description=(
        "1-based filament slot to switch to; required for tool_change (see "
        "list_filament_slots)."))] = None,
    message: Annotated[str | None, Field(description=(
        "For pause: a short note kept with the mark, as the slider's Add pause asks for one. "
        "ElegooSlicer does not write it into the G-code, so the printer does not show it."))] = None,
    plate: Annotated[int | None, _PLATE_OR_CURRENT] = None,
) -> dict:
    """Add a filament switch or a pause at a layer height, as adding one on the preview's layer
    slider does. Returns the plate's full list, as list_layer_changes.

    A change at the same z replaces the one there. Use tool_change for a print that changes
    colour by height (a striped vase, a two-colour sign), pause to embed something or to swap
    filament by hand. The plate is left unsliced: re-slice to see the change in the preview and
    in the time and filament estimates (a tool_change adds a filament change and its flush).
    Errors: bad_z, bad_slot or bad_type (422), unknown_plate (404), slicing (409). Needs
    ElegooSlicer MCP ('layer_changes' in get_status capabilities)."""
    if type == "tool_change" and slot is None:
        return {"error": "bad_slot",
                "hint": "tool_change needs slot: the 1-based filament slot to print with from z up."}
    change: dict = {"z": z, "type": type}
    if slot is not None:
        change["slot"] = slot
    if message is not None:
        change["message"] = message
    try:
        async with _client() as c:
            return await c.add_layer_change(await _plate_or_current(c, plate), change)
    except ApiError as e:
        return _elegoo_err(e, "layer_changes")


@mcp.tool()
async def remove_layer_change(
    z: Annotated[float, Field(description=(
        "Height in mm of the change to remove, as list_layer_changes reports it (matched to "
        "0.001 mm)."))],
    plate: Annotated[int | None, _PLATE_OR_CURRENT] = None,
) -> dict:
    """Remove the change set at height z on a plate, as deleting a mark on the preview's layer
    slider does. Returns the plate's remaining list, as list_layer_changes.

    No change at that height returns no_change_at_z (404); take z from list_layer_changes. The
    plate is left unsliced, so re-slice afterwards. Needs ElegooSlicer MCP ('layer_changes' in
    get_status capabilities)."""
    try:
        async with _client() as c:
            return await c.remove_layer_change(await _plate_or_current(c, plate), z)
    except ApiError as e:
        return _elegoo_err(e, "layer_changes")


# --- Calibration (ElegooSlicer MCP, capability "calibration") ------------------


@mcp.tool()
async def run_calibration(
    type: Annotated[Literal["flow_rate", "pressure_advance", "temperature", "retraction",
                            "max_volumetric_speed", "vfa"], Field(description=(
        "Which calibration from the window's Calibration menu: 'flow_rate' (flow ratio), "
        "'pressure_advance', 'temperature' (temperature tower), 'retraction' (retraction "
        "tower), 'max_volumetric_speed', or 'vfa' (vertical fine artifacts, a speed tower)."))],
    params: Annotated[dict | None, Field(description=(
        "The fields of that calibration's dialog in the window, by name; see the tool "
        "description. A missing or wrong field is answered bad_params naming it."))] = None,
    discard_project: Annotated[bool, Field(description=(
        "The calibration replaces the open project with a new one. With unsaved changes it is "
        "refused (unsaved_project) unless this is True, which throws those changes away. To "
        "keep them, save_project first."))] = False,
) -> dict:
    """Set up a calibration print, as the window's Calibration menu does: the open project is
    replaced by a new one holding the test objects, with the settings the test needs. It does
    not slice. Returns {"type", "objects": [{id, name}]}.

    The params mirror the calibration dialogs' fields: a range to sweep for the towers
    (temperature, retraction, max_volumetric_speed, vfa, pressure_advance), typically start,
    end and step, in the dialog's units (C, mm, mm3/s, mm/s, PA value); which pass for
    flow_rate. Afterwards slice_and_wait, look at the result with render_plate, and print it.
    Uses the selected printer, filament and print presets, so pick those first. Errors:
    unsaved_project (409), bad_type or bad_params (422, detail names the field). Needs
    ElegooSlicer MCP ('calibration' in get_status capabilities)."""
    try:
        async with _client() as c:
            return await c.run_calibration(type, params, discard_project)
    except UiTimeout:
        # The slicer stopped waiting for its window, but the new project may still be built
        # there: look before trying again, or a second calibration replaces the first.
        return {"error": "calibration_still_running",
                "hint": "The slicer did not finish setting up the calibration in time. Check "
                        "list_objects in a moment before calling run_calibration again."}
    except ApiError as e:
        return _elegoo_err(e, "calibration")


@mcp.tool()
async def list_plates() -> dict:
    """List the plates in the project: each one's 0-based index, name, whether it is the
    current plate (the one slice, get_gcode, render_plate and get_job_status act on), lock
    state, build volume and the objects it holds. Also says how many plates the build allows
    (max) and whether one can be added or deleted right now. Read-only.

    Plate numbers shown in the OrcaSlicer window are 1-based; every tool here takes the
    0-based index from this list. Needs fork v2.4.2-mcp.13 or later."""
    try:
        async with _client() as c:
            return await c.get_plates()
    except ApiError as e:
        return _plates_err(e)


@mcp.tool()
async def add_plate(
    select: Annotated[bool, Field(description=(
        "Make the new plate the current one, as the toolbar button does. Pass false to "
        "keep working on the plate that is current now."))] = True,
) -> dict:
    """Add an empty plate to the project, the same way the toolbar's "Add plate" button
    does. Returns the new plate's 0-based index for move_object_to_plate and select_plate.

    Use it when parts should print separately: load them, add a plate, move the second
    part across with move_object_to_plate, then select_plate and slice each one. A plate
    cannot be added while a slice is running (409) or at the build's plate limit (422)."""
    try:
        async with _client() as c:
            return await c.create_plate(select)
    except ApiError as e:
        return _plates_err(e)


@mcp.tool()
async def select_plate(
    index: Annotated[int, Field(description=(
        "0-based plate index from list_plates (the window shows plates 1-based)."))],
) -> dict:
    """Make a plate the current one, exactly like clicking it: slice_and_wait, get_gcode,
    render_plate, get_slice_status and get_job_status then act on that plate.

    get_slice_status then reports that plate's own result: its stats if it is still sliced,
    otherwise idle with "plate N is not sliced". Switching plates leaves unsaved config
    overrides alone; it changes only which plate is sliced. Selecting the plate that is
    already current is a no-op."""
    try:
        async with _client() as c:
            return await c.select_plate(index)
    except ApiError as e:
        return _plates_err(e)


@mcp.tool()
async def delete_plate(
    index: Annotated[int, Field(description=(
        "0-based plate index from list_plates. Later plates shift down one index."))],
) -> dict:
    """Delete a plate. The objects it held are NOT deleted: OrcaSlicer parks them beside
    the bed, as the toolbar's delete does, and the reply says how many were moved off-plate
    (objects_moved_off_plate). Use move_object_to_plate to put them somewhere printable.
    OrcaSlicer parks them where the next plate appears, so an add_plate straight after a
    delete picks them back up.

    The only plate cannot be deleted (422 last_plate). Call list_plates first for the index;
    this cannot be undone through the API."""
    try:
        async with _client() as c:
            return await c.delete_plate(index)
    except ApiError as e:
        return _plates_err(e)


@mcp.tool()
async def move_object_to_plate(
    object_id: Annotated[int, Field(description=(
        "Integer id of the object, from the 'id' field of list_objects."))],
    plate: Annotated[int, Field(description=(
        "0-based index of the destination plate, from list_plates or add_plate."))],
    instance: Annotated[int, Field(description=(
        "Which copy to move when the object has several (duplicate_object); 0 is the "
        "original."))] = 0,
) -> dict:
    """Move an object onto another plate. Coming from a plate it keeps its position relative
    to that plate (front-left of plate 1 lands front-left of plate 2); coming from off-plate
    (parked beside the bed) it lands at the destination's centre. If it then overlaps a
    part, shift it with transform_object; arrange_plate would re-pack every plate together.

    list_objects reports each object's plate (-1 when off every plate). Moving leaves both
    plates' slices invalid, so re-slice. An object already on that plate is left where it
    is (moved: false); a locked plate at either end refuses the move (plate_locked)."""
    try:
        async with _client() as c:
            return await c.set_object_plate(object_id, plate, instance)
    except ApiError as e:
        return _plates_err(e)


@mcp.tool()
async def get_job_status() -> dict:
    """Whether the plate's background job worker is idle or still running. Read-only.

    arrange_plate and auto_orient start async jobs; poll this until it reports idle before you
    read object positions or slice, so you act on the settled layout rather than a mid-move
    state."""
    try:
        async with _client() as c:
            return await c.job_status()
    except ApiError as e:
        return _m4b_err(e)


@mcp.tool()
async def watch_events(seconds: int = 10) -> dict:
    """Collect live events (slice.*/config.changed/project.opened) over a bounded window."""
    try:
        async with _client() as c:
            return {"events": await c.collect_events(seconds=seconds)}
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def find_config_keys(substring: str) -> dict:
    """Find config keys containing `substring` (helps discover among the ~600 keys)."""
    try:
        async with _client() as c:
            cfg = await c.get_config(None)
            return {"keys": sorted(k for k in cfg if substring in k)}
    except ApiError as e:
        return _err(e)


_PLATE_CFG_KEYS = ["printable_area", "bed_exclude_area", "skirt_loops", "skirt_distance",
                   "skirt_height", "brim_type", "brim_width", "brim_object_gap",
                   "extruder_clearance_radius", "draft_shield"]


@mcp.tool()
async def diagnose_plate() -> dict:
    """One-call plate diagnosis: app/slice status, objects on the plate, bed + active
    skirt/brim/clearance settings, and the last slice's warnings, so you don't have to
    chain status->objects->config. Start here for 'why won't this slice / fit'.

    Slice warnings are only as complete as the fork exposes today (see get_slice_warnings).
    For a 'does it fit the bed' estimate, pair with check_placement."""
    try:
        async with _client() as c:
            status = await c.get_status()
            try:
                objs = await c.get_objects()
            except NotFound:
                objs = {"count": 0, "objects": [], "note": "objects API needs M4b build"}
            cfg = await c.get_config(_PLATE_CFG_KEYS)
            sl = summarize_slice(await c.slice_status())
        return {"status": status, "objects": objs, "adhesion_bed": cfg,
                "slice": {"state": sl["state"], "warnings": sl["warnings"],
                          "valid": status.get("slice_result_valid")}}
    except ApiError as e:
        return _err(e)


@mcp.tool()
async def check_placement() -> dict:
    """Estimate whether every object (plus its skirt/brim ring) fits inside the printable
    area. Returns per-object fit, expanded first-layer bbox, per-edge clearance (mm), and
    overflow.

    Each object is checked against the plate it sits on (its own plate's origin, from
    list_plates), and its brim ring uses its own brim_type/brim_width/brim_object_gap
    overrides when it has them, else the print preset's. `notes` says when the slicer did not
    report the plate positions or per-object settings, so the estimate fell back.

    APPROXIMATE: uses the object's bounding box footprint, not the sliced toolpath (skirt
    arcs, half-line-width, travel/wipe excluded); single-instance objects only. At ~mm
    margins the true verdict needs get_slice_warnings - this is a fast first-pass."""
    try:
        async with _client() as c:
            objs = await c.get_objects()
            cfg = await c.get_config(placement.CFG_KEYS)
            try:
                plates = await c.get_plates()
            except NotFound:
                plates = None  # build without the plates API; placement notes the fallback
        return placement.check_placement(objs, cfg, plates)
    except ApiError as e:
        return _m4b_err(e)


@mcp.tool()
async def consult(query: str) -> dict:
    """Retrieve curated slicing knowledge + saved context notes for a topic,
    symptom, or intent. ALWAYS call before deriving or changing settings for
    a user goal. Composes principles per situation, never returns preset
    bundles. Falls back to find_config_keys/web search if empty.

    When recommending, present 2-3 concrete options quantified with
    predicted print time and filament mass from real slice results (slice +
    status tools) - never adjectives alone."""
    chunks = [{"file": c.relpath, "title": c.title, "content": c.body}
              for c in search_knowledge(query)]
    return {"chunks": chunks, "notes": _notes.search_notes(query)}


@mcp.tool()
async def check_profile_physics(changes: dict | None = None) -> dict:
    """Deterministic pre-save gate: fetches the live config, overlays optional
    proposed `changes`, and runs flow/temperature/geometry/cooling math.
    RUN THIS BEFORE save_preset. verdict=blocked means DO NOT SAVE.

    The filament preset's own filament_max_volumetric_speed is the flow authority: when the
    demand is within it, the generic melt-rate-vs-temperature estimate (temp_vs_flow) only
    warns, so a vendor profile tuned above that estimate is not blocked. A nozzle at or
    below the material's base temperature (e.g. PLA 195C) still fails temp_vs_flow."""
    try:
        async with _client() as c:
            cfg = await c.get_config(None)
    except ApiError as e:
        return _err(e)
    if changes:
        cfg = dict(cfg) | {k: str(v) for k, v in changes.items()}
    results = run_checks(cfg)
    grouped = {"pass": [], "warn": [], "fail": []}
    for r in results:
        grouped[r.status].append({"name": r.name, "detail": r.detail})
    grouped["verdict"] = "blocked" if grouped["fail"] else ("warnings" if grouped["warn"] else "ok")
    return grouped


@mcp.tool()
async def remember(note: str, scope: str) -> dict:
    """Persist a context fact for future sessions. scope: 'machine:<printer>/<filament>',
    'user', or 'project:<name>'. Local plain files; user-readable and deletable."""
    return {"saved": str(_notes.append_note(note, scope))}


@mcp.tool()
def describe_setting(
    key: Annotated[str, Field(description=(
        "Exact OrcaSlicer config key, e.g. 'layer_height' or 'sparse_infill_density'. Find keys "
        "with search_settings or find_config_keys. Unknown keys return an error."))],
) -> dict:
    """Authoritative definition of one OrcaSlicer setting: label, tooltip, type, unit, valid
    range, enum values, and default. Read-only and offline, so it works even when OrcaSlicer is
    not running.

    Use it to learn a setting's exact type and allowed values before writing it with set_config
    or edit_preset. To find candidate keys by keyword first, use search_settings."""
    rec = settings_schema.describe(key)
    if rec is None:
        return {"error": "unknown_setting", "key": key}
    return rec


@mcp.tool()
def search_settings(query: str, limit: int = 25) -> dict:
    """Search settings by keyword across key/label/tooltip; returns compact matches (key, label, category, short tooltip), ranked key/label first. Offline."""
    return {"results": settings_schema.search(query, limit)}


# --- MCP resources: the offline reference data, addressable as context ---
# All of these read the packaged settings schema / knowledge base, so they
# work even when OrcaSlicer is not running.


@mcp.resource("orca://knowledge", title="Slicing knowledge index",
              description="Index of the curated slicing knowledge base shipped with this server; each entry lists its orca://knowledge/{slug} URI.")
def knowledge_index_resource() -> str:
    lines = ["# Curated slicing knowledge", ""]
    for c in load_knowledge():
        slug = c.relpath[:-3].replace("/", "__") if c.relpath.endswith(".md") else c.relpath.replace("/", "__")
        topics = f" (topics: {', '.join(c.topics)})" if c.topics else ""
        lines.append(f"- orca://knowledge/{slug} — {c.title}{topics}")
    return "\n".join(lines)


@mcp.resource("orca://knowledge/{slug}", title="Slicing knowledge chunk",
              description="One knowledge chunk by slug (relpath with '/' as '__', no .md); see orca://knowledge for the index.")
def knowledge_chunk_resource(slug: str) -> str:
    for c in load_knowledge():
        rel = c.relpath[:-3] if c.relpath.endswith(".md") else c.relpath
        if rel.replace("/", "__") == slug:
            return c.body
    raise ValueError(f"unknown knowledge slug: {slug}")


@mcp.resource("orca://setting/{key}", title="Setting definition",
              description="Authoritative definition of one OrcaSlicer setting (label, tooltip, type, unit, range, enum, default), from OrcaSlicer's own source.")
def setting_resource(key: str) -> str:
    import json as _json
    rec = settings_schema.describe(key)
    if rec is None:
        raise ValueError(f"unknown setting: {key}")
    return _json.dumps(rec, indent=2)


# --- MCP prompts: guided workflows that encode this server's intended use ---


@mcp.prompt(name="slice-a-model", title="Slice a model, safely",
            description="Guided load -> check -> slice -> read-back workflow for one model file.")
def prompt_slice_a_model(model_path: str) -> str:
    return (
        f"Slice {model_path} in OrcaSlicer via the orcaslicer tools, step by step:\n"
        "1. get_status — confirm OrcaSlicer is reachable and note the selected presets.\n"
        "2. list_objects — see what is already on the plate before adding anything.\n"
        f"3. load_model with path {model_path}, then check_placement; fix placement with "
        "auto_orient / arrange_plate / transform_object if it reports problems.\n"
        "4. recall_prints — if this model (or one like it) was printed before, say how it went "
        "(result, any verdict such as warped or stringing, and the settings used) BEFORE proposing changes.\n"
        "5. consult with the model's material and purpose before touching settings.\n"
        "6. slice_and_wait, then get_slice_warnings and get_slice_breakdown; report "
        "print time, filament mass, and any warnings. Call describe_plate and relay its summary "
        "before proposing any orientation or support change; use render_plate to show the result.\n"
        "7. If the user wants to print it, save_gcode (this records the slice so the real outcome "
        "can be learned from later) and hand the returned path to the printer tools.\n"
        "Do not start a print or change temperatures without asking me first."
    )


@mcp.prompt(name="optimize-print-time", title="Optimize print time",
            description="Data-driven print-time reduction from a real slice breakdown, never adjectives alone.")
def prompt_optimize_print_time(constraints: str = "") -> str:
    extra = f"\nMy constraints: {constraints}" if constraints else ""
    return (
        "Reduce the print time of the current plate, driven by data:\n"
        "1. recall_prints — check whether faster settings for this model already failed or "
        "warped before; do not re-propose a known failure.\n"
        "2. slice_and_wait (if no valid slice), then get_slice_breakdown — identify which "
        "feature roles actually dominate time.\n"
        "3. consult with 'print time optimization' plus the dominant roles; only propose "
        "levers the knowledge base or breakdown supports.\n"
        "4. Apply candidate changes with set_config, then check_profile_physics — drop "
        "anything blocked.\n"
        "5. Re-slice and quantify: present 2-3 options as minutes and grams versus the "
        "baseline, with the quality trade-off of each. Do not save presets unless I say so."
        + extra
    )


@mcp.prompt(name="edit-preset-safely", title="Edit a preset, safely",
            description="Preset change with the physics gate: consult -> set_config -> check_profile_physics -> save_preset.")
def prompt_edit_preset_safely(change: str) -> str:
    return (
        f"I want this preset change: {change}\n"
        "Follow the safe path — never edit_preset directly for physics-relevant keys:\n"
        "1. consult and describe_setting for every key involved; confirm units and ranges.\n"
        "2. Apply with set_config on the live config first.\n"
        "3. check_profile_physics — if verdict is blocked, stop and show me why; if "
        "warnings, explain them before proceeding.\n"
        "4. Only then save_preset, and confirm with get_status that nothing is left dirty."
    )


@mcp.tool()
async def load_model(path: str) -> dict:
    """Load a model file (path on the OrcaSlicer host) onto the current plate.
    Accepts .stl/.obj/.3mf, plus .step/.stp on fork v2.3.2-mcp.3+. Large STEP files
    can take a minute to tessellate; the call waits."""
    try:
        async with _client() as c:
            return await c.load_model(path)
    except NotFound as e:
        # The slicer answers 404 file_not_found when the path does not exist on its host.
        # Say so plainly: reporting it as a missing route sent users hunting for a newer build.
        if str(e) == "file_not_found":
            return {"error": f"file not found: {path}",
                    "hint": "Check that the file exists on the OrcaSlicer host and pass an "
                            "absolute path (on Windows, e.g. C:\\Users\\me\\models\\part.stl)."}
        return _m4a_err(e)
    except ApiError as e:
        return _m4a_err(e)


@mcp.tool()
async def save_project(
    path: Annotated[str | None, Field(description=(
        "Where to save, on the computer running the slicer; must end in .3mf. An absolute "
        "path is safest (on Windows, e.g. C:\\Users\\me\\prints\\bracket.3mf). Leave it out "
        "to save over the project's current file."))] = None,
    overwrite: Annotated[bool, Field(description=(
        "Allow replacing a file that already exists at path. Without it an existing file is "
        "left alone and the reply is file_exists."))] = False,
) -> dict:
    """Save the project as a .3mf file, the way File > Save project does, without any dialog.
    Returns {"path": ...} with the file written.

    The file keeps the objects, plates, parts, painting, per-object settings and the current
    settings, so the work can be reopened later. Without path it saves over the project's
    current file (project_unnamed when the project was never saved; pass a path then). A path
    not ending in .3mf is refused (not_3mf), and an existing file is replaced only with
    overwrite=True (file_exists otherwise). save_still_running means the slicer is still
    writing after about 50 s; wait before saving again. Needs ElegooSlicer MCP ('project_save'
    in get_status capabilities)."""
    try:
        async with _client() as c:
            return await c.save_project(path, overwrite)
    except UiTimeout:
        # The slicer stopped waiting for its GUI thread, but the save goes on there. A plain
        # retry to the same path would then fail with file_exists for the file still being
        # written, so say what to do instead of "retry".
        return {"error": "save_still_running",
                "hint": "The slicer did not finish saving in time and may still be writing the "
                        "file. Wait a moment before saving again; a second save to the same path "
                        "needs overwrite=True."}
    except ApiError as e:
        return _elegoo_err(e, "project_save")


@mcp.tool()
async def select_preset(
    type: Annotated[str, Field(description=(
        "Which preset group to switch: 'print' (process/quality), 'filament' (material), "
        "or 'printer' (machine)."))],
    name: Annotated[str, Field(description=(
        "Exact name of an existing preset in that group, as returned by list_presets "
        "(e.g. '0.20mm Standard'). Unknown names are rejected."))],
) -> dict:
    """Make the named preset the active one for its group (print, filament, or printer).

    Discards unsaved overrides in EVERY preset group, not just the one being switched: a
    filament switch also drops unsaved print and printer edits, including ones made by hand
    in the OrcaSlicer window. That is deliberate (OrcaSlicer consults the other groups when
    switching, and any unsaved one would open a modal dialog nothing can dismiss remotely),
    so call save_preset first if any unsaved edit should survive. Newer slicer builds report
    which groups were affected as 'discarded_changes' in the reply.

    This also makes it the canonical way to reset dirty config. It leaves the last slice
    invalid, so re-slice afterwards. Use list_presets for valid names. Because of this, make
    filament selections and edits before print-group edits (docs/order-of-operations.md)."""
    try:
        async with _client() as c:
            return await c.select_preset(type, name)
    except ApiError as e:
        return _m4a_err(e)


@mcp.tool()
async def save_preset(type: str, name: str, detach: bool = False) -> dict:
    """Save the currently edited settings as a named user preset (create or update,
    visible in the GUI immediately). type = print|filament|printer. detach=True saves
    it standalone instead of inheriting the current base preset; use it when creating a
    filament preset for a DIFFERENT material than the one selected, otherwise the new
    preset inherits the base's filament_type and temperatures.

    Run check_profile_physics first; do not save when verdict=blocked."""
    try:
        async with _client() as c:
            return await c.save_preset(type, name, detach)
    except ApiError as e:
        return _m4a_err(e)


_PRESET_TYPES = ("print", "filament", "printer")


def _filter_presets(presets: dict, ptype: str | None, include_system: bool) -> dict:
    """F12 filter: unless include_system, keep only user presets plus whatever is
    currently selected (a selected system preset is the active config, so it stays
    visible). Reports how many system presets were hidden."""
    cats = [ptype] if ptype else list(_PRESET_TYPES)
    out: dict = {}
    hidden = 0
    for cat in cats:
        items = presets.get(cat, []) or []
        if include_system:
            kept = items
        else:
            kept = [p for p in items if not p.get("system") or p.get("selected")]
        hidden += len(items) - len(kept)
        out[cat] = kept
    out["hidden_system"] = hidden
    return out


@mcp.tool()
async def list_presets(type: str | None = None, include_system: bool = False) -> dict:
    """List print/filament/printer presets with system/selected/visible flags.

    F12: by default returns only USER presets plus whatever is currently SELECTED -
    the built-in system presets are ~400 entries of noise. Pass include_system=True
    for the full list, and/or type='print'|'filament'|'printer' to restrict to one
    category. `hidden_system` reports how many system presets were filtered out."""
    if type is not None and type not in _PRESET_TYPES:
        return {"error": "invalid_type", "type": type, "valid": list(_PRESET_TYPES)}
    try:
        async with _client() as c:
            presets = await c.get_presets()
    except ApiError as e:
        return _m4a_err(e)
    return _filter_presets(presets, type, include_system)


@mcp.tool()
async def set_layer_height(object_id: int, mode: str, quality: float = 0.5) -> dict:
    """Variable layer height for one object. mode='adaptive' (quality 0..1, higher = finer
    detail) generates an adaptive profile; mode='reset' (aliases: 'default', 'none')
    restores uniform layers."""
    # F6: the fork only understands adaptive|reset; accept the spellings that
    # were documented for the clear path instead of bouncing them as unknown_mode.
    if mode in ("default", "none"):
        mode = "reset"
    try:
        async with _client() as c:
            return await c.set_layer_height(object_id, mode, quality)
    except ApiError as e:
        return _m4a_err(e)


@mcp.tool()
async def set_height_range(
    object_id: Annotated[int, Field(description="Integer id of the target object, from list_objects.")],
    min_z: Annotated[float | None, Field(description="Lower Z bound of the band in mm (object-relative). Required unless clear=True.")] = None,
    max_z: Annotated[float | None, Field(description="Upper Z bound of the band in mm. Required unless clear=True.")] = None,
    layer_height: Annotated[float | None, Field(description="Layer height in mm to use within the band, e.g. 0.1. Required unless clear=True.")] = None,
    clear: Annotated[bool, Field(description="If True, remove all height-range overrides on the object and ignore the z and layer_height args.")] = False,
) -> dict:
    """Override the layer height over a Z band of one object (e.g. 0 to 5 mm printed at 0.1 mm
    for finer detail near the base). Passing the same min_z and max_z again updates that band's
    height; clear=True removes every band on the object.

    Bands are per-object and invalidate the last slice, so re-slice afterwards. For a single
    height across the whole object use set_layer_height instead. Get the id from list_objects."""
    try:
        async with _client() as c:
            return await c.set_height_range(object_id, min_z, max_z, layer_height, clear)
    except ApiError as e:
        return _m4a_err(e)


@mcp.tool()
async def get_preset_config(type: str, name: str) -> dict:
    """Read the full settings of a named preset without selecting it.
    type = print|filament|printer."""
    try:
        async with _client() as c:
            return await c.get_preset_config(type, name)
    except ApiError as e:
        return _m4a_err(e)


@mcp.tool()
async def delete_preset(type: str, name: str) -> dict:
    """Delete a USER preset (system presets and the currently-selected one are refused).
    type = print|filament|printer."""
    try:
        async with _client() as c:
            return await c.delete_preset(type, name)
    except ApiError as e:
        return _m4a_err(e)


@mcp.tool()
async def edit_preset(type: str, name: str, changes: dict) -> dict:
    """Edit a named preset's settings and persist them: selects it, applies the
    changes atomically, saves under the same name. Runs the check_profile_physics
    gate first (F15) and refuses with error=physics_blocked if the changes would
    INTRODUCE a failing physics check (pre-existing failures do not block
    unrelated edits). For type='filament', checks that also depend on the currently
    selected PRINT preset (flow_ceiling, temp_vs_flow) do not block; they come back
    as cross_layer_warnings, because a filament preset pairs with many print presets."""
    try:
        async with _client() as c:
            await c.guard_preset_edit(type, name, changes)  # refuse before selecting anything
            await c.select_preset(type, name)
            cfg = await c.get_config(None)
            overlay = dict(cfg) | {k: str(v) for k, v in changes.items()}
            fails_before = {r.name for r in run_checks(cfg) if r.status == "fail"}
            fails_after = [r for r in run_checks(overlay) if r.status == "fail"]
            new_fails = [r for r in fails_after if r.name not in fails_before]
            # A filament preset pairs with many print presets, so a check that mixes
            # its inputs with the currently selected print preset's speeds cannot
            # fairly block the filament edit; it is reported instead. (Print-preset
            # edits are still blocked by those checks: they are tuned for the
            # filament selected at the time.)
            blocking, cross_layer = [], []
            for r in new_fails:
                layers = CHECK_LAYERS.get(r.name, frozenset({"print", "filament", "printer"}))
                if type == "filament" and not layers <= {"filament", "printer"}:
                    cross_layer.append(r)
                else:
                    blocking.append(r)
            if blocking:
                return {"error": "physics_blocked", "preset": name,
                        "fails": [{"name": r.name, "detail": r.detail} for r in blocking],
                        "hint": "these changes introduce a physics failure; adjust them or "
                                "inspect with check_profile_physics(changes)"}
            applied = await c.put_config(changes)
            if applied.get("errors"):
                return {"error": "invalid_keys", "errors": applied["errors"]}
            saved = await c.save_preset(type, name)
            out = {"preset": name, "applied": applied.get("applied", []), "saved": saved}
            if cross_layer:
                out["cross_layer_warnings"] = [
                    {"name": r.name, "detail": r.detail,
                     "hint": "judged against the currently selected print preset's speeds; this "
                             "filament preset was saved anyway. Pair it with a slower print preset, "
                             "or re-run check_profile_physics with the print preset you intend to use."}
                    for r in cross_layer]
            return out
    except ApiError as e:
        return _m4a_err(e)


@mcp.tool()
async def rename_preset(
    type: Annotated[str, Field(description="Preset group: 'print', 'filament', or 'printer'.")],
    old_name: Annotated[str, Field(description="Current name of the user preset to rename, as shown by list_presets.")],
    new_name: Annotated[str, Field(description="New name for the preset. Should not collide with an existing preset of the same type.")],
) -> dict:
    """Rename a user preset by copying it to new_name, selecting the copy, and deleting the
    original. Only user presets can be renamed; system presets are read-only.

    Because it selects the renamed preset, this leaves it active and discards unsaved config
    overrides, the same as select_preset, and leaves the last slice invalid, so re-slice
    afterwards."""
    try:
        async with _client() as c:
            await c.select_preset(type, old_name)
            await c.save_preset(type, new_name)
            await c.select_preset(type, new_name)
            deleted = await c.delete_preset(type, old_name)
            return {"renamed": old_name, "to": new_name, "deleted": deleted}
    except ApiError as e:
        return _m4a_err(e)


@mcp.tool()
async def get_gcode() -> dict:
    """Retrieve the last successful slice's G-code as text."""
    try:
        async with _client() as c:
            data = await c.get_gcode()
            return {"bytes": len(data), "gcode": data.decode("utf-8", errors="replace")}
    except Conflict:
        return {"error": "not_sliced"}
    except ApiError as e:
        return _m4a_err(e)


_DESCRIBE_CACHE: dict[str, dict] = {}   # single entry in practice: the current slice


def _gcode_cache_key(data: bytes) -> str:
    h = hashlib.sha1()
    h.update(data[:65536])
    h.update(data[-65536:])
    return f"{len(data)}:{h.hexdigest()}"


async def _slice_purge(c) -> dict | None:
    """The filament-change totals of the current plate's slice (ElegooSlicer MCP, capability
    breakdown_purge), or None when the build does not report them."""
    try:
        breakdown = (await c.slice_status()).get("breakdown") or {}
    except ApiError:
        return None
    purge = breakdown.get("purge")
    return purge if isinstance(purge, dict) else None


def _with_purge(out: dict, purge: dict | None) -> dict:
    """describe_plate's reply with the plate's filament changes added: plate.purge always (when
    the build reports it), and a sentence in the summary when there is at least one change."""
    if purge is None:
        return out
    out = {**out, "plate": {**out["plate"], "purge": purge}}
    if int(purge.get("filament_changes") or 0) > 0:
        out["summary"] = (out["summary"] + "\n" + purge_summary(purge)).strip()
    return out


@mcp.tool()
async def describe_plate() -> dict:
    """Machine-readable plate facts from the last slice's G-code, per object, so you can answer
    orientation and placement questions instead of guessing from Euler angles or a picture:
    how the part stands (flat / tilted / on an edge or corner, from first-layer contact versus its
    widest layer), the first-layer footprint as islands, where overhang extrusions concentrate by
    10 mm height band, where support stands and where its interface touches the part, and which
    side the outer-wall seams sit on (checked against seam_position). Each object gets a
    server-written summary sentence; relay it rather than recomputing. Read-only. Needs a valid
    slice; returns {"error": "not_sliced"} otherwise. Copies of one object are aggregated (Orca
    labels every copy 0); footprint islands still show per-copy contact. The parsed result is
    cached per slice (the G-code itself is still downloaded each call to detect a new slice).
    All bboxes are [x0, y0, x1, y1] in plate millimetres. On ElegooSlicer MCP ('breakdown_purge'
    in get_status capabilities) plate.purge has the filament changes, their time and the flushed
    filament, and the summary names them when the plate changes filament."""
    try:
        async with _client() as c:
            data = await c.get_gcode()
            try:
                objs = (await c.get_objects()).get("objects", [])
            except ApiError:
                objs = []
            purge = await _slice_purge(c)
    except Conflict:
        return {"error": "not_sliced"}
    except ApiError as e:
        return _m4a_err(e)
    key = _gcode_cache_key(data)
    hit = _DESCRIBE_CACHE.get(key)
    if hit is not None:
        return _with_purge({**hit, "cached": True}, purge)
    text = data.decode("utf-8", errors="replace")
    try:
        t0 = time.perf_counter()
        parsed = await asyncio.to_thread(_plate.parse_gcode, text)
        out = _plate.describe(parsed, objs)
        out["parse_seconds"] = round(time.perf_counter() - t0, 2)
    except Exception as e:
        return {"error": "parse_failed", "detail": str(e)[:200]}
    _DESCRIBE_CACHE.clear()
    _DESCRIBE_CACHE[key] = out
    return _with_purge({**out, "cached": False}, purge)


def _safe_gcode_name(name: str) -> str:
    base = Path(name).name
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    base = re.sub(r"\.\.+", "_", base).strip("._")
    if not base or base == "gcode":
        base = f"print_{datetime.datetime.now():%Y%m%d-%H%M%S}"
    return base if base.lower().endswith(".gcode") else base + ".gcode"


# Model-file extensions dropped from object names in a default G-code file name
# ("visor.stl" -> "visor"), so the name stays short.
_MODEL_EXTS = (".stl", ".obj", ".3mf", ".step", ".stp", ".amf", ".drc")


def _short_object_name(name: str) -> str:
    for ext in _MODEL_EXTS:
        if name.lower().endswith(ext):
            return name[: -len(ext)]
    return name


def _plate_gcode_name(plates: dict | None, objs: list[dict]) -> tuple[str, str]:
    """(base of the default file name, model name to record) for save_gcode.

    The G-code is that of the CURRENT plate, so name it after that plate: its own name if
    the user gave it one, else "plate<N>" (1-based, as the window numbers plates) followed by
    the objects on it. Builds without the plates API fall back to the project's first object."""
    fallback = objs[0]["name"] if objs else "plate"
    current = next((p for p in (plates or {}).get("plates") or [] if p.get("current")), None)
    if current is None:
        return fallback, fallback
    # A plate lists one entry per instance; keep each object name once, in plate order.
    names: list[str] = []
    for o in current.get("objects") or []:
        n = _short_object_name(str(o.get("name") or ""))
        if n and n not in names:
            names.append(n)
    # The recorded model name keeps the object's full name ("visor.stl"), as before, so
    # recall_prints' name match still finds it; only the file name is shortened.
    plate_objs = current.get("objects") or []
    model_name = str(plate_objs[0].get("name") or fallback) if plate_objs else fallback
    plate_name = str(current.get("name") or "").strip()
    if plate_name:
        base = plate_name
    else:
        # Up to three object names keep the file name readable; the rest are counted.
        shown = names[:3] + ([f"{len(names) - 3}more"] if len(names) > 3 else [])
        base = "_".join([f"plate{int(current.get('index', 0)) + 1}"] + shown)
    # The name becomes a file name: a slash in a plate or object name must not act as a folder.
    return base.replace("/", "_").replace("\\", "_"), model_name


def _unique_gcode_path(out_dir: Path, fname: str) -> tuple[Path, str]:
    """Never overwrite an existing file: append -2, -3, ... (then a uuid4 suffix as a last resort)."""
    path = out_dir / fname
    if not path.exists():
        return path, fname
    stem, suffix = fname[: -len(".gcode")], ".gcode"
    for n in range(2, 1000):
        candidate = f"{stem}-{n}{suffix}"
        path = out_dir / candidate
        if not path.exists():
            return path, candidate
    candidate = f"{stem}-{uuid.uuid4().hex[:6]}{suffix}"
    return out_dir / candidate, candidate


def _gcode_base_dir() -> Path:
    """Where save_gcode writes the G-code file: PRINT_OUTCOMES_DIR if set (whitespace counts as unset),
    else the shared print-outcomes folder if it already exists (klipper-mcp's start_print finds files
    there), else ~/.orcaslicer-mcp (the folder `remember` already owns). The outcome store itself lives
    at outcomes.store_dir(), which defaults to ~/.orcaslicer-mcp/outcomes."""
    if os.environ.get("PRINT_OUTCOMES_DIR", "").strip() or _outcomes.LEGACY_DIR.exists():
        return _outcomes.store_dir()
    return Path.home() / ".orcaslicer-mcp"


@mcp.tool()
async def save_gcode(filename: str | None = None) -> dict:
    """Save the last successful slice's G-code and record the slice (model, a fingerprint of the
    objects' names and sizes, full settings snapshot, and OrcaSlicer's time and filament estimates)
    in the local outcome store under that filename. When that file is printed, list_print_history (or klipper-mcp's recorder) matches the
    finished job by filename, so the real result joins back to these settings. Returns the saved
    path; hand it to klipper-mcp's start_print. Default filename: <plate>_<timestamp>.gcode, named
    after the current plate (the one sliced): its name if set, else plate<N> plus the objects on
    it. Never
    overwrites an existing file: a name collision gets a -2, -3, ... suffix. Writes into a gcode
    folder under PRINT_OUTCOMES_DIR if set, else under the shared print-outcomes folder
    (~/projects/_shared/print-outcomes) if it already exists, else under ~/.orcaslicer-mcp; the folder
    is created if missing. If the store write fails, the file is still saved and outcome_recorded is
    False."""
    try:
        async with _client() as c:
            data = await c.get_gcode()
            objs = (await c.get_objects()).get("objects") or []
            try:
                plates = await c.get_plates()
            except NotFound:
                plates = None  # build without the plates API: named after the first object
            cfg = await c.get_config(None)
            stats: dict = {}
            printer_name = None
            try:
                stats = (await c.slice_status()).get("stats") or {}
            except ApiError:
                pass  # the estimate is nice to have; the file still saves without it
            try:
                printer_name = ((await c.get_status()).get("presets") or {}).get("printer")
            except ApiError:
                pass
    except Conflict:
        return {"error": "not_sliced"}
    except ApiError as e:
        return _m4a_err(e)
    base, model_name = _plate_gcode_name(plates, objs)
    fname = _safe_gcode_name(filename or f"{base}_{datetime.datetime.now():%Y%m%d-%H%M%S}")
    out_dir = _gcode_base_dir() / "gcode"
    out_dir.mkdir(parents=True, exist_ok=True)
    path, fname = _unique_gcode_path(out_dir, fname)
    try:
        path.write_bytes(data)
    except OSError as e:
        return {"error": "write_failed", "detail": str(e), "path": str(path)}
    row_id = None
    outcome_error = None
    try:
        row_id = _outcomes.record_slice(
            fname, model_name, _outcomes.geometry_hash_for(objs), cfg,
            printer_id=_ptarget.printer_id(printer_name),
            est_time_s=stats.get("estimated_time_seconds"), est_filament_g=stats.get("filament_used_g"))
    except Exception as e:  # noqa: BLE001 - whatever goes wrong while recording, the saved file stays
        outcome_error = str(e) or type(e).__name__
    result = {"path": str(path), "filename": fname, "bytes": len(data), "model_name": model_name,
              "outcome_recorded": row_id is not None, "outcome_row_id": row_id}
    if outcome_error is not None:
        result["outcome_error"] = outcome_error
    return result


def _recall_summary(rows: list[dict], subject: str) -> str:
    n = len(rows)
    if n == 0:
        return f"no past prints of {subject}" if subject != "recent" else "no recorded prints"
    counts: dict[str, int] = {}
    for r in rows:
        k = r.get("result") or "unprinted"
        counts[k] = counts.get(k, 0) + 1
    parts = ", ".join(f"{v} {k}" for k, v in counts.items())
    verdict_counts: dict[str, int] = {}
    for r in rows:
        v = r.get("human_verdict")
        if v:
            verdict_counts[v] = verdict_counts.get(v, 0) + 1
    tail = f" ({', '.join(f'{c} marked {v}' for v, c in sorted(verdict_counts.items()))})" if verdict_counts else ""
    head = f"{n} recent print{'s' if n != 1 else ''}" if subject == "recent" else f"{n} past print{'s' if n != 1 else ''} of {subject}"
    return f"{head}: {parts}{tail}"


@mcp.tool()
async def recall_prints(model_name: str | None = None, limit: int = 5) -> dict:
    """How did past prints of THIS model actually turn out? Matches the current plate by geometry
    (or by model_name if given / the slicer is offline), returning each past print's result
    (success/cancelled/error), your recorded verdict (e.g. 'warped'), and the settings it was sliced
    with. Call this BEFORE slicing and tell the user anything relevant (a past warp, a failed layer
    height). Each row also carries failure_reason (the printer's own words, when known) and the slicer's
    estimates (est_time_s, est_filament_g). duration_s is Moonraker's whole job time, including heating
    up and any pauses, so beside the estimates it overstates an overrun; list_print_history compares the
    print time (without heating or pauses) with the estimate. filament_g comes from the G-code file's
    own estimate, because printers don't report grams. Call list_print_history first to pull in recent
    results. Read-only. Returns available=false and nothing else when no
    outcome store exists yet.
    If neither geometry nor name matches, it returns the most recent prints of ANY model with
    matched_by='recent'; never attribute those to the current model."""
    if not _outcomes.is_available():
        return {"available": False, "matched_by": None, "prints": [], "summary": "no outcome store"}
    objs: list[dict] = []
    try:
        async with _client() as c:
            objs = (await c.get_objects()).get("objects") or []
    except ApiError:
        pass
    limit = max(1, min(limit, 50))
    if objs:
        h = _outcomes.geometry_hash_for(objs)
        rows = _outcomes.recall(geometry_hash=h, limit=limit)
        if rows:
            return {"available": True, "matched_by": "geometry", "prints": rows,
                    "summary": _recall_summary(rows, objs[0]["name"])}
        model_name = model_name or objs[0]["name"]
    if model_name:
        rows = _outcomes.recall(model_name=model_name, limit=limit)
        if rows:
            return {"available": True, "matched_by": "name", "prints": rows,
                    "summary": _recall_summary(rows, model_name)}
    rows = _outcomes.recall(limit=limit)
    return {"available": True, "matched_by": "recent", "prints": rows, "summary": _recall_summary(rows, "recent")}


@mcp.tool()
async def render_plate(view: str = "editor", angle: str = "iso",
                       width: int = 800, height: int = 600,
                       frame: str | None = None):
    """Render a PNG picture of the current plate so you can SEE it.

    view="editor": the models on the bed BEFORE slicing - use to check
    orientation, plate contact, and first-layer footprint (an Euler triple is
    near-unreadable; this is the ground truth). view="preview": the sliced
    toolpaths colored by feature role AFTER a successful slice - support is
    visibly distinct, so use it to check where support actually went.
    angle: iso|top|front|left|right|rear|bottom.
    frame: "plate" zooms out to the whole bed (where the part sits, footprint),
    "object" zooms in on the model/toolpaths (detail). Defaults to "plate" for
    the editor view and "object" for the preview view; pass it explicitly when
    a side view of a small part would otherwise be a speck on a big bed.
    """
    try:
        async with _client() as c:
            png = await c.get_plate_render(view=view, angle=angle,
                                           width=width, height=height, frame=frame)
            return Image(data=png, format="png")
    except Conflict:
        return {"error": "no_slice_result",
                "hint": "slice first, then render view='preview'"}
    except ApiError as e:
        return _m4_err(e, "plate_render (fork v2.3.2-mcp.4+)")



# --- Printer feedback (read-only) ---------------------------------------------
# The printer is found through OrcaSlicer's active printer profile (or ORCA_PRINTER_URL) and asked
# over HTTP GET only. Nothing here sends commands to a printer.


def _printer_api_key() -> str | None:
    return os.environ.get("ORCA_PRINTER_API_KEY", "").strip() or None


def _remembered_age(at) -> str | None:
    """How long ago a remembered time was, in words, or None when it is no usable time: not a number,
    NaN or Infinity (json.loads accepts both), an integer too big for a float, or a number no calendar
    can hold (all of which a hand-edited printer.json can contain). Asking the calendar to read the
    time is the test: it refuses each of those with OverflowError, OSError or ValueError."""
    if isinstance(at, bool) or not isinstance(at, (int, float)):
        return None
    try:
        datetime.datetime.fromtimestamp(at, datetime.timezone.utc)
        age = time.time() - at
    except (OverflowError, OSError, ValueError):
        return None
    return f"{_duration_text(max(0.0, age))} ago"


def _remembered_note(target) -> str:
    age = _remembered_age(target.remembered_at) or "earlier"
    return f"OrcaSlicer isn't running or can't be reached, so this uses the printer remembered {age}."


async def _with_printer(fn) -> dict:
    """Find the printer (override > OrcaSlicer profile > remembered), open the protocol that
    answers, and run fn(target, client). Printer problems come back as {error, message, hint, ...};
    once the printer is known, every reply (error or not) says which one it is in `printer`."""
    target = None
    try:
        target = await _ptarget.resolve_target(_client)
        target, client = await _ptarget.open_printer(target, _printer_api_key())
        async with client:
            out = await fn(target, client)
        if target.source == "remembered" and isinstance(out, dict):
            notes = out.get("notes")
            out["notes"] = (notes if isinstance(notes, list) else []) + [_remembered_note(target)]
        return out
    except PrinterError as e:
        out = e.as_dict()
        if target is not None:
            out.setdefault("printer", target.public())
        return out


@mcp.tool()
async def get_printer_status() -> dict:
    """What the 3D printer is doing right now: state (idle, heating, printing, paused, finished,
    cancelled, error, shutdown, offline), nozzle and bed temperatures with their targets, the current
    job (file, progress, layer, time elapsed and left, and what the time left is based on), and any
    problems the printer reports, each in the printer's own words with a plain-English hint for
    common ones. Relay `headline` as-is. Never invent a cause the printer did not report.

    Finds the printer from OrcaSlicer's active printer profile (or ORCA_PRINTER_URL), so it needs no
    extra setup once the printer is connected in OrcaSlicer. Klipper (Moonraker) printers report the
    most; OctoPrint reports basic status. Read-only: it never sends commands to the printer."""
    return await _with_printer(lambda t, c: take_snapshot(t, c))


@mcp.tool()
async def wait_for_printer(
    until: Annotated[Literal["heated", "printing", "first_layer_done", "finished"], Field(description=(
        "What to wait for. 'heated': every heater with a target is within 3 °C of it. 'printing': "
        "extrusion has started. OctoPrint reports no extrusion, so there it means the job is "
        "running with the heaters at temperature. 'first_layer_done': the second layer has begun "
        "(Klipper only); without layer info from the slicer it is judged from the nozzle height, so "
        "it is approximate. 'finished': the job completed, was cancelled or errored."))],
    timeout_s: Annotated[int, Field(description=(
        "Seconds to wait before returning the latest status. Default 300, at most 1800. "
        "Call again to keep waiting."))] = _pwait.DEFAULT_TIMEOUT_S,
    ctx: Context | None = None,
) -> dict:
    """Wait until the printer reaches a point in a print, checking every 5 seconds. Returns as soon as
    it happens (met: true), or at the timeout with the latest status (met: false). It also returns
    early, with stopped_early saying why, when the printer reports a fault, when the job ends before
    the point it waits for (a cancelled print, say), or after losing contact ("Lost contact with the
    printer"). A job that completes successfully while the wait is for 'printing' or 'first_layer_done'
    counts as met (a one-layer print passes both at once). A met result can carry stopped_early too,
    for example a job that finished with an error. Waiting for 'finished' when nothing is printing
    returns at once with the note 'nothing is printing'. Read-only: it never sends commands."""
    async def report(done: float, total: float, message: str) -> None:
        if ctx is not None:
            await ctx.report_progress(done, total, message)

    return await _with_printer(lambda t, c: _pwait.run_wait(t, c, until, timeout_s, report))


@mcp.tool()
async def list_print_history(limit: int = 10) -> dict:
    """How did recent prints on the printer actually end? For each finished job: result (success,
    cancelled or error), how long it took against the slicer's estimate, filament used, and for failed
    or stopped jobs the printer's own reason while its console still holds it. `summary` says how
    the slicer's time estimates compare with reality once there are a few finished prints. Also copies
    those jobs into the local outcome store (safe to repeat), so recall_prints can learn from them.
    Klipper printers only. `limit` is capped at 50."""
    return await _with_printer(
        lambda t, c: _phistory.print_history(t, c, limit, _ptarget.printer_id_for(t)))


@mcp.tool()
async def check_printer_match() -> dict:
    """Does the active OrcaSlicer profile match the printer it will print on? Compares nozzle size,
    bed size and height, the highest speeds and accelerations, nozzle and bed temperatures, and
    firmware retraction against the printer's own Klipper settings. Each check comes back ok, warn or
    unknown with both values and why it matters; relay `headline` and the warnings. Warnings only: it
    never changes or blocks anything. Run it before printing with a new or edited profile, or when
    prints take longer than estimated. Klipper printers only; needs OrcaSlicer running."""
    async def run(t, c):
        if t.kind != "klipper":
            return {"supported": False, "reason": "Only Klipper printers report their settings.",
                    "printer": t.public()}
        settings = ((await c.objects_query(["configfile=settings"])).get("configfile") or {}).get("settings") or {}
        try:
            async with _client() as fork:
                cfg = await fork.get_config(list(_pmatch.PROFILE_KEYS))
        except ApiError as e:
            raise PrinterError("orca_unreachable",
                               "check_printer_match couldn't read the active profile from OrcaSlicer.",
                               hint=_ptarget.orca_read_hint(
                                   e, "Start OrcaSlicer (MCP build) with the Remote API enabled."),
                               detail=str(e)) from e
        out = _pmatch.compare(cfg, settings)
        out["printer"] = t.public()
        return out

    return await _with_printer(run)


# --- Tool annotations (title + read-only/destructive hints) -----------------
# The Claude connectors directory requires every tool to carry a title and the
# applicable readOnlyHint / destructiveHint. Kept as one table so completeness
# is reviewable at a glance; _apply_tool_annotations() raises on any tool
# missing from it, and tests/test_tool_annotations.py enforces the inverse.

_TOOL_ANNOTATIONS: dict[str, tuple[str, bool, bool]] = {
    # name: (title, read_only, destructive)
    "get_status": ("Get OrcaSlicer status", True, False),
    "get_config": ("Get config values", True, False),
    "get_slice_status": ("Get slice status", True, False),
    "get_slice_warnings": ("Get slice warnings", True, False),
    "get_slice_breakdown": ("Get slice time/flow breakdown", True, False),
    "compare_settings": ("Compare settings", True, False),
    "compare_slices": ("Compare slice variants", True, False),
    "list_objects": ("List plate objects", True, False),
    "list_plates": ("List plates", True, False),
    "get_job_status": ("Get background job status", True, False),
    "watch_events": ("Watch OrcaSlicer events", True, False),
    "find_config_keys": ("Find config keys", True, False),
    "diagnose_plate": ("Diagnose plate issues", True, False),
    "check_placement": ("Check object placement", True, False),
    "consult": ("Consult slicing knowledge", True, False),
    "check_profile_physics": ("Sanity-check profile physics", True, False),
    "describe_setting": ("Describe a setting", True, False),
    "search_settings": ("Search settings", True, False),
    "list_presets": ("List presets", True, False),
    "get_preset_config": ("Get preset config", True, False),
    "get_gcode": ("Download sliced gcode", True, False),
    "describe_plate": ("Describe plate placement from G-code", True, False),
    "save_gcode": ("Save G-code and record the slice", False, False),
    "recall_prints": ("Recall past prints of this model", True, False),
    "render_plate": ("Render plate image", True, False),
    "set_config": ("Set config values", False, False),
    "slice": ("Start slicing", False, False),
    "slice_and_wait": ("Slice and wait for result", False, False),
    "apply_and_slice": ("Apply config and slice", False, False),
    "cancel_slice": ("Cancel running slice", False, False),
    "set_object_config": ("Set per-object config", False, False),
    "duplicate_object": ("Duplicate object", False, False),
    "transform_object": ("Transform object", False, False),
    "arrange_plate": ("Arrange plate", False, False),
    "add_plate": ("Add plate", False, False),
    "select_plate": ("Select plate", False, False),
    "move_object_to_plate": ("Move object to plate", False, False),
    "auto_orient": ("Auto-orient objects", False, False),
    "set_view": ("Switch the Prepare/Preview tab", False, False),
    "list_parts": ("List an object's parts", True, False),
    "add_part": ("Add a modifier, negative part or support enforcer/blocker", False, False),
    "set_part_config": ("Set per-part config", False, False),
    "set_part_filament": ("Set a part's filament", False, False),
    "remove_part": ("Remove a part from an object", False, True),
    "save_project": ("Save project as .3mf (can replace a file)", False, True),
    # Painting replaces whatever paint was there, including the user's own brush work.
    "paint_region": ("Paint support, seam or color on a region", False, True),
    "split_object": ("Split an object into objects or parts", False, False),
    "list_filament_slots": ("List filament slots", True, False),
    "set_filament_slot": ("Set a filament slot's preset or colour", False, False),
    # Removing slots (with force) moves objects and parts that used them to another filament.
    "set_filament_count": ("Set the number of filament slots", False, True),
    "undo": ("Undo the last change", False, False),
    "redo": ("Redo the last undone change", False, False),
    "list_layer_changes": ("List filament changes and pauses by height", True, False),
    "add_layer_change": ("Add a filament change or pause at a height", False, False),
    "remove_layer_change": ("Remove a filament change or pause", False, True),
    # A calibration replaces the open project (refused with unsaved changes unless discarded).
    "run_calibration": ("Set up a calibration print (replaces the project)", False, True),
    "remember": ("Save a note", False, False),
    "load_model": ("Load a model file", False, False),
    "select_preset": ("Select preset", False, False),
    "set_layer_height": ("Set layer height", False, False),
    "set_height_range": ("Set height-range config", False, False),
    "rename_preset": ("Rename preset", False, False),
    "save_preset": ("Save preset (overwrites stored settings)", False, True),
    "edit_preset": ("Edit preset (overwrites stored settings)", False, True),
    "delete_object": ("Delete object from plate", False, True),
    "delete_plate": ("Delete plate (its objects are parked off-plate)", False, True),
    "delete_preset": ("Delete preset", False, True),
    "get_printer_status": ("Get printer status", True, False),
    "wait_for_printer": ("Wait for the printer", True, False),
    "list_print_history": ("List print history", False, False),
    "check_printer_match": ("Check the profile against the printer", True, False),
}


# Tools that reach outside OrcaSlicer (the printer on the network), and tools that are safe to repeat
# although they write local state.
_OPEN_WORLD_TOOLS = frozenset({"get_printer_status", "wait_for_printer", "list_print_history", "check_printer_match"})
_IDEMPOTENT_TOOLS = frozenset({"list_print_history"})


def _apply_tool_annotations() -> None:
    for name, tool in mcp._tool_manager._tools.items():
        title, read_only, destructive = _TOOL_ANNOTATIONS[name]
        tool.title = title
        # destructiveHint is only meaningful on tools that modify state, so
        # read-only tools carry no hint rather than a misleading False.
        tool.annotations = ToolAnnotations(
            title=title,
            readOnlyHint=read_only,
            destructiveHint=None if read_only else destructive,
            idempotentHint=True if name in _IDEMPOTENT_TOOLS else None,
            openWorldHint=True if name in _OPEN_WORLD_TOOLS else None,
        )


_apply_tool_annotations()


def _hide_windows_console() -> None:
    """Hide the console window an MCP client pops when launching this stdio server
    on Windows (GUI clients have no console, so uv.exe/the launcher spawns one).

    The MCP transport uses the redirected stdin/stdout *pipes* the client hands us,
    not the console, so hiding the window leaves the protocol untouched. No-op off
    Windows and when there is no console (e.g. launched from a terminal a user owns
    — GetConsoleWindow returns NULL and ShowWindow(NULL, ...) does nothing).
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
    except Exception:
        pass  # never let a cosmetic tweak stop the server from starting


def main() -> None:
    _hide_windows_console()
    for w in allow_override_warnings():
        print(w, file=sys.stderr)  # stdout carries the MCP protocol; stderr reaches the client log
    mcp.run()


if __name__ == "__main__":
    main()
