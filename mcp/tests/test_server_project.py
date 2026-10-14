"""Project tools for routes only ElegooSlicer MCP has: filament slots, split, undo/redo, changes
at a layer height and calibration (tools/overnight/CONTRACT.md)."""
import json

import httpx
import respx

import orcaslicer_mcp.server as srv

BASE = "http://x:13130/api/v1"


def _env(m):
    m.setenv("ORCA_API_TOKEN", "tok"); m.setenv("ORCA_API_URL", "http://x:13130")


def _sent(route):
    return json.loads(route.calls.last.request.content)


def _route_missing():
    # What any build answers for a route it does not have.
    return httpx.Response(404, json={"error": "not_found"})


def _hints(name):
    return srv.mcp._tool_manager._tools[name].annotations


def _needs(out, capability):
    assert "ElegooSlicer MCP" in out["error"] and f"'{capability}'" in out["error"]


_SLOTS = {"count": 2, "max": 16, "slots": [
    {"slot": 1, "preset": "Elegoo PLA @ECC2", "type": "PLA", "color": "#ffffff",
     "used_on_current_plate": True},
    {"slot": 2, "preset": "Elegoo PLA @ECC2", "type": "PLA", "color": "#ff0000",
     "used_on_current_plate": False}]}


# --- filament slots -------------------------------------------------------------


@respx.mock
async def test_list_filament_slots(monkeypatch):
    _env(monkeypatch)
    respx.get(f"{BASE}/filaments").mock(return_value=httpx.Response(200, json=_SLOTS))
    out = await srv.list_filament_slots()
    assert out == _SLOTS


@respx.mock
async def test_list_filament_slots_needs_an_elegoo_build(monkeypatch):
    _env(monkeypatch)
    respx.get(f"{BASE}/filaments").mock(return_value=_route_missing())
    _needs(await srv.list_filament_slots(), "filaments")


@respx.mock
async def test_set_filament_slot_sends_preset_and_color(monkeypatch):
    _env(monkeypatch)
    route = respx.put(f"{BASE}/filaments/2").mock(return_value=httpx.Response(200, json=_SLOTS["slots"][1]))
    out = await srv.set_filament_slot(2, preset="Elegoo PLA @ECC2", color="#ff0000")
    assert out["slot"] == 2
    assert _sent(route) == {"preset": "Elegoo PLA @ECC2", "color": "#ff0000"}


@respx.mock
async def test_set_filament_slot_color_only(monkeypatch):
    _env(monkeypatch)
    route = respx.put(f"{BASE}/filaments/1").mock(return_value=httpx.Response(200, json=_SLOTS["slots"][0]))
    await srv.set_filament_slot(1, color="#00FF7f")
    assert _sent(route) == {"color": "#00FF7f"}


@respx.mock
async def test_set_filament_slot_refuses_nothing_and_a_bad_color_locally(monkeypatch):
    _env(monkeypatch)
    route = respx.put(f"{BASE}/filaments/1").mock(return_value=httpx.Response(200, json={}))
    assert (await srv.set_filament_slot(1))["error"] == "missing_fields"
    for bad in ("red", "#ff00", "ff0000", "#gg0000", "#ff0000aa"):
        assert (await srv.set_filament_slot(1, color=bad))["error"] == "bad_color", bad
    assert not route.called


@respx.mock
async def test_set_filament_slot_errors_pass_through(monkeypatch):
    _env(monkeypatch)
    respx.put(f"{BASE}/filaments/9").mock(return_value=httpx.Response(404, json={"error": "unknown_slot"}))
    assert (await srv.set_filament_slot(9, color="#000000"))["error"] == "unknown_slot"
    respx.put(f"{BASE}/filaments/1").mock(return_value=httpx.Response(
        422, json={"error": "incompatible_preset", "detail": "not for this printer"}))
    out = await srv.set_filament_slot(1, preset="Bambu PLA Basic @BBL X1C")
    assert out["error"] == "incompatible_preset" and out["detail"] == "not for this printer"


@respx.mock
async def test_set_filament_count(monkeypatch):
    _env(monkeypatch)
    route = respx.put(f"{BASE}/filaments").mock(return_value=httpx.Response(200, json=_SLOTS))
    out = await srv.set_filament_count(2)
    assert out["count"] == 2
    assert _sent(route) == {"count": 2}
    await srv.set_filament_count(1, force=True)
    assert _sent(route) == {"count": 1, "force": True}


@respx.mock
async def test_set_filament_count_slot_in_use_names_the_users(monkeypatch):
    _env(monkeypatch)
    used_by = [{"object_id": 4, "name": "helmet.stl"}, {"object_id": 4, "name": "visor", "volume_id": 12}]
    respx.put(f"{BASE}/filaments").mock(return_value=httpx.Response(
        409, json={"error": "slot_in_use", "used_by": used_by}))
    out = await srv.set_filament_count(1)
    assert out["error"] == "slot_in_use"
    assert out["used_by"] == used_by


@respx.mock
async def test_set_filament_count_bad_count(monkeypatch):
    _env(monkeypatch)
    respx.put(f"{BASE}/filaments").mock(return_value=httpx.Response(422, json={"error": "bad_count", "max": 16}))
    out = await srv.set_filament_count(40)
    assert out["error"] == "bad_count" and out["max"] == 16


def test_filament_tools_hints():
    assert _hints("list_filament_slots").read_only_hint is True
    assert _hints("set_filament_slot").destructive_hint is False
    # Removing slots moves what used them to another filament.
    assert _hints("set_filament_count").destructive_hint is True


# --- split ----------------------------------------------------------------------


@respx.mock
async def test_split_object(monkeypatch):
    _env(monkeypatch)
    route = respx.post(f"{BASE}/objects/7/split").mock(return_value=httpx.Response(200, json={"ids": [8, 9]}))
    out = await srv.split_object(7, "objects")
    assert out == {"ids": [8, 9]}
    assert _sent(route) == {"to": "objects"}


@respx.mock
async def test_split_object_part_and_heights(monkeypatch):
    _env(monkeypatch)
    route = respx.post(f"{BASE}/objects/7/split").mock(return_value=httpx.Response(
        200, json={"ids": [70, 71, 72], "object_id": 7}))
    await srv.split_object(7, "parts", part_id=71)
    assert _sent(route) == {"to": "parts", "volume_id": 71}
    await srv.split_object(7, "objects", drop_to_bed=False)
    assert _sent(route) == {"to": "objects", "drop_to_bed": False}


@respx.mock
async def test_split_object_errors(monkeypatch):
    _env(monkeypatch)
    respx.post(f"{BASE}/objects/7/split").mock(return_value=httpx.Response(422, json={"error": "not_splittable"}))
    assert (await srv.split_object(7, "parts"))["error"] == "not_splittable"
    respx.post(f"{BASE}/objects/5/split").mock(return_value=httpx.Response(404, json={"error": "unknown_object"}))
    assert (await srv.split_object(5, "parts"))["error"] == "unknown_object"
    respx.post(f"{BASE}/objects/6/split").mock(return_value=_route_missing())
    _needs(await srv.split_object(6, "parts"), "split")


@respx.mock
async def test_split_object_still_running(monkeypatch):
    # The slicer keeps splitting after the tool's wait ran out: say so, do not report a failure.
    _env(monkeypatch)
    route = respx.post(f"{BASE}/objects/7/split").mock(side_effect=httpx.ReadTimeout("slow"))
    out = await srv.split_object(7, "objects")
    assert out["error"] == "split_unconfirmed" and "list_objects" in out["hint"]
    assert route.calls.last.request.extensions["timeout"]["read"] == 50.0


def test_split_object_hints():
    assert _hints("split_object").read_only_hint is False
    assert _hints("split_object").destructive_hint is False


# --- undo / redo ----------------------------------------------------------------


@respx.mock
async def test_undo_and_redo(monkeypatch):
    _env(monkeypatch)
    u = respx.post(f"{BASE}/undo").mock(return_value=httpx.Response(
        200, json={"ok": True, "can_undo": False, "can_redo": True}))
    r = respx.post(f"{BASE}/redo").mock(return_value=httpx.Response(
        200, json={"ok": False, "can_undo": True, "can_redo": False}))
    assert (await srv.undo()) == {"ok": True, "can_undo": False, "can_redo": True}
    assert (await srv.redo())["ok"] is False
    assert u.called and r.called


@respx.mock
async def test_undo_refused_while_slicing_and_on_an_older_build(monkeypatch):
    _env(monkeypatch)
    respx.post(f"{BASE}/undo").mock(return_value=httpx.Response(409, json={"error": "slicing"}))
    assert (await srv.undo())["error"] == "slicing"
    respx.post(f"{BASE}/redo").mock(return_value=_route_missing())
    _needs(await srv.redo(), "undo")


def test_undo_redo_hints():
    for name in ("undo", "redo"):
        assert _hints(name).read_only_hint is False and _hints(name).destructive_hint is False


# --- changes at a layer height ----------------------------------------------------

_PLATES = {"current": 1, "plates": [{"index": 0, "current": False}, {"index": 1, "current": True}]}
_CHANGES = {"plate": 1, "items": [{"z": 5.0, "type": "tool_change", "slot": 2},
                                  {"z": 12.4, "type": "pause", "message": "insert magnet"},
                                  {"z": 15.0, "type": "custom", "gcode": "M117 hello"}]}


@respx.mock
async def test_list_layer_changes_defaults_to_the_current_plate(monkeypatch):
    _env(monkeypatch)
    respx.get(f"{BASE}/plates").mock(return_value=httpx.Response(200, json=_PLATES))
    route = respx.get(f"{BASE}/plates/1/layer_changes").mock(return_value=httpx.Response(200, json=_CHANGES))
    assert (await srv.list_layer_changes()) == _CHANGES
    assert route.called


@respx.mock
async def test_list_layer_changes_of_another_plate(monkeypatch):
    _env(monkeypatch)
    plates = respx.get(f"{BASE}/plates").mock(return_value=httpx.Response(200, json=_PLATES))
    respx.get(f"{BASE}/plates/0/layer_changes").mock(return_value=httpx.Response(
        200, json={"plate": 0, "items": []}))
    assert (await srv.list_layer_changes(plate=0))["items"] == []
    assert not plates.called  # no need to look up the current plate


@respx.mock
async def test_add_layer_change_tool_change(monkeypatch):
    _env(monkeypatch)
    respx.get(f"{BASE}/plates").mock(return_value=httpx.Response(200, json=_PLATES))
    route = respx.post(f"{BASE}/plates/1/layer_changes").mock(return_value=httpx.Response(201, json=_CHANGES))
    out = await srv.add_layer_change(5.0, "tool_change", slot=2)
    assert out == _CHANGES
    assert _sent(route) == {"z": 5.0, "type": "tool_change", "slot": 2}


@respx.mock
async def test_add_layer_change_pause_with_message(monkeypatch):
    _env(monkeypatch)
    route = respx.post(f"{BASE}/plates/0/layer_changes").mock(return_value=httpx.Response(201, json=_CHANGES))
    await srv.add_layer_change(12.4, "pause", message="insert magnet", plate=0)
    assert _sent(route) == {"z": 12.4, "type": "pause", "message": "insert magnet"}


@respx.mock
async def test_add_layer_change_tool_change_needs_a_slot(monkeypatch):
    _env(monkeypatch)
    route = respx.post(f"{BASE}/plates/0/layer_changes").mock(return_value=httpx.Response(201, json={}))
    out = await srv.add_layer_change(5.0, "tool_change", plate=0)
    assert out["error"] == "bad_slot"
    assert not route.called


@respx.mock
async def test_add_layer_change_errors_pass_through(monkeypatch):
    _env(monkeypatch)
    respx.post(f"{BASE}/plates/0/layer_changes").mock(return_value=httpx.Response(
        422, json={"error": "bad_z", "detail": "z must be within 0.2..20 mm"}))
    out = await srv.add_layer_change(99.0, "pause", plate=0)
    assert out["error"] == "bad_z" and "20 mm" in out["detail"]
    respx.post(f"{BASE}/plates/7/layer_changes").mock(return_value=httpx.Response(404, json={"error": "unknown_plate"}))
    assert (await srv.add_layer_change(1.0, "pause", plate=7))["error"] == "unknown_plate"


@respx.mock
async def test_remove_layer_change_sends_z_as_a_query(monkeypatch):
    _env(monkeypatch)
    route = respx.delete(f"{BASE}/plates/0/layer_changes").mock(return_value=httpx.Response(
        200, json={"plate": 0, "items": []}))
    out = await srv.remove_layer_change(5.0, plate=0)
    assert out["items"] == []
    assert route.calls.last.request.url.params["z"] == "5.0"


@respx.mock
async def test_remove_layer_change_nothing_there(monkeypatch):
    _env(monkeypatch)
    respx.delete(f"{BASE}/plates/0/layer_changes").mock(return_value=httpx.Response(
        404, json={"error": "no_change_at_z"}))
    assert (await srv.remove_layer_change(3.3, plate=0))["error"] == "no_change_at_z"


@respx.mock
async def test_layer_changes_need_an_elegoo_build(monkeypatch):
    _env(monkeypatch)
    respx.get(f"{BASE}/plates/0/layer_changes").mock(return_value=_route_missing())
    _needs(await srv.list_layer_changes(plate=0), "layer_changes")


def test_add_layer_change_offers_no_colour_change():
    # ElegooSlicer writes no G-code for a colour change and its route refuses one.
    import typing
    hints = typing.get_type_hints(srv.add_layer_change, include_extras=True)
    assert typing.get_args(typing.get_args(hints["type"])[0]) == ("tool_change", "pause")


def test_layer_change_hints():
    assert _hints("list_layer_changes").read_only_hint is True
    assert _hints("add_layer_change").destructive_hint is False
    assert _hints("remove_layer_change").destructive_hint is True


# --- calibration ------------------------------------------------------------------


@respx.mock
async def test_run_calibration(monkeypatch):
    _env(monkeypatch)
    route = respx.post(f"{BASE}/calibration").mock(return_value=httpx.Response(200, json={
        "type": "temperature", "objects": [{"id": 1, "name": "temperature_tower"}]}))
    out = await srv.run_calibration("temperature", {"start": 230, "end": 190, "step": 5})
    assert out["objects"][0]["name"] == "temperature_tower"
    assert _sent(route) == {"type": "temperature", "params": {"start": 230, "end": 190, "step": 5}}


@respx.mock
async def test_run_calibration_discard_project(monkeypatch):
    _env(monkeypatch)
    route = respx.post(f"{BASE}/calibration").mock(return_value=httpx.Response(200, json={
        "type": "flow_rate", "objects": []}))
    await srv.run_calibration("flow_rate", discard_project=True)
    assert _sent(route) == {"type": "flow_rate", "params": {}, "discard_project": True}


@respx.mock
async def test_run_calibration_errors_pass_through(monkeypatch):
    _env(monkeypatch)
    respx.post(f"{BASE}/calibration").mock(side_effect=[
        httpx.Response(409, json={"error": "unsaved_project"}),
        httpx.Response(422, json={"error": "bad_params", "detail": "step must be above 0"}),
        _route_missing(),
    ])
    assert (await srv.run_calibration("retraction", {"step": 1}))["error"] == "unsaved_project"
    out = await srv.run_calibration("retraction", {"step": 0})
    assert out["error"] == "bad_params" and out["detail"] == "step must be above 0"
    _needs(await srv.run_calibration("retraction"), "calibration")


@respx.mock
async def test_run_calibration_unsaved_project_names_what_is_unsaved(monkeypatch):
    _env(monkeypatch)
    respx.post(f"{BASE}/calibration").mock(return_value=httpx.Response(409, json={
        "error": "unsaved_project", "project_unsaved": True, "presets_unsaved": ["print"]}))
    out = await srv.run_calibration("pressure_advance", {"method": "line", "end": 0.08})
    assert out == {"error": "unsaved_project", "project_unsaved": True, "presets_unsaved": ["print"]}


@respx.mock
async def test_run_calibration_failed_to_load(monkeypatch):
    _env(monkeypatch)
    respx.post(f"{BASE}/calibration").mock(return_value=httpx.Response(
        500, json={"error": "calibration_failed"}))
    assert (await srv.run_calibration("vfa"))["error"] == "calibration_failed"


@respx.mock
async def test_run_calibration_timeout_says_to_look_first(monkeypatch):
    _env(monkeypatch)
    respx.post(f"{BASE}/calibration").mock(side_effect=httpx.ReadTimeout("slow"))
    out = await srv.run_calibration("vfa")
    assert out["error"] == "calibration_still_running"


def test_run_calibration_hints():
    # It replaces the open project.
    assert _hints("run_calibration").destructive_hint is True
