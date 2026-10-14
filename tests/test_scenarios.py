# tests/test_scenarios.py
"""Canonical regression scenario: a 0.8 mm nozzle profile that under-extruded at 205C."""
from orcaslicer_mcp.physics_check import run_checks

PRE_TUNE = {  # as found before tuning
    "nozzle_diameter": "0.8", "layer_height": "0.4", "line_width": "0.85",
    "inner_wall_line_width": "0.88", "inner_wall_speed": "45",
    "outer_wall_line_width": "0.8", "outer_wall_speed": "35",
    "sparse_infill_line_width": "0.9", "sparse_infill_speed": "50",
    "filament_type": "PLA", "nozzle_temperature": "205",
    "filament_max_volumetric_speed": "25", "retraction_length": "0.8",
    "fan_min_speed": "80", "fan_max_speed": "100", "slow_down_layer_time": "5",
}
POST_TUNE = PRE_TUNE | {
    "nozzle_temperature": "215", "filament_max_volumetric_speed": "16",
    "sparse_infill_speed": "45", "retraction_length": "1", "slow_down_layer_time": "8",
}

def test_pre_tune_profile_is_caught():
    # The profile declares filament_max_volumetric_speed 25 and its demand (16.3) is within
    # it, so the declared limit is the authority and the cold-temperature estimate (8.3)
    # warns instead of blocking. It must still be reported, never pass silently.
    r = next(r for r in run_checks(PRE_TUNE) if r.name == "temp_vs_flow")
    assert r.status == "warn", "205C@high-flow must be flagged"
    assert "8.3" in r.detail


def test_pre_tune_profile_is_blocked_when_the_demand_exceeds_the_preset_limit():
    fails = [r for r in run_checks(PRE_TUNE | {"filament_max_volumetric_speed": "12"})
             if r.status == "fail"]
    assert {"flow_ceiling", "temp_vs_flow"} <= {r.name for r in fails}

def test_post_tune_profile_is_clean():
    rs = run_checks(POST_TUNE)
    assert not [r for r in rs if r.status == "fail"], [r.detail for r in rs if r.status == "fail"]
