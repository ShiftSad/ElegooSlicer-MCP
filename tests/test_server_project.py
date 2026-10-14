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
    assert (await srv.set_filament_slot(1))["error"] == "nothing_to_change"
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
async def test_split_object_errors(monkeypatch):
    _env(monkeypatch)
    respx.post(f"{BASE}/objects/7/split").mock(return_value=httpx.Response(422, json={"error": "not_splittable"}))
    assert (await srv.split_object(7, "parts"))["error"] == "not_splittable"
    respx.post(f"{BASE}/objects/5/split").mock(return_value=httpx.Response(404, json={"error": "unknown_object"}))
    assert (await srv.split_object(5, "parts"))["error"] == "unknown_object"
    respx.post(f"{BASE}/objects/6/split").mock(return_value=_route_missing())
    _needs(await srv.split_object(6, "parts"), "split")


def test_split_object_hints():
    assert _hints("split_object").read_only_hint is False
    assert _hints("split_object").destructive_hint is False
