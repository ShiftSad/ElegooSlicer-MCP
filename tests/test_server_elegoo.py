"""Tools for routes only ElegooSlicer MCP has: orient one object, switch the view, parts,
saving the project and region painting."""
import json

import httpx
import respx

import orcaslicer_mcp.server as srv

BASE = "http://x:13130/api/v1"


def _env(m):
    m.setenv("ORCA_API_TOKEN", "tok"); m.setenv("ORCA_API_URL", "http://x:13130")


def _status(*capabilities):
    return httpx.Response(200, json={"app": "ElegooSlicer", "mcp_build": 2,
                                     "capabilities": ["status", "objects", *capabilities]})


def _sent(route):
    return json.loads(route.calls.last.request.content)


# --- auto_orient(object_id) ---------------------------------------------------


@respx.mock
async def test_auto_orient_one_object_sends_its_id(monkeypatch):
    _env(monkeypatch)
    respx.get(f"{BASE}/status").mock(return_value=_status("orient_object"))
    route = respx.post(f"{BASE}/orient").mock(return_value=httpx.Response(202, json={"started": True}))
    out = await srv.auto_orient(object_id=7)
    assert out["started"] is True
    assert _sent(route) == {"object_id": 7}


@respx.mock
async def test_auto_orient_without_id_orients_all_with_no_body(monkeypatch):
    _env(monkeypatch)
    status = respx.get(f"{BASE}/status").mock(return_value=_status())
    route = respx.post(f"{BASE}/orient").mock(return_value=httpx.Response(202, json={"started": True}))
    assert (await srv.auto_orient())["started"] is True
    assert route.calls.last.request.content == b""
    assert not status.called  # no capability check when every object is meant


@respx.mock
async def test_auto_orient_one_object_is_refused_on_a_build_without_it(monkeypatch):
    # An older build ignores the body and would orient EVERY object, so nothing is sent.
    _env(monkeypatch)
    respx.get(f"{BASE}/status").mock(return_value=_status())
    route = respx.post(f"{BASE}/orient").mock(return_value=httpx.Response(202, json={"started": True}))
    out = await srv.auto_orient(object_id=7)
    assert "orient_object" in out["error"] and "ElegooSlicer MCP" in out["error"]
    assert not route.called


@respx.mock
async def test_auto_orient_unknown_object(monkeypatch):
    _env(monkeypatch)
    respx.get(f"{BASE}/status").mock(return_value=_status("orient_object"))
    respx.post(f"{BASE}/orient").mock(return_value=httpx.Response(404, json={"error": "unknown_object"}))
    out = await srv.auto_orient(object_id=99)
    assert out["error"] == "unknown_object"


# --- set_view -----------------------------------------------------------------


@respx.mock
async def test_set_view_sends_the_tab(monkeypatch):
    _env(monkeypatch)
    route = respx.post(f"{BASE}/view").mock(return_value=httpx.Response(200, json={"view": "preview"}))
    out = await srv.set_view("preview")
    assert out == {"view": "preview"}
    assert _sent(route) == {"view": "preview"}


@respx.mock
async def test_set_view_bad_view_422(monkeypatch):
    _env(monkeypatch)
    respx.post(f"{BASE}/view").mock(return_value=httpx.Response(422, json={"error": "bad_view"}))
    out = await srv.set_view("monitor")
    assert out["error"] == "bad_view"


@respx.mock
async def test_set_view_needs_an_elegoo_build(monkeypatch):
    _env(monkeypatch)
    respx.post(f"{BASE}/view").mock(return_value=httpx.Response(404, json={"error": "not_found"}))
    out = await srv.set_view("prepare")
    assert "ElegooSlicer MCP" in out["error"] and "'view'" in out["error"]


# --- parts (volumes) ----------------------------------------------------------

_VOLUME = {"id": 901, "index": 1, "name": "Modifier", "type": "modifier", "extruder": 0,
           "size_mm": [10, 10, 5], "position_mm": [128, 128, 2.5],
           "config": {"sparse_infill_density": "50%"}}


@respx.mock
async def test_list_parts(monkeypatch):
    _env(monkeypatch)
    respx.get(f"{BASE}/objects/42/volumes").mock(return_value=httpx.Response(200, json={
        "object": 42, "volumes": [dict(_VOLUME, id=900, index=0, type="part", name="cube"), _VOLUME]}))
    out = await srv.list_parts(42)
    assert out["object"] == 42
    assert [v["type"] for v in out["volumes"]] == ["part", "modifier"]


@respx.mock
async def test_list_parts_needs_an_elegoo_build(monkeypatch):
    _env(monkeypatch)
    respx.get(f"{BASE}/objects/42/volumes").mock(return_value=httpx.Response(404, json={"error": "not_found"}))
    out = await srv.list_parts(42)
    assert "ElegooSlicer MCP" in out["error"] and "'volumes'" in out["error"]


@respx.mock
async def test_list_parts_unknown_object_is_not_a_capability_gap(monkeypatch):
    _env(monkeypatch)
    respx.get(f"{BASE}/objects/5/volumes").mock(return_value=httpx.Response(404, json={"error": "unknown_object"}))
    out = await srv.list_parts(5)
    assert out["error"] == "unknown_object"


@respx.mock
async def test_add_part_composes_the_body(monkeypatch):
    _env(monkeypatch)
    route = respx.post(f"{BASE}/objects/42/volumes").mock(
        return_value=httpx.Response(201, json={"volume": _VOLUME}))
    out = await srv.add_part(42, "modifier", "box", [10, 10, 5], [128, 128, 2.5],
                             name="base infill", config={"sparse_infill_density": "50%"})
    assert out["volume"]["id"] == 901
    assert _sent(route) == {"type": "modifier", "shape": "box", "size_mm": [10, 10, 5],
                            "position_mm": [128, 128, 2.5], "name": "base infill",
                            "config": {"sparse_infill_density": "50%"}}


@respx.mock
async def test_add_part_leaves_out_name_and_config_when_not_given(monkeypatch):
    _env(monkeypatch)
    route = respx.post(f"{BASE}/objects/42/volumes").mock(
        return_value=httpx.Response(201, json={"volume": _VOLUME}))
    await srv.add_part(42, "support_blocker", "sphere", [8, 8, 8], [100, 100, 4])
    assert _sent(route) == {"type": "support_blocker", "shape": "sphere",
                            "size_mm": [8, 8, 8], "position_mm": [100, 100, 4]}


@respx.mock
async def test_add_part_bad_shape_422(monkeypatch):
    _env(monkeypatch)
    respx.post(f"{BASE}/objects/42/volumes").mock(return_value=httpx.Response(422, json={"error": "bad_shape"}))
    out = await srv.add_part(42, "modifier", "cone", [10, 10, 10], [0, 0, 5])
    assert out["error"] == "bad_shape"


@respx.mock
async def test_add_part_refuses_a_sensitive_key_before_sending(monkeypatch):
    _env(monkeypatch)
    route = respx.post(f"{BASE}/objects/42/volumes").mock(
        return_value=httpx.Response(201, json={"volume": _VOLUME}))
    out = await srv.add_part(42, "modifier", "box", [10, 10, 10], [0, 0, 5],
                             config={"post_process": "evil.exe"})
    assert out["error"].startswith("blocked_by_local_policy") and "post_process" in out["error"]
    assert not route.called


@respx.mock
async def test_remove_part(monkeypatch):
    _env(monkeypatch)
    respx.delete(f"{BASE}/objects/42/volumes/901").mock(return_value=httpx.Response(200, json={"ok": True}))
    assert await srv.remove_part(42, 901) == {"ok": True}


@respx.mock
async def test_remove_last_model_part_409(monkeypatch):
    _env(monkeypatch)
    respx.delete(f"{BASE}/objects/42/volumes/900").mock(return_value=httpx.Response(409, json={"error": "last_part"}))
    out = await srv.remove_part(42, 900)
    assert out["error"] == "last_part"


@respx.mock
async def test_set_part_config(monkeypatch):
    _env(monkeypatch)
    route = respx.put(f"{BASE}/objects/42/volumes/901/config").mock(return_value=httpx.Response(200, json={
        "applied": ["wall_loops"], "errors": {}}))
    out = await srv.set_part_config(42, 901, {"wall_loops": 4})
    assert out["applied"] == ["wall_loops"]
    assert _sent(route) == {"wall_loops": 4}


@respx.mock
async def test_set_part_config_invalid_key_422(monkeypatch):
    _env(monkeypatch)
    respx.put(f"{BASE}/objects/42/volumes/901/config").mock(return_value=httpx.Response(422, json={
        "error": "invalid_keys", "errors": {"no_such_key": "unknown_key"}}))
    out = await srv.set_part_config(42, 901, {"no_such_key": 1})
    assert out["error"] == "invalid_keys" and out["errors"] == {"no_such_key": "unknown_key"}


@respx.mock
async def test_set_part_config_refuses_custom_gcode(monkeypatch):
    _env(monkeypatch)
    route = respx.put(f"{BASE}/objects/42/volumes/901/config").mock(return_value=httpx.Response(200, json={}))
    out = await srv.set_part_config(42, 901, {"machine_start_gcode": "M104 S300"})
    assert "machine_start_gcode" in out["error"]
    assert not route.called


@respx.mock
async def test_set_part_config_unknown_volume(monkeypatch):
    _env(monkeypatch)
    respx.put(f"{BASE}/objects/42/volumes/77/config").mock(return_value=httpx.Response(404, json={"error": "unknown_volume"}))
    out = await srv.set_part_config(42, 77, {"wall_loops": 4})
    assert out["error"] == "unknown_volume"


@respx.mock
async def test_set_part_filament_sends_extruder(monkeypatch):
    _env(monkeypatch)
    route = respx.put(f"{BASE}/objects/42/volumes/901/config").mock(return_value=httpx.Response(200, json={
        "applied": ["extruder"], "errors": {}}))
    out = await srv.set_part_filament(42, 901, 2)
    assert out["applied"] == ["extruder"]
    assert _sent(route) == {"extruder": 2}
