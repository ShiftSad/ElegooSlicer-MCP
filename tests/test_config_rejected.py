"""Every tool that writes config keeps the per-key reasons of a refused batch and says that
nothing was applied. The slicer answers a refused config write with
422 {"error": "config_rejected", "applied": [], "errors": {key: reason}}."""
import json

import httpx
import respx

import orcaslicer_mcp.server as srv

BASE = "http://x:13130/api/v1"


def _env(m):
    m.setenv("ORCA_API_TOKEN", "tok"); m.setenv("ORCA_API_URL", "http://x:13130")


def _rejected(errors):
    return httpx.Response(422, json={"error": "config_rejected", "applied": [], "errors": errors})


def _assert_rejected(out, errors):
    assert out["error"] == "config_rejected"
    assert out["errors"] == errors
    assert out["message"].startswith("Nothing was applied")
    for key in errors:
        assert key in out["message"]


@respx.mock
async def test_set_config(monkeypatch):
    _env(monkeypatch)
    respx.put(f"{BASE}/config").mock(return_value=_rejected({"layer_hight": "unknown_key"}))
    out = await srv.set_config({"layer_hight": 0.2})
    _assert_rejected(out, {"layer_hight": "unknown_key"})
    assert "search_settings" in out["hint"]


@respx.mock
async def test_set_config_without_a_token_from_an_older_build(monkeypatch):
    _env(monkeypatch)
    respx.put(f"{BASE}/config").mock(return_value=httpx.Response(
        422, json={"applied": [], "errors": {"layer_hight": "unknown_key"}}))
    out = await srv.set_config({"layer_hight": 0.2})
    _assert_rejected(out, {"layer_hight": "unknown_key"})


@respx.mock
async def test_set_config_readonly_key_gets_its_hint(monkeypatch):
    _env(monkeypatch)
    respx.get(f"{BASE}/config").mock(return_value=httpx.Response(200, json={"config": {}}))
    respx.put(f"{BASE}/config").mock(return_value=_rejected({"nozzle_diameter": "readonly_key"}))
    out = await srv.set_config({"nozzle_diameter": "0.6"})
    _assert_rejected(out, {"nozzle_diameter": "readonly_key"})
    assert "readonly_key" in out["hint"]


@respx.mock
async def test_a_bad_value_keeps_the_slicers_own_words(monkeypatch):
    _env(monkeypatch)
    reason = "Invalid value for layer_height: abc"
    respx.put(f"{BASE}/config").mock(return_value=_rejected({"layer_height": reason}))
    out = await srv.set_config({"layer_height": "abc"})
    _assert_rejected(out, {"layer_height": reason})
    assert "hint" not in out  # nothing to add to the slicer's own reason


@respx.mock
async def test_apply_and_slice_does_not_slice_after_a_rejection(monkeypatch):
    _env(monkeypatch)
    respx.put(f"{BASE}/config").mock(return_value=_rejected({"bogus": "unknown_key"}))
    sl = respx.post(f"{BASE}/slice").mock(return_value=httpx.Response(202, json={"started": True}))
    out = await srv.apply_and_slice({"bogus": 1})
    _assert_rejected(out, {"bogus": "unknown_key"})
    assert not sl.called


@respx.mock
async def test_set_object_config(monkeypatch):
    _env(monkeypatch)
    respx.put(f"{BASE}/objects/9/config").mock(return_value=_rejected({"wall_loops": "Invalid value"}))
    out = await srv.set_object_config(9, {"wall_loops": "x"})
    _assert_rejected(out, {"wall_loops": "Invalid value"})


@respx.mock
async def test_add_part_with_a_rejected_config(monkeypatch):
    _env(monkeypatch)
    respx.post(f"{BASE}/objects/42/volumes").mock(return_value=_rejected({"bogus": "unknown_key"}))
    out = await srv.add_part(42, "modifier", "box", [10, 10, 10], [0, 0, 5], config={"bogus": 1})
    _assert_rejected(out, {"bogus": "unknown_key"})


@respx.mock
async def test_set_part_filament(monkeypatch):
    _env(monkeypatch)
    respx.put(f"{BASE}/objects/42/volumes/901/config").mock(
        return_value=_rejected({"extruder": "Value out of range"}))
    out = await srv.set_part_filament(42, 901, 9)
    _assert_rejected(out, {"extruder": "Value out of range"})


@respx.mock
async def test_edit_preset_does_not_save_after_a_rejection(monkeypatch):
    _env(monkeypatch)
    respx.put(f"{BASE}/preset").mock(return_value=httpx.Response(200, json={"selected": True}))
    respx.get(f"{BASE}/config").mock(return_value=httpx.Response(
        200, json={"config": {"layer_height": "0.2", "nozzle_diameter": "0.4"}}))
    respx.put(f"{BASE}/config").mock(return_value=_rejected({"bogus": "unknown_key"}))
    save = respx.post(f"{BASE}/preset/save").mock(return_value=httpx.Response(200, json={"saved": True}))
    out = await srv.edit_preset("print", "P", {"bogus": 1})
    _assert_rejected(out, {"bogus": "unknown_key"})
    assert not save.called


@respx.mock
async def test_compare_settings_row_keeps_the_reasons(monkeypatch):
    _env(monkeypatch)
    respx.get(f"{BASE}/config").mock(return_value=httpx.Response(
        200, json={"config": {"layer_height": "0.2"}}))
    put = respx.put(f"{BASE}/config").mock(side_effect=[
        _rejected({"layer_height": "Value out of range"}),              # the sweep value
        httpx.Response(200, json={"applied": ["layer_height"], "errors": {}}),  # the restore
    ])
    out = await srv.compare_settings("layer_height", [9.0])
    row = out["rows"][0]
    assert row["error"] == "config_rejected"
    assert row["errors"] == {"layer_height": "Value out of range"}
    assert json.loads(put.calls.last.request.content) == {"layer_height": "0.2"}


@respx.mock
async def test_compare_slices_variant_keeps_the_reasons(monkeypatch):
    _env(monkeypatch)

    def put_handler(request):
        body = json.loads(request.content)
        if "bogus" in body:
            return _rejected({"bogus": "unknown_key"})
        return httpx.Response(200, json={"applied": list(body), "errors": {}})

    respx.put(f"{BASE}/config").mock(side_effect=put_handler)
    respx.get(f"{BASE}/config").mock(return_value=httpx.Response(
        200, json={"config": {"layer_height": "0.2", "nozzle_diameter": "0.4"}}))
    respx.get(f"{BASE}/slice/status").mock(return_value=httpx.Response(200, json={
        "state": "done", "percent": 100, "message": "", "warnings": [],
        "stats": {"estimated_time_seconds": 410, "filament_used_g": 48.0}}))
    respx.get(f"{BASE}/status").mock(return_value=httpx.Response(200, json={"slice_result_valid": True}))
    respx.post(f"{BASE}/slice").mock(return_value=httpx.Response(200, json={"already_valid": False}))
    out = await srv.compare_slices([
        {"name": "good", "changes": {"layer_height": "0.3"}},
        {"name": "broken", "changes": {"bogus": "x"}},
    ])
    rows = {v["name"]: v for v in out["variants"]}
    assert rows["broken"]["error"] == "config_rejected"
    assert rows["broken"]["errors"] == {"bogus": "unknown_key"}
    assert "errors" not in rows["good"]
