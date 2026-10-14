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
