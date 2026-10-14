"""Pure, I/O-free plate-fit estimation.

Advisory ONLY. Given the object footprints (world-space AABB from size_mm + offset,
already baked with rotation/scale by the fork) plus the active skirt/brim settings, it
estimates whether each object *and its skirt/brim ring* stays inside the printable area.

Accuracy floor: this works from the object footprint, NOT the sliced toolpath. It excludes
skirt arc rounding, half-line-width, travel/wipe excursions, and exclusion zones. At small
margins (a few mm) it can report "fits" when OrcaSlicer's own boundary check errors - so it
is a fast first-pass, and the fork's real slice warnings (get_slice_warnings) are the arbiter.

Plates: printable_area is the bed of ONE plate in plate-local coordinates, while object
positions are world coordinates (plate 2 sits beside plate 1, and so on). When the plate list
(GET /plates) is given, each object is checked against the plate it sits on, shifted by that
plate's origin. Without it, every object is checked against the first plate and a note says so.

Brim: an object can override brim_type / brim_width / brim_object_gap (set_object_config);
those overrides replace the print preset's values for that object's ring.
"""
from __future__ import annotations
import re

# Config keys the check needs; server fetches these and passes cfg through.
CFG_KEYS = ["printable_area", "brim_type", "brim_width", "brim_object_gap",
            "skirt_loops", "skirt_distance", "skirt_line_width",
            "initial_layer_line_width", "line_width", "nozzle_diameter"]

# Brim types that add material OUTSIDE the object outline.
_OUTWARD_BRIM = {"outer_only", "outer_and_inner", "auto_brim", "brim_ears", "painted"}


def _f(cfg: dict, key: str, default: float = 0.0) -> float:
    """Coerce a config value (often a string) to float; default on missing/blank/bad."""
    v = cfg.get(key, default)
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _parse_bed(s):
    """Parse a printable_area polygon string ('0x0,300x0,...' or ';'-separated) to
    (min_xy, max_xy). Returns None if absent/unparseable."""
    if not s or not isinstance(s, str):
        return None
    pts = []
    for tok in re.split(r"[;,]", s):
        tok = tok.strip()
        if not tok:
            continue
        parts = tok.split("x")
        if len(parts) != 2:
            return None
        try:
            pts.append((float(parts[0]), float(parts[1])))
        except ValueError:
            return None
    if len(pts) < 3:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return ([min(xs), min(ys)], [max(xs), max(ys)])


def _ring(cfg: dict) -> float:
    """Isotropic outward margin = brim reach + skirt reach (skirt stacks outside brim)."""
    brim_out = 0.0
    if cfg.get("brim_type") in _OUTWARD_BRIM and _f(cfg, "brim_width") > 0:
        brim_out = _f(cfg, "brim_object_gap") + _f(cfg, "brim_width")
    skirt_out = 0.0
    loops = _f(cfg, "skirt_loops")
    if loops > 0:
        skw = next((w for w in (_f(cfg, "skirt_line_width"), _f(cfg, "initial_layer_line_width"),
                                _f(cfg, "line_width"), _f(cfg, "nozzle_diameter")) if w > 0), 0.0)
        skirt_out = _f(cfg, "skirt_distance") + loops * skw
    return brim_out + skirt_out


# Brim settings an object can override in its own config (set_object_config).
_BRIM_KEYS = ("brim_type", "brim_width", "brim_object_gap")


def _object_cfg(cfg: dict, obj: dict) -> tuple[dict, bool]:
    """The print preset's config with this object's own brim overrides on top.
    Returns (config, True when the object overrides at least one brim key)."""
    overrides = obj.get("config") or {}
    own = {k: overrides[k] for k in _BRIM_KEYS if k in overrides}
    return dict(cfg) | own, bool(own)


def _plate_origins(plates: dict) -> dict:
    """{plate index: (x, y)} from GET /plates. A plate's bed is printable_area moved by
    its origin, because printable_area only describes the first plate's position."""
    origins = {}
    for p in plates.get("plates") or []:
        org = p.get("origin")
        if isinstance(org, list) and len(org) >= 2:
            origins[p.get("index")] = (float(org[0]), float(org[1]))
    return origins


def _footprint(o: dict) -> tuple[float, float, float, float]:
    """(centre x, centre y, half width, half depth) of the object on the bed. The fork's
    world bbox is preferred: transform.offset is the mesh origin, which is not the bbox
    centre once the part is rotated. Older builds without bbox fall back to offset+size."""
    bmin, bmax = o.get("bbox_min"), o.get("bbox_max")
    if isinstance(bmin, list) and isinstance(bmax, list) and len(bmin) >= 2 and len(bmax) >= 2:
        return ((bmin[0] + bmax[0]) / 2.0, (bmin[1] + bmax[1]) / 2.0,
                (bmax[0] - bmin[0]) / 2.0, (bmax[1] - bmin[1]) / 2.0)
    size = o.get("size_mm", [0, 0, 0])
    off = o.get("transform", {}).get("offset", [0, 0, 0])
    return float(off[0]), float(off[1]), float(size[0]) / 2.0, float(size[1]) / 2.0


def _clearances(cx, cy, hx, hy, m, bmin, bmax):
    return {
        "left": cx - hx - m - bmin[0],
        "right": bmax[0] - (cx + hx + m),
        "front": cy - hy - m - bmin[1],
        "back": bmax[1] - (cy + hy + m),
    }


def check_placement(objects: dict, cfg: dict, plates: dict | None = None) -> dict:
    """Estimate per-object plate fit including the skirt/brim ring. See module docstring
    for the accuracy floor. Returns bed rect (plate-local), the preset's ring width,
    per-object plate/bed/ring/clearances, all_fit, and notes on data the API did not give."""
    bed = _parse_bed(cfg.get("printable_area"))
    m_preset = _ring(cfg)
    notes = []
    if bed is None:
        return {"bed": None, "ring_mm": m_preset, "objects": [],
                "all_fit": None, "note": "printable_area missing/unparseable"}
    local_min, local_max = bed
    origins = _plate_origins(plates) if plates is not None else None
    if origins is None:
        notes.append("plate positions not available (GET /plates needs fork v2.4.2-mcp.13 or "
                     "later): every object was checked against the first plate's bed")
    if any("config" not in o for o in objects.get("objects", [])):
        notes.append("per-object config not available from this build: the print preset's "
                     "brim was used for every object")
    results = []
    all_fit = True
    for o in objects.get("objects", []):
        entry = {"id": o.get("id"), "name": o.get("name")}
        if o.get("instances", 1) != 1:
            entry.update({"supported": False,
                          "note": "multi-instance not supported by the estimate"})
            results.append(entry)
            all_fit = None if all_fit is not False else False
            continue
        # Which bed to judge against: the plate the object sits on, moved by its origin.
        ox, oy = 0.0, 0.0
        if origins is not None:
            plate = o.get("plate")
            entry["plate"] = plate
            if plate == -1:
                # Parked beside the bed (or left behind by a deleted plate): no plate prints it.
                entry.update({"fits": False, "note": "off every plate; it will not be printed. "
                              "Use move_object_to_plate to put it on one."})
                results.append(entry)
                all_fit = False
                continue
            if plate not in origins:
                entry.update({"supported": False,
                              "note": f"plate {plate} not in the plate list; cannot place it"})
                results.append(entry)
                all_fit = None if all_fit is not False else False
                continue
            ox, oy = origins[plate]
        bmin = [local_min[0] + ox, local_min[1] + oy]
        bmax = [local_max[0] + ox, local_max[1] + oy]
        obj_cfg, own_brim = _object_cfg(cfg, o)
        m = _ring(obj_cfg)
        cx, cy, hx, hy = _footprint(o)
        cl = _clearances(cx, cy, hx, hy, m, bmin, bmax)
        obj_only = _clearances(cx, cy, hx, hy, 0.0, bmin, bmax)
        fits = all(v >= 0 for v in cl.values())
        overflow = max(0.0, -min(cl.values()))
        entry.update({
            "fits": fits,
            "bed": {"min": bmin, "max": bmax},
            "ring_mm": m,
            "brim_from": "object" if own_brim else "preset",
            "expanded_bbox": {"min": [cx - hx - m, cy - hy - m],
                              "max": [cx + hx + m, cy + hy + m]},
            "clearances": cl,
            "object_only_clearances": obj_only,
            "overflow_mm": overflow,
        })
        results.append(entry)
        if not fits:
            all_fit = False
    out = {"bed": {"min": local_min, "max": local_max}, "ring_mm": m_preset,
           "objects": results, "all_fit": all_fit}
    if notes:
        out["notes"] = notes
    return out
