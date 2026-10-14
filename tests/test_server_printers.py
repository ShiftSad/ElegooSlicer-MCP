"""ElegooSlicer MCP's printer and CANVAS tools: list_printers, get_canvas, sync_canvas_to_project,
preview_filament_mapping (mock slicer API)."""
import json

import httpx
import respx

import orcaslicer_mcp.server as srv

F = "http://x:13130/api/v1"


def _env(m):
    m.setenv("ORCA_API_TOKEN", "tok"); m.setenv("ORCA_API_URL", "http://x:13130")


def printer(pid="p1", name="CC2", status="idle", task=None):
    return {"id": pid, "name": name, "model": "Elegoo Centauri Carbon 2", "vendor": "Elegoo",
            "host": "192.168.1.50", "port": None, "network": "lan", "firmware": "1.0.2",
            "connected": True, "status": status, "status_code": 0, "task": task,
            "supports": {"canvas": True}}


def listing(*printers, selected="p1"):
    respx.get(f"{F}/printers").mock(return_value=httpx.Response(
        200, json={"printers": list(printers), "selected_id": selected}))


def tray(tid, status="loaded", type_="PLA", color="#ffffff", preset="Elegoo PLA @ECC2"):
    empty = status == "empty"
    return {"id": tid, "unit": "0", "index": int(tid[1]) - 1, "status": status,
            "type": None if empty else type_, "name": None if empty else type_,
            "vendor": None if empty else "Elegoo", "color": None if empty else color,
            "nozzle_temp": None if empty else [190, 230], "bed_temp": None,
            "matched_preset": None if empty else preset, "matched_alias": None}


CANVAS = {"connected": True, "auto_refill": False, "active": {"unit": "0", "tray": "A1"},
          "units": [{"id": "0", "name": None, "model": None, "connected": True,
                     "temperature_c": None, "humidity": None,
                     "trays": [tray("A1"), tray("A2", "empty"), tray("A3", type_="PETG", color="#ff0000",
                                                                    preset=None),
                               tray("A4", "unknown_filament")]}],
          "external_spool": None, "age_s": 0}


# --- list_printers -------------------------------------------------------------------------------


@respx.mock
async def test_list_printers_relays_the_list_with_a_summary(monkeypatch):
    _env(monkeypatch)
    listing(printer(status="printing", task={"file": "helmet.gcode", "progress": 42}),
            printer("p2", "Garage"), selected="p1")
    out = await srv.list_printers()
    assert [p["id"] for p in out["printers"]] == ["p1", "p2"] and out["selected_id"] == "p1"
    assert "CC2 (Elegoo Centauri Carbon 2, 192.168.1.50, LAN): printing helmet.gcode 42%" in out["summary"]
    assert out["summary"].endswith("The Print dialog would pick CC2.")


@respx.mock
async def test_list_printers_when_there_are_none(monkeypatch):
    _env(monkeypatch)
    listing(selected=None)
    out = await srv.list_printers()
    assert out["printers"] == [] and "Device tab" in out["summary"]


@respx.mock
async def test_list_printers_on_an_older_build_names_the_capability(monkeypatch):
    _env(monkeypatch)
    respx.get(f"{F}/printers").mock(return_value=httpx.Response(404, json={"error": "not_found"}))
    out = await srv.list_printers()
    assert "'printers'" in out["error"] and "ElegooSlicer MCP" in out["error"]


@respx.mock
async def test_list_printers_never_relays_a_secret(monkeypatch):
    _env(monkeypatch)
    leaky = {**printer(), "accessCode": "SECRET1", "password": "SECRET2", "serialNumber": "SECRET3",
             "extraInfo": "{\"token\": \"SECRET4\"}", "webUrl": "SECRET5", "mainboardId": "SECRET6",
             "username": "SECRET7", "pinCode": "SECRET8", "token": "SECRET9"}
    listing(leaky)
    out = await srv.list_printers()
    assert out["printers"][0]["id"] == "p1" and "SECRET" not in json.dumps(out)


# --- get_canvas -------------------------------------------------------------------------------------


@respx.mock
async def test_get_canvas_highlights_the_trays_that_are_not_ready(monkeypatch):
    _env(monkeypatch)
    listing(printer())
    route = respx.get(f"{F}/printers/p1/canvas").mock(return_value=httpx.Response(200, json=CANVAS))
    out = await srv.get_canvas()
    assert route.called and out["units"][0]["trays"][0]["id"] == "A1"
    assert out["not_ready"] == [{"tray": "A2", "status": "empty"},
                                {"tray": "A4", "status": "unknown_filament"}]
    assert out["unmatched"] == ["A3"]
    assert out["summary"].startswith("CANVAS connected, auto refill off, feeding from A1.")
    assert "A1 PLA #ffffff (Elegoo PLA @ECC2)" in out["summary"]
    assert "A3 PETG #ff0000 (no matching preset)" in out["summary"]
    assert "Not ready: A2 empty; A4 unknown filament." in out["summary"]


@respx.mock
async def test_get_canvas_picks_the_printer_by_name(monkeypatch):
    _env(monkeypatch)
    listing(printer(), printer("p2", "Garage"))
    route = respx.get(f"{F}/printers/p2/canvas").mock(return_value=httpx.Response(200, json=CANVAS))
    await srv.get_canvas(printer="garage")
    assert route.called


@respx.mock
async def test_get_canvas_unknown_printer(monkeypatch):
    _env(monkeypatch)
    listing(printer())
    out = await srv.get_canvas(printer="Kitchen")
    assert out["error"] == "not_configured" and "CC2 (p1)" in out["hint"]


@respx.mock
async def test_get_canvas_errors_keep_the_token_and_explain_it(monkeypatch):
    _env(monkeypatch)
    listing(printer())
    respx.get(f"{F}/printers/p1/canvas").mock(
        return_value=httpx.Response(504, json={"error": "printer_timeout"}))
    out = await srv.get_canvas()
    assert out["error"] == "printer_timeout" and "Centauri Carbon 2" in out["hint"]
    respx.get(f"{F}/printers/p1/canvas").mock(
        return_value=httpx.Response(409, json={"error": "canvas_unsupported"}))
    out = await srv.get_canvas()
    assert out["error"] == "canvas_unsupported" and "no multi-filament" in out["message"]


@respx.mock
async def test_get_canvas_on_a_build_without_canvas(monkeypatch):
    _env(monkeypatch)
    listing(printer())
    respx.get(f"{F}/printers/p1/canvas").mock(return_value=httpx.Response(404, json={"error": "not_found"}))
    out = await srv.get_canvas()
    assert "'canvas'" in out["error"]


@respx.mock
async def test_get_canvas_scrubs_a_secret_in_the_tray_data(monkeypatch):
    _env(monkeypatch)
    listing(printer())
    leaky = json.loads(json.dumps(CANVAS))
    leaky["units"][0]["trays"][0]["serialNumber"] = "SECRET"
    leaky["units"][0]["serial_number"] = "SECRET"
    respx.get(f"{F}/printers/p1/canvas").mock(return_value=httpx.Response(200, json=leaky))
    assert "SECRET" not in json.dumps(await srv.get_canvas())


# --- sync_canvas_to_project -----------------------------------------------------------------------


SYNCED = {"synced": 2,
          "slots": [{"slot": 1, "preset": "Elegoo PLA @ECC2", "type": "PLA", "color": "#ffffff", "tray": "A1"},
                    {"slot": 2, "preset": "Elegoo PETG @ECC2", "type": "PETG", "color": "#ff0000",
                     "tray": "A3"}],
          "unknown": [{"tray": "A2", "type": None, "reason": "empty"}]}


@respx.mock
async def test_sync_sends_only_what_was_asked_and_summarises(monkeypatch):
    _env(monkeypatch)
    listing(printer())
    route = respx.post(f"{F}/printers/p1/canvas/sync").mock(return_value=httpx.Response(200, json=SYNCED))
    out = await srv.sync_canvas_to_project()
    assert json.loads(route.calls.last.request.content) == {}
    assert out["synced"] == 2 and out["slots"][1]["tray"] == "A3"
    assert "slot 1 Elegoo PLA @ECC2 #ffffff (from A1)" in out["summary"]
    assert "A2 (empty)" in out["summary"] and "re-slice" in out["summary"]
    await srv.sync_canvas_to_project(mode="color_only", discard_changes=True)
    assert json.loads(route.calls.last.request.content) == {"mode": "color_only", "discard_changes": True}


@respx.mock
async def test_sync_refused_for_unsaved_filament_edits_keeps_the_slicer_hint(monkeypatch):
    _env(monkeypatch)
    listing(printer())
    respx.post(f"{F}/printers/p1/canvas/sync").mock(return_value=httpx.Response(409, json={
        "error": "unsaved_filament_changes", "preset": "My PLA", "hint": "slicer hint"}))
    out = await srv.sync_canvas_to_project()
    assert out["error"] == "unsaved_filament_changes" and out["preset"] == "My PLA"
    assert out["hint"] == "slicer hint" and "Nothing was changed" in out["message"]


@respx.mock
async def test_sync_with_no_compatible_tray(monkeypatch):
    _env(monkeypatch)
    listing(printer())
    respx.post(f"{F}/printers/p1/canvas/sync").mock(return_value=httpx.Response(409, json={
        "error": "no_compatible_filaments", "unknown": [{"tray": "A1", "reason": "not_matched"}]}))
    out = await srv.sync_canvas_to_project()
    assert out["error"] == "no_compatible_filaments" and out["unknown"][0]["tray"] == "A1"


# --- preview_filament_mapping -----------------------------------------------------------------------


@respx.mock
async def test_mapping_table_and_problems(monkeypatch):
    _env(monkeypatch)
    listing(printer())
    route = respx.get(f"{F}/printers/p1/canvas/mapping").mock(return_value=httpx.Response(200, json={
        "plate": 1, "sliced": True, "ok": False, "filaments": [
            {"slot": 1, "preset": "Elegoo PLA @ECC2", "type": "PLA", "color": "#ffffff", "weight_g": 12.4,
             "tray": {"unit": "0", "tray": "A1", "type": "PLA", "color": "#ffffff"}, "issues": []},
            {"slot": 3, "preset": "Elegoo PETG @ECC2", "type": "PETG", "color": "#00ff00", "weight_g": 3.1,
             "tray": None, "issues": ["no_tray"]}]}))
    out = await srv.preview_filament_mapping(plate=1)
    assert route.calls.last.request.url.params["plate"] == "1"
    lines = out["table_markdown"].splitlines()
    assert lines[2] == "| 1 | Elegoo PLA @ECC2 (PLA) | #ffffff | A1 PLA #ffffff | ok |"
    assert lines[3] == "| 3 | Elegoo PETG @ECC2 (PETG) | #00ff00 | none | no_tray |"
    assert out["summary"].startswith("Plate 1: slot 3: no ready tray holds this filament")


@respx.mock
async def test_mapping_all_ok_on_an_unsliced_plate(monkeypatch):
    _env(monkeypatch)
    listing(printer())
    route = respx.get(f"{F}/printers/p1/canvas/mapping").mock(return_value=httpx.Response(200, json={
        "plate": 0, "sliced": False, "ok": True, "filaments": [
            {"slot": 1, "preset": "Elegoo PLA @ECC2", "type": "PLA", "color": "#ffffff", "weight_g": None,
             "tray": {"unit": "0", "tray": "A1", "type": "PLA", "color": "#ffffff"}, "issues": []}]}))
    out = await srv.preview_filament_mapping()
    assert "plate" not in route.calls.last.request.url.params
    assert out["summary"].startswith("All 1 filament(s) of plate 0 have a ready matching tray.")
    assert "not sliced yet" in out["summary"]


@respx.mock
async def test_mapping_with_a_disconnected_canvas(monkeypatch):
    _env(monkeypatch)
    listing(printer())
    respx.get(f"{F}/printers/p1/canvas/mapping").mock(
        return_value=httpx.Response(409, json={"error": "canvas_disconnected"}))
    out = await srv.preview_filament_mapping()
    assert out["error"] == "canvas_disconnected" and out["hint"]


@respx.mock
async def test_no_printers_at_all(monkeypatch):
    _env(monkeypatch)
    listing(selected=None)
    out = await srv.preview_filament_mapping()
    assert out["error"] == "not_configured" and "Device tab" in out["hint"]
