"""The printer tools on an Elegoo printer: read through ElegooSlicer MCP's /printers routes."""
import json

import httpx
import pytest
import respx

import orcaslicer_mcp.server as srv
from orcaslicer_mcp.errors import error_from_status
from orcaslicer_mcp.guard import is_printer_secret, scrub_printer_secrets
from orcaslicer_mcp.printer import elegoo
from orcaslicer_mcp.printer import wait as w
from orcaslicer_mcp.printer.errors import PrinterError
from orcaslicer_mcp.printer.target import PrinterTarget

F = "http://x:13130/api/v1"
EL = PrinterTarget(url="http://192.168.1.50", source="profile", kind="elegoo", printer_id="p1", name="CC2")
SECRETS = ("password", "token", "accessCode", "pinCode", "extraInfo", "username", "serialNumber",
           "mainboardId", "webUrl")


def _env(m):
    m.setenv("ORCA_API_TOKEN", "tok"); m.setenv("ORCA_API_URL", "http://x:13130")


def printer(status="idle", connected=True, task=None, **extra):
    p = {"id": "p1", "name": "CC2", "model": "Elegoo Centauri Carbon 2", "vendor": "Elegoo",
         "host": "192.168.1.50", "port": None, "network": "lan", "firmware": "1.0.2",
         "connected": connected, "status": status, "status_code": 0, "task": task,
         "supports": {"canvas": True, "timelapse": True, "auto_leveling": True,
                      "bed_switching": True, "auto_refill": True, "filament_mapping": True}}
    p.update(extra)
    return p


TASK = {"name": None, "file": "helmet.gcode", "progress": 42, "current_s": 3120, "total_s": 7400,
        "estimated_s": 4280, "begin_ts": None}


def slicer_routes(host_type="elegoolink", print_host="", capabilities=("printers", "canvas"),
                  printers=None, status=None):
    respx.get(f"{F}/status").mock(return_value=httpx.Response(200, json={
        "presets": {"printer": "Elegoo Centauri Carbon 2 0.4 nozzle"},
        "capabilities": ["status", *capabilities]}))
    respx.post(f"{F}/preset/config").mock(return_value=httpx.Response(200, json={
        "name": "Elegoo Centauri Carbon 2 0.4 nozzle",
        "config": {"host_type": host_type, "print_host": print_host,
                   "printer_model": "Elegoo Centauri Carbon 2"}}))
    listed = [printer()] if printers is None else printers
    respx.get(f"{F}/printers").mock(return_value=httpx.Response(200, json={
        "printers": listed, "selected_id": listed[0]["id"] if listed else None}))
    respx.get(f"{F}/printers/p1/status").mock(return_value=httpx.Response(
        200, json=status or {**printer("printing", task=TASK), "exceptions": [], "age_s": 1}))


# --- get_printer_status --------------------------------------------------------------------------


@respx.mock
async def test_status_of_an_elegoolink_profile_comes_from_the_slicer(monkeypatch):
    _env(monkeypatch)
    slicer_routes()
    out = await srv.get_printer_status()
    assert out["state"] == "printing" and out["connected"] is True
    assert out["headline"].startswith("Printing helmet: 42%, about 1 h 11 min left.")
    assert out["job"]["file"] == "helmet.gcode" and out["job"]["elapsed_s"] == 3120
    assert out["job"]["remaining_s"] == 4280 and out["job"]["remaining_basis"] == "printer_estimate"
    assert out["temps"] == {"nozzle": None, "bed": None}
    assert out["printer"]["kind"] == "elegoo" and out["printer"]["printer_id"] == "p1"
    assert out["printer"]["name"] == "CC2" and out["printer"]["url"] == "http://192.168.1.50"
    assert out["elegoo"]["model"] == "Elegoo Centauri Carbon 2"
    # nothing was sent to the printer itself: only the slicer was asked
    assert all(c.request.url.host == "x" for c in respx.calls)


@respx.mock
async def test_an_elegoolink_profile_on_a_build_without_printers_says_what_is_needed(monkeypatch):
    _env(monkeypatch)
    slicer_routes(capabilities=())
    out = await srv.get_printer_status()
    assert out["error"] == "unsupported_connection" and "'printers'" in out["hint"]


@respx.mock
async def test_a_profile_without_address_uses_the_slicer_printers_when_listed(monkeypatch):
    _env(monkeypatch)
    slicer_routes(host_type="octoprint")
    out = await srv.get_printer_status()
    assert out["state"] == "printing" and out["printer"]["kind"] == "elegoo"


@respx.mock
async def test_no_address_and_no_elegoo_printer_explains_both_ways(monkeypatch):
    _env(monkeypatch)
    slicer_routes(host_type="octoprint", printers=[])
    out = await srv.get_printer_status()
    assert out["error"] == "not_configured"
    assert "Device tab" in out["hint"] and "ORCA_PRINTER_URL" in out["hint"]


@respx.mock
async def test_an_elegoolink_profile_without_printers_points_at_the_device_tab(monkeypatch):
    _env(monkeypatch)
    slicer_routes(printers=[])
    out = await srv.get_printer_status()
    assert out["error"] == "not_configured" and "Device tab" in out["hint"]


@respx.mock
async def test_a_klipper_profile_keeps_moonraker_on_a_build_with_printers(monkeypatch):
    _env(monkeypatch)
    slicer_routes(host_type="moonraker", print_host="http://192.0.2.10")
    respx.get("http://192.0.2.10/server/info").mock(return_value=httpx.Response(200, json={
        "result": {"klippy_state": "ready", "klippy_connected": True, "warnings": []}}))
    respx.get(url__startswith="http://192.0.2.10/printer/objects/query").mock(return_value=httpx.Response(
        200, json={"result": {"status": {"webhooks": {"state": "ready"},
                                         "print_stats": {"state": "standby"}}}}))
    respx.get(url__startswith="http://192.0.2.10/server/gcode_store").mock(
        return_value=httpx.Response(200, json={"result": {"gcode_store": []}}))
    out = await srv.get_printer_status()
    assert out["printer"]["kind"] == "klipper" and out["state"] == "idle"
    assert not any(c.request.url.path.endswith("/printers") for c in respx.calls)


@respx.mock
async def test_the_override_wins_over_an_elegoolink_profile(monkeypatch):
    _env(monkeypatch)
    monkeypatch.setenv("ORCA_PRINTER_URL", "http://192.0.2.10")
    status = respx.get(f"{F}/status").mock(return_value=httpx.Response(200, json={}))
    respx.get("http://192.0.2.10/server/info").mock(return_value=httpx.Response(200, json={
        "result": {"klippy_state": "ready", "klippy_connected": True, "warnings": []}}))
    respx.get(url__startswith="http://192.0.2.10/printer/objects/query").mock(return_value=httpx.Response(
        200, json={"result": {"status": {"webhooks": {"state": "ready"}}}}))
    respx.get(url__startswith="http://192.0.2.10/server/gcode_store").mock(
        return_value=httpx.Response(200, json={"result": {"gcode_store": []}}))
    out = await srv.get_printer_status()
    assert out["printer"]["source"] == "override" and not status.called


def test_busy_offline_and_exception_codes():
    busy = elegoo.elegoo_snapshot({**printer("auto_leveling"), "exceptions": [{"code": "803"}]},
                                  EL.public())
    assert busy["state"] == "busy" and busy["activity"] == "auto leveling"
    assert busy["headline"].startswith("Busy: auto leveling.")
    assert busy["problems"][0]["severity"] == "warning" and "803" in busy["problems"][0]["message"]
    off = elegoo.elegoo_snapshot(printer("idle", connected=False), EL.public())
    assert off["state"] == "offline" and off["problems"][0]["severity"] == "fatal"
    assert off["headline"].startswith("The printer is offline.")
    auth = elegoo.elegoo_snapshot(printer("auth_error", connected=False), EL.public())
    assert auth["state"] == "offline" and "access code" in auth["problems"][0]["message"]
    done = elegoo.elegoo_snapshot(printer("print_completed"), EL.public())
    assert done["state"] == "finished" and done["job"]["state"] == "complete"
    assert elegoo.elegoo_snapshot(printer("preheating"), EL.public())["state"] == "heating"
    assert elegoo.elegoo_snapshot(printer("canceled"), EL.public())["job"]["state"] == "cancelled"


@respx.mock
async def test_a_printer_timeout_is_reported_as_not_reachable(monkeypatch):
    _env(monkeypatch)
    slicer_routes()
    respx.get(f"{F}/printers/p1/status").mock(
        return_value=httpx.Response(504, json={"error": "printer_timeout"}))
    out = await srv.get_printer_status()
    assert out["error"] == "not_reachable" and "printer_timeout" in out["message"]


# --- picking a printer ----------------------------------------------------------------------------


def test_pick_printer_by_selection_id_and_name():
    a, b = printer(), {**printer(), "id": "p2", "name": "Garage"}
    listing = {"printers": [a, b], "selected_id": "p2"}
    assert elegoo.pick_printer(listing)["id"] == "p2"
    assert elegoo.pick_printer(listing, "p1")["id"] == "p1"
    assert elegoo.pick_printer(listing, "garage")["id"] == "p2"
    with pytest.raises(PrinterError) as e:
        elegoo.pick_printer(listing, "Kitchen")
    assert e.value.code == "not_configured" and "Garage (p2)" in e.value.hint
    with pytest.raises(PrinterError) as e:
        elegoo.pick_printer({"printers": [a, {**a, "id": "p3"}]}, "CC2")
    assert "Several" in e.value.message


def test_host_url_drops_credentials_ports_and_paths():
    assert elegoo.host_url("192.168.1.50") == "http://192.168.1.50"
    assert elegoo.host_url("http://user:pw@192.168.1.50:80/x") == "http://192.168.1.50"
    assert elegoo.host_url("") == "" and elegoo.host_url(None) == ""


# --- wait_for_printer ------------------------------------------------------------------------------


def _snap(status, task=None):
    return elegoo.elegoo_snapshot(printer(status, task=task), EL.public())


async def _run(monkeypatch, snaps, until, timeout_s=300):
    snaps = list(snaps)
    t = {"now": 0.0}

    async def take(target, client):
        return snaps.pop(0) if len(snaps) > 1 else snaps[0]

    async def sleep(s):
        t["now"] += s

    monkeypatch.setattr(w._snapshot, "take_snapshot", take)
    return await w.run_wait(EL, None, until, timeout_s, None, poll_s=5.0, sleep=sleep,
                            clock=lambda: t["now"])


async def test_printing_rides_out_the_steps_before_the_first_layer(monkeypatch):
    out = await _run(monkeypatch, [_snap("file_transferring"), _snap("preheating"), _snap("homing"),
                                   _snap("auto_leveling"), _snap("printing", TASK)], "printing")
    assert out["met"] is True and out["stopped_early"] is None and out["waited_s"] == 20


async def test_finished_after_a_print_completes(monkeypatch):
    out = await _run(monkeypatch, [_snap("printing", TASK), _snap("filament_operating"),
                                   _snap("printing", TASK), _snap("print_completed")], "finished")
    assert out["met"] is True and out["stopped_early"] is None


async def test_finished_when_the_printer_goes_straight_back_to_idle(monkeypatch):
    out = await _run(monkeypatch, [_snap("printing", TASK), _snap("idle")], "finished")
    assert out["met"] is True and out["stopped_early"] is None
    out = await _run(monkeypatch, [_snap("idle")], "finished")
    assert out["met"] is False and out["note"] == "nothing is printing"
    out = await _run(monkeypatch, [_snap("printing", TASK), _snap("error")], "finished")
    assert out["met"] is True and "error" in out["stopped_early"]


async def test_a_moment_offline_does_not_end_the_wait(monkeypatch):
    def offline():
        return elegoo.elegoo_snapshot(printer("printing", connected=False), EL.public())
    # One or two disconnected polls in a row: a Wi-Fi blip, the wait carries on.
    out = await _run(monkeypatch, [_snap("printing", TASK), offline(), offline(),
                                   _snap("printing", TASK), _snap("print_completed")], "finished")
    assert out["met"] is True and out["stopped_early"] is None
    # Three in a row: the printer is gone, and the reply says so.
    out = await _run(monkeypatch, [_snap("printing", TASK), offline(), offline(), offline(),
                                   _snap("print_completed")], "finished")
    assert out["met"] is False and "isn't connected" in out["stopped_early"]
    assert out["status"]["state"] == "offline" and out["waited_s"] == 15


async def test_a_cancelled_print_ends_the_wait_for_printing(monkeypatch):
    out = await _run(monkeypatch, [_snap("preheating"), _snap("canceled")], "printing")
    assert out["met"] is False and out["stopped_early"] == "The job ended before extrusion started."


@pytest.mark.parametrize("until", ["heated", "first_layer_done"])
async def test_waits_that_need_temperatures_or_layers_are_refused(monkeypatch, until):
    with pytest.raises(PrinterError) as e:
        await _run(monkeypatch, [_snap("printing", TASK)], until)
    assert e.value.code == "unsupported_for_connection" and "'printing'" in e.value.hint


@respx.mock
async def test_wait_for_printer_tool_reads_through_the_slicer(monkeypatch):
    _env(monkeypatch)
    slicer_routes()
    out = await srv.wait_for_printer(until="printing", timeout_s=5)
    assert out["met"] is True and out["status"]["printer"]["printer_id"] == "p1"


# --- list_print_history ---------------------------------------------------------------------------


@respx.mock
async def test_history_of_a_lan_printer_is_unsupported_with_the_reason(monkeypatch):
    _env(monkeypatch)
    slicer_routes()
    route = respx.get(f"{F}/printers/p1/tasks").mock(
        return_value=httpx.Response(501, json={"error": "unsupported_on_lan"}))
    out = await srv.list_print_history()
    assert out["supported"] is False and "cloud" in out["reason"] and route.called
    assert out["printer"]["printer_id"] == "p1"


@respx.mock
async def test_history_of_a_cloud_printer_lists_its_jobs(monkeypatch):
    _env(monkeypatch)
    slicer_routes()
    route = respx.get(f"{F}/printers/p1/tasks").mock(return_value=httpx.Response(200, json={
        "items": [{"id": "t1", "name": "helmet", "begin_ts": 1760000000, "end_ts": 1760003600,
                   "status_code": 2}], "total": 1}))
    out = await srv.list_print_history(limit=5)
    assert route.calls.last.request.url.params["size"] == "5"
    assert out["total"] == 1 and out["jobs"][0]["duration_s"] == 3600
    assert out["jobs"][0]["name"] == "helmet" and out["jobs"][0]["status_code"] == 2
    assert out["jobs"][0]["started_at"]


# --- check_printer_match --------------------------------------------------------------------------


@respx.mock
async def test_match_compares_model_and_canvas(monkeypatch):
    _env(monkeypatch)
    slicer_routes()
    respx.get(f"{F}/config").mock(return_value=httpx.Response(200, json={"config": {
        "printer_model": "Elegoo Centauri Carbon", "support_mms": "1"}}))
    out = await srv.check_printer_match()
    checks = {c["check"]: c for c in out["checks"]}
    assert checks["model"]["status"] == "warn" and checks["canvas"]["status"] == "ok"
    assert out["headline"].startswith("1 warning: model.") and "only the model" in out["note"]


def test_canvas_check_both_ways():
    assert elegoo.check_canvas({"support_mms": "1"},
                               {"supports": {"canvas": False}})["status"] == "warn"
    assert elegoo.check_canvas({"support_mms": "0"},
                               {"supports": {"canvas": True}})["status"] == "warn"
    assert elegoo.check_canvas({}, {"supports": {"canvas": True}})["status"] == "unknown"


# --- secrets never reach the model (defence in depth) ---------------------------------------------


def test_secret_names_in_every_spelling():
    for key in (*SECRETS, "access_code", "PIN_CODE", "serial-number", "accessToken", "user_password",
                "apiSecret", "web_url"):
        assert is_printer_secret(key), key
    for key in ("id", "name", "host", "port", "status", "task", "model", "upload_id", "plan_id"):
        assert not is_printer_secret(key), key


def test_scrub_removes_secrets_at_any_depth():
    leaky = {"id": "p1", "accessCode": "123456", "task": {"file": "a.gcode", "token": "t"},
             "list": [{"pinCode": "1", "ok": 1}], "extraInfo": "{\"password\": \"x\"}"}
    assert scrub_printer_secrets(leaky) == {"id": "p1", "task": {"file": "a.gcode"},
                                            "list": [{"ok": 1}]}


@respx.mock
async def test_a_leaking_slicer_reply_is_scrubbed_before_the_model_sees_it(monkeypatch):
    _env(monkeypatch)
    leaky = {**printer("printing", task={**TASK, "token": "tok-SECRET"}), "exceptions": [],
             **{k: f"{k}-SECRET" for k in SECRETS}}
    slicer_routes(printers=[leaky], status=leaky)
    out = await srv.get_printer_status()
    assert out["state"] == "printing"
    assert "SECRET" not in json.dumps(out)


@respx.mock
async def test_printer_error_bodies_are_scrubbed_too(monkeypatch):
    _env(monkeypatch)
    respx.get(f"{F}/printers").mock(return_value=httpx.Response(
        409, json={"error": "printer_offline", "accessCode": "123456", "detail": "x"}))
    async with srv._client() as c:
        with pytest.raises(Exception) as e:
            await c.get_printers()
    assert e.value.body == {"error": "printer_offline", "detail": "x"}


def test_error_bodies_keep_their_details():
    e = error_from_status(409, {"error": "printer_offline", "detail": "x"})
    assert e.body["detail"] == "x"
