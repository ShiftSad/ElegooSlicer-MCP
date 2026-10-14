import httpx, respx
import orcaslicer_mcp.server as srv
from orcaslicer_mcp.errors import NotFound

def _env(m):
    m.setenv("ORCA_API_TOKEN", "tok"); m.setenv("ORCA_API_URL", "http://x:13130")

@respx.mock
async def test_find_config_keys(monkeypatch):
    _env(monkeypatch)
    respx.get("http://x:13130/api/v1/config").mock(
        return_value=httpx.Response(200, json={"config": {"layer_height": "0.2",
                                                          "first_layer_height": "0.25", "wall_loops": "2"}}))
    out = await srv.find_config_keys("layer_height")
    assert set(out["keys"]) == {"layer_height", "first_layer_height"}

@respx.mock
async def test_get_gcode_not_sliced(monkeypatch):
    _env(monkeypatch)
    respx.get("http://x:13130/api/v1/gcode").mock(
        return_value=httpx.Response(409, json={"error": "not_sliced"}))
    out = await srv.get_gcode()
    assert out["error"] == "not_sliced"

@respx.mock
async def test_load_model_not_available(monkeypatch):
    _env(monkeypatch)
    respx.post("http://x:13130/api/v1/model").mock(
        return_value=httpx.Response(404, json={"error": "not_found"}))
    out = await srv.load_model("/tmp/x.stl")
    assert "M4a" in out["error"]


@respx.mock
async def test_load_model_missing_file_says_file_not_found(monkeypatch):
    _env(monkeypatch)
    path = "C:/models/missing.stl"
    respx.post("http://x:13130/api/v1/model").mock(
        return_value=httpx.Response(404, json={"error": "file_not_found", "path": path}))
    out = await srv.load_model(path)
    assert out["error"] == f"file not found: {path}"
    assert "absolute path" in out["hint"]
    assert "M4a" not in out["error"]


@respx.mock
async def test_load_model_other_resource_404_keeps_its_message(monkeypatch):
    _env(monkeypatch)
    respx.post("http://x:13130/api/v1/model").mock(
        return_value=httpx.Response(404, json={"error": "unknown_plate"}))
    out = await srv.load_model("C:/models/part.stl")
    assert out == {"error": "unknown_plate"}
