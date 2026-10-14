import math
from orcaslicer_mcp import placement


def _objs(size, offset, instances=1, oid=91, name="enclosure.stl"):
    return {"count": 1, "objects": [
        {"id": oid, "index": 0, "name": name, "size_mm": size, "instances": instances,
         "transform": {"offset": offset, "rotation": [0, 0, 0], "scale": [1, 1, 1]}}]}


BED = "0x0,300x0,300x300,0x300"  # live fork returns comma-separated
PART_SIZE = [250.0, 270.0, 90.0]
PART_OFFSET = [150.0, 150.0, 45.0]


def test_bed_parse_comma_and_semicolon():
    for s in ("0x0,300x0,300x300,0x300", "0x0;300x0;300x300;0x300"):
        out = placement.check_placement(_objs(PART_SIZE, PART_OFFSET), {"printable_area": s})
        assert out["bed"]["min"] == [0.0, 0.0]
        assert out["bed"]["max"] == [300.0, 300.0]


def test_state_a_outer_brim_only_fits():
    # brim 10 (+0.1 gap), no skirt -> ring 10.1; tight Y edge clears ~4.9mm
    cfg = {"printable_area": BED, "brim_type": "outer_only", "brim_width": "10",
           "brim_object_gap": "0.1", "skirt_loops": "0"}
    out = placement.check_placement(_objs(PART_SIZE, PART_OFFSET), cfg)
    assert out["all_fit"] is True
    assert math.isclose(out["ring_mm"], 10.1, abs_tol=1e-6)
    o = out["objects"][0]
    assert o["fits"] is True
    assert math.isclose(o["clearances"]["back"], 4.9, abs_tol=0.05)
    assert math.isclose(o["clearances"]["front"], 4.9, abs_tol=0.05)
    assert math.isclose(o["clearances"]["left"], 14.9, abs_tol=0.05)


def test_state_b_documents_accuracy_floor():
    # brim 5 + 2 skirt loops @ dist 3, line 0.9 -> ring 9.9; predicts +5.1mm (FITS)
    # even though the real slice ERRORED. The static check cannot arbitrate ~mm margins.
    cfg = {"printable_area": BED, "brim_type": "auto_brim", "brim_width": "5",
           "brim_object_gap": "0.1", "skirt_loops": "2", "skirt_distance": "3",
           "skirt_line_width": "0.9"}
    out = placement.check_placement(_objs(PART_SIZE, PART_OFFSET), cfg)
    assert math.isclose(out["ring_mm"], 9.9, abs_tol=1e-6)
    o = out["objects"][0]
    assert o["fits"] is True  # <-- under-flags the real error; B1 warnings are the arbiter
    assert math.isclose(o["clearances"]["back"], 5.1, abs_tol=0.05)


def test_object_over_edge_flagged():
    cfg = {"printable_area": BED, "brim_type": "no_brim", "skirt_loops": "0"}
    out = placement.check_placement(_objs([250, 270, 90], [290, 150, 45]), cfg)
    o = out["objects"][0]
    assert o["fits"] is False
    assert o["overflow_mm"] > 0
    assert out["all_fit"] is False
    # object-only clearance also negative on the right
    assert o["object_only_clearances"]["right"] < 0


def test_missing_bed_degrades():
    out = placement.check_placement(_objs(PART_SIZE, PART_OFFSET), {"brim_type": "no_brim"})
    assert out["bed"] is None
    assert out["all_fit"] is None


def test_multi_instance_flagged_unsupported():
    cfg = {"printable_area": BED, "skirt_loops": "0", "brim_type": "no_brim"}
    out = placement.check_placement(_objs([20, 20, 20], [150, 150, 10], instances=3), cfg)
    o = out["objects"][0]
    assert o.get("supported") is False
    assert "note" in o


# --- plates and per-object brim (Elegoo feedback: other plates and object brim ignored) ---

def _plates(*origins):
    return {"plates": [{"index": i, "origin": [x, y, 0]} for i, (x, y) in enumerate(origins)]}


def _obj(oid, plate, bbox_min, bbox_max, config=None):
    o = {"id": oid, "name": f"part{oid}", "instances": 1, "plate": plate,
         "size_mm": [bbox_max[0] - bbox_min[0], bbox_max[1] - bbox_min[1], 10],
         "transform": {"offset": [0, 0, 0]},
         "bbox_min": [bbox_min[0], bbox_min[1], 0], "bbox_max": [bbox_max[0], bbox_max[1], 10]}
    if config is not None:
        o["config"] = config
    return o


NO_RING = {"printable_area": "0x0,256x0,256x256,0x256", "brim_type": "no_brim", "skirt_loops": "0"}


def test_object_on_second_plate_is_checked_against_that_plate():
    # Plate 2 starts at x=281. A part at x 300..340 is well inside it, but would be
    # "off the bed" if judged against plate 1 (0..256).
    objs = {"objects": [_obj(1, 1, [300, 50], [340, 90], config={})]}
    out = placement.check_placement(objs, NO_RING, _plates((0, 0), (281, 0)))
    o = out["objects"][0]
    assert o["plate"] == 1
    assert o["fits"] is True
    assert o["bed"] == {"min": [281.0, 0.0], "max": [537.0, 256.0]}
    assert math.isclose(o["clearances"]["left"], 19.0)
    assert out["all_fit"] is True
    assert "notes" not in out


def test_object_over_its_own_plate_edge_is_flagged():
    objs = {"objects": [_obj(1, 1, [520, 50], [560, 90], config={})]}
    out = placement.check_placement(objs, NO_RING, _plates((0, 0), (281, 0)))
    o = out["objects"][0]
    assert o["fits"] is False
    assert math.isclose(o["overflow_mm"], 23.0)


def test_object_off_every_plate_does_not_fit():
    objs = {"objects": [_obj(1, -1, [-100, 50], [-60, 90], config={})]}
    out = placement.check_placement(objs, NO_RING, _plates((0, 0)))
    o = out["objects"][0]
    assert o["fits"] is False
    assert "off every plate" in o["note"]
    assert out["all_fit"] is False


def test_per_object_brim_override_widens_the_ring():
    # The preset has no brim, the object asks for a 20 mm one: 10 mm from the edge is not enough.
    objs = {"objects": [_obj(1, 0, [10, 100], [50, 140],
                             config={"brim_type": "outer_only", "brim_width": "20",
                                     "brim_object_gap": "0"})]}
    out = placement.check_placement(objs, NO_RING, _plates((0, 0)))
    o = out["objects"][0]
    assert o["brim_from"] == "object"
    assert math.isclose(o["ring_mm"], 20.0)
    assert o["fits"] is False
    assert math.isclose(o["clearances"]["left"], -10.0)
    assert out["ring_mm"] == 0.0  # the preset's own ring is unchanged


def test_per_object_no_brim_overrides_the_preset_brim():
    cfg = dict(NO_RING, brim_type="outer_only", brim_width="15", brim_object_gap="0")
    objs = {"objects": [_obj(1, 0, [10, 100], [50, 140], config={"brim_type": "no_brim"}),
                        _obj(2, 0, [100, 100], [140, 140], config={})]}
    out = placement.check_placement(objs, cfg, _plates((0, 0)))
    own, preset = out["objects"]
    assert own["brim_from"] == "object" and own["ring_mm"] == 0.0 and own["fits"] is True
    assert preset["brim_from"] == "preset" and math.isclose(preset["ring_mm"], 15.0)


def test_without_plates_falls_back_to_first_plate_and_says_so():
    objs = {"objects": [_obj(1, 1, [300, 50], [340, 90], config={})]}
    out = placement.check_placement(objs, NO_RING)
    assert out["objects"][0]["fits"] is False
    assert "plate" not in out["objects"][0]
    assert any("plate positions not available" in n for n in out["notes"])


def test_missing_object_config_is_noted():
    objs = {"objects": [_obj(1, 0, [10, 10], [50, 50])]}
    out = placement.check_placement(objs, NO_RING, _plates((0, 0)))
    assert out["objects"][0]["brim_from"] == "preset"
    assert any("per-object config not available" in n for n in out["notes"])


def test_bbox_is_preferred_over_offset():
    # offset is the mesh origin; after a rotation it is not the bbox centre.
    o = _obj(1, 0, [200, 10], [250, 60], config={})
    o["transform"]["offset"] = [10, 10, 0]
    out = placement.check_placement({"objects": [o]}, NO_RING, _plates((0, 0)))
    assert out["objects"][0]["expanded_bbox"] == {"min": [200.0, 10.0], "max": [250.0, 60.0]}
