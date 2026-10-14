import httpx, respx
import orcaslicer_mcp.server as srv


def _env(m):
    m.setenv("ORCA_API_TOKEN", "tok"); m.setenv("ORCA_API_URL", "http://x:13130")


_BREAKDOWN = {
    "mode": "normal", "total_time_s": 26854.9,
    "roles": [{"role": "inner_wall", "time_s": 100.0, "time_pct": 10.0, "filament_g": 1.0,
               "speed_mm_s": {"min": 10, "max": 46, "mean": 44},
               "flow_mm3_s": {"min": 1.0, "max": 19.9, "mean": 18.0}}],
    "metrics": {}, "layers": [],
}
_CONFIG = {"config": {"layer_height": "0.5", "nozzle_diameter": "0.8", "line_width": "0.85",
                      "filament_max_volumetric_speed": "20", "inner_wall_speed": "55"}}


@respx.mock
async def test_get_slice_breakdown_available_with_prediction(monkeypatch):
    _env(monkeypatch)
    respx.get(url__regex=r"http://x:13130/api/v1/slice/status.*").mock(
        return_value=httpx.Response(200, json={"state": "done", "breakdown": _BREAKDOWN}))
    respx.get(url__regex=r"http://x:13130/api/v1/config.*").mock(
        return_value=httpx.Response(200, json=_CONFIG))
    out = await srv.get_slice_breakdown()
    assert out["available"] is True
    assert out["total_time_s"] == 26854.9
    # inner_wall predicted ~20.4 vs observed 19.9 pinned at ceiling 20 -> clamped
    assert out["prediction_check"][0]["verdict"] == "clamped"


@respx.mock
async def test_get_slice_breakdown_passes_the_purge_on(monkeypatch):
    _env(monkeypatch)
    purge = {"filament_changes": 2, "filament_change_time_s": 95.0, "flush_g": 3.25, "flush_mm": 1080.0}
    respx.get(url__regex=r"http://x:13130/api/v1/slice/status.*").mock(
        return_value=httpx.Response(200, json={"state": "done", "breakdown": dict(_BREAKDOWN, purge=purge)}))
    respx.get(url__regex=r"http://x:13130/api/v1/config.*").mock(
        return_value=httpx.Response(200, json=_CONFIG))
    out = await srv.get_slice_breakdown()
    assert out["purge"] == purge
    assert out["purge_summary"].startswith("2 filament changes: about 1 min 35 s and 3.2 g")


@respx.mock
async def test_get_slice_breakdown_single_filament_purge(monkeypatch):
    _env(monkeypatch)
    purge = {"filament_changes": 0, "filament_change_time_s": 0, "flush_g": 0, "flush_mm": 0}
    respx.get(url__regex=r"http://x:13130/api/v1/slice/status.*").mock(
        return_value=httpx.Response(200, json={"state": "done", "breakdown": dict(_BREAKDOWN, purge=purge)}))
    respx.get(url__regex=r"http://x:13130/api/v1/config.*").mock(
        return_value=httpx.Response(200, json=_CONFIG))
    out = await srv.get_slice_breakdown()
    assert out["purge_summary"] == "No filament changes."


@respx.mock
async def test_get_slice_breakdown_names_the_first_filament_load(monkeypatch):
    # The CC2 start G-code loads the first filament with a purging M6211: time and flush with
    # no filament change at all.
    _env(monkeypatch)
    purge = {"filament_changes": 0, "filament_change_time_s": 62.0, "flush_g": 0.6, "flush_mm": 200.0}
    respx.get(url__regex=r"http://x:13130/api/v1/slice/status.*").mock(
        return_value=httpx.Response(200, json={"state": "done", "breakdown": dict(_BREAKDOWN, purge=purge)}))
    respx.get(url__regex=r"http://x:13130/api/v1/config.*").mock(
        return_value=httpx.Response(200, json=_CONFIG))
    out = await srv.get_slice_breakdown()
    assert out["purge_summary"].startswith(
        "No filament changes; loading the first filament takes about 1 min 2 s and flushes 0.6 g")


@respx.mock
async def test_get_slice_breakdown_without_purge_from_an_older_build(monkeypatch):
    _env(monkeypatch)
    respx.get(url__regex=r"http://x:13130/api/v1/slice/status.*").mock(
        return_value=httpx.Response(200, json={"state": "done", "breakdown": _BREAKDOWN}))
    respx.get(url__regex=r"http://x:13130/api/v1/config.*").mock(
        return_value=httpx.Response(200, json=_CONFIG))
    out = await srv.get_slice_breakdown()
    assert "purge" not in out and "purge_summary" not in out


@respx.mock
async def test_get_slice_breakdown_degrades_on_old_fork(monkeypatch):
    _env(monkeypatch)
    respx.get(url__regex=r"http://x:13130/api/v1/slice/status.*").mock(
        return_value=httpx.Response(200, json={"state": "done"}))  # no breakdown key
    respx.get(url__regex=r"http://x:13130/api/v1/config.*").mock(
        return_value=httpx.Response(200, json=_CONFIG))
    out = await srv.get_slice_breakdown()
    assert out["available"] is False
    assert "reason" in out
