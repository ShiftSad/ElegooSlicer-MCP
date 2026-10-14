"""Two-step printing on ElegooSlicer MCP: prepare_print, start_print (the user's confirmation),
get_upload_status, cancel_upload, pause_print, resume_print, stop_print (mock slicer API)."""
import json

import httpx
import mcp.types as types
import pytest
import respx
from mcp.client.client import Client

import orcaslicer_mcp.server as srv

F = "http://x:13130/api/v1"

SUMMARY = {"printer": "CC2", "plate": 0, "file_name": "helmet.gcode", "estimated_time_s": 7380,
           "filament_g": 45.24, "layers": 230, "bed_side": "A", "leveling": True,
           "timelapse": False, "auto_refill": False,
           "mapping": [{"slot": 1, "unit": "0", "tray": "A1"}, {"slot": 2, "unit": "0", "tray": "A3"}],
           "warnings": ["The prime tower is close to the edge"]}
SENTENCE = ("Print helmet.gcode (plate 1) on CC2: about 2 h 3 min, 45.2 g of filament, 230 layers; "
            "bed side A, leveling on, timelapse off, auto refill off; filament 1 from A1, filament 2 "
            "from A3. Warnings: The prime tower is close to the edge.")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ORCA_API_TOKEN", "tok")
    monkeypatch.setenv("ORCA_API_URL", "http://x:13130")
    srv._PRINT_PLANS.clear()
    yield
    srv._PRINT_PLANS.clear()


def printer(status="idle", task=None, connected=True):
    return {"id": "p1", "name": "CC2", "model": "Elegoo Centauri Carbon 2", "host": "192.168.1.50",
            "network": "lan", "connected": connected, "status": status, "task": task}


def listing(*printers):
    respx.get(f"{F}/printers").mock(return_value=httpx.Response(
        200, json={"printers": list(printers) or [printer()], "selected_id": "p1"}))


def prepare_route(plan_id="plan-1"):
    return respx.post(f"{F}/printers/p1/print/prepare").mock(return_value=httpx.Response(
        200, json={"plan_id": plan_id, "expires_in_s": 300, "summary": SUMMARY}))


def print_route():
    return respx.post(f"{F}/printers/p1/print").mock(
        return_value=httpx.Response(202, json={"upload_id": "u-1"}))


def _sent(route):
    return json.loads(route.calls.last.request.content)


# --- prepare_print -----------------------------------------------------------------------------------


@respx.mock
async def test_prepare_returns_one_sentence_and_the_plan():
    listing()
    route = prepare_route()
    out = await srv.prepare_print()
    assert _sent(route) == {}
    assert out["plan_id"] == "plan-1" and out["expires_in_s"] == 300
    assert out["summary"] == SENTENCE
    assert out["details"] == SUMMARY and "ask" in out["next"]
    assert srv._PRINT_PLANS["plan-1"]["printer_id"] == "p1"


@respx.mock
async def test_prepare_sends_only_the_options_given():
    listing()
    route = prepare_route()
    await srv.prepare_print(plate=2, bed_side="B", leveling=False, timelapse=True, auto_refill=True,
                            mapping=[{"slot": 1, "unit": "0", "tray": "A2"}])
    assert _sent(route) == {"plate": 2, "bed_side": "B", "leveling": False, "timelapse": True,
                            "auto_refill": True, "mapping": [{"slot": 1, "unit": "0", "tray": "A2"}]}


@respx.mock
async def test_prepare_when_print_control_is_off_says_who_can_turn_it_on():
    listing()
    respx.post(f"{F}/printers/p1/print/prepare").mock(return_value=httpx.Response(403, json={
        "error": "print_control_disabled",
        "hint": "Preferences > Remote API > Allow sending and controlling prints"}))
    out = await srv.prepare_print()
    assert out["error"] == "print_control_disabled"
    assert out["hint"] == "Preferences > Remote API > Allow sending and controlling prints"
    assert "only they can turn it on" in out["message"] and not srv._PRINT_PLANS


@respx.mock
async def test_prepare_refusals_keep_the_slicer_details():
    listing()
    route = respx.post(f"{F}/printers/p1/print/prepare")
    route.mock(return_value=httpx.Response(409, json={"error": "printer_busy", "status": "printing"}))
    out = await srv.prepare_print()
    assert out["error"] == "printer_busy" and out["status"] == "printing" and out["hint"]
    route.mock(return_value=httpx.Response(422, json={
        "error": "mapping_incomplete", "issues": [{"slot": 2, "issue": "no_tray"}]}))
    out = await srv.prepare_print()
    assert out["error"] == "mapping_incomplete" and out["issues"][0]["slot"] == 2
    assert "preview_filament_mapping" in out["hint"]
    route.mock(return_value=httpx.Response(409, json={"error": "not_sliced"}))
    assert (await srv.prepare_print())["error"] == "not_sliced"


@respx.mock
async def test_prepare_on_a_build_without_print_control():
    listing()
    respx.post(f"{F}/printers/p1/print/prepare").mock(
        return_value=httpx.Response(404, json={"error": "not_found"}))
    out = await srv.prepare_print()
    assert "'print_control'" in out["error"]


def test_plan_sentence_from_the_slicer_plan():
    # The plan as RemoteAPIElegoo.cpp builds it: mapping entries are the filaments as
    # preview_filament_mapping shows them, warnings are objects with a code and a message.
    plan = {"printer": "CC2", "printer_id": "p1", "plate": 1, "file_name": "helmet.gcode",
            "estimated_time_s": 3600, "filament_g": 12.0, "layers": 100, "bed_side": None,
            "leveling": False, "timelapse": False, "auto_refill": True,
            "mapping": [{"slot": 1, "preset": "Elegoo PLA @ECC2", "type": "PLA", "color": "#ffffff",
                         "weight_g": 12.0, "tray": {"unit": "0", "tray": "A1", "type": "PLA",
                                                    "color": "#ffffff"}, "issues": []}],
            "warnings": [{"code": "print_completed", "message": "the printer shows its last print as "
                                                               "completed: make sure the bed is clear"}]}
    assert srv._plan_sentence(plan) == (
        "Print helmet.gcode (plate 2) on CC2: about 1 h, 12.0 g of filament, 100 layers; leveling off, "
        "timelapse off, auto refill on; filament 1 (PLA #ffffff) from A1. Warnings: The printer shows "
        "its last print as completed: make sure the bed is clear.")


@respx.mock
async def test_prepare_errors_of_the_slicer():
    listing()
    route = respx.post(f"{F}/printers/p1/print/prepare")
    route.mock(return_value=httpx.Response(409, json={"error": "upload_in_progress", "upload_id": "u-0"}))
    out = await srv.prepare_print()
    assert out["error"] == "upload_in_progress" and out["upload_id"] == "u-0" and "get_upload_status" in out["hint"]
    route.mock(return_value=httpx.Response(422, json={"error": "bad_mapping", "detail": "no tray A9 in the CANVAS"}))
    out = await srv.prepare_print(mapping=[{"slot": 1, "tray": "A9"}])
    assert out["error"] == "bad_mapping" and out["detail"] == "no tray A9 in the CANVAS"
    route.mock(return_value=httpx.Response(422, json={
        "error": "mapping_incomplete", "issues": [{"slot": 2, "issues": ["no_tray"]}], "filaments": [],
        "hint": "load the filaments, sync the CANVAS (POST /printers/{id}/canvas/sync)"}))
    out = await srv.prepare_print()
    # the slicer's hint names HTTP routes; the tool's hint names tools
    assert "preview_filament_mapping" in out["hint"] and "POST" not in out["hint"]


def test_plan_sentence_with_little_information():
    assert srv._plan_sentence({}) == "Print the plate on the printer."
    assert srv._plan_sentence({"printer": {"id": "p1", "name": "CC2"}, "file_name": "a.gcode",
                               "mapping": [{"slot": 1, "tray": None}]}) == \
        "Print a.gcode on CC2; filament 1 from no tray."


# --- start_print without a client prompt: user_confirmed -------------------------------------------


@respx.mock
async def test_start_without_confirmation_is_refused_and_sends_nothing():
    listing()
    prepare_route()
    route = print_route()
    plan = await srv.prepare_print()
    out = await srv.start_print(plan["plan_id"])
    assert out["error"] == "confirmation_required" and out["summary"] == SENTENCE
    assert out["message"] == "Start this print? " + SENTENCE
    assert not route.called and "plan-1" in srv._PRINT_PLANS  # still there to confirm


@respx.mock
async def test_start_with_user_confirmed_sends_the_plan_once():
    listing()
    prepare_route()
    route = print_route()
    plan = await srv.prepare_print()
    out = await srv.start_print(plan["plan_id"], user_confirmed=True)
    assert _sent(route) == {"plan_id": "plan-1"}
    assert out["upload_id"] == "u-1" and out["summary"] == SENTENCE
    assert "get_upload_status" in out["next"]
    again = await srv.start_print(plan["plan_id"], user_confirmed=True)
    assert again["error"] == "unknown_plan" and route.call_count == 1


@respx.mock
async def test_start_an_unknown_or_expired_plan():
    route = print_route()
    assert (await srv.start_print("nope", user_confirmed=True))["error"] == "unknown_plan"
    srv._PRINT_PLANS["old"] = {"printer_id": "p1", "summary": "x", "expires_at": 0.0}
    out = await srv.start_print("old", user_confirmed=True)
    assert out["error"] == "plan_expired" and "prepare_print" in out["hint"]
    assert "old" not in srv._PRINT_PLANS and not route.called


@respx.mock
async def test_start_refused_by_the_slicer():
    srv._PRINT_PLANS["plan-1"] = {"printer_id": "p1", "summary": "x", "expires_at": 1e18}
    respx.post(f"{F}/printers/p1/print").mock(return_value=httpx.Response(403, json={
        "error": "print_control_disabled", "hint": "Preferences > Remote API"}))
    out = await srv.start_print("plan-1", user_confirmed=True)
    assert out["error"] == "print_control_disabled"
    assert out["hint"] == "Preferences > Remote API > Allow sending and controlling prints"
    # the attempt used the plan up: a retry can never start a print twice
    assert "plan-1" not in srv._PRINT_PLANS
    srv._PRINT_PLANS["plan-2"] = {"printer_id": "p1", "summary": "x", "expires_at": 1e18}
    respx.post(f"{F}/printers/p1/print").mock(return_value=httpx.Response(410, json={"error": "plan_expired"}))
    out = await srv.start_print("plan-2", user_confirmed=True)
    assert out["error"] == "plan_expired" and "ask again" in out["hint"]


@respx.mock
async def test_start_that_times_out():
    srv._PRINT_PLANS["plan-1"] = {"printer_id": "p1", "summary": "x", "expires_at": 1e18}
    route = respx.post(f"{F}/printers/p1/print").mock(side_effect=httpx.ReadTimeout("slow"))
    out = await srv.start_print("plan-1", user_confirmed=True)
    # The slicer starts the transfer just before it answers: the print may be on its way.
    assert out["error"] == "start_unconfirmed" and "may have started" in out["message"]
    assert "get_printer_status" in out["hint"] and "plan-1" not in srv._PRINT_PLANS
    assert route.calls.last.request.extensions["timeout"]["read"] == 50.0
    # The slicer's own timeouts come before it sends anything, and the plan is used up.
    for token in ("printer_timeout", "ui_timeout"):
        srv._PRINT_PLANS["plan-2"] = {"printer_id": "p1", "summary": "x", "expires_at": 1e18}
        respx.post(f"{F}/printers/p1/print").mock(return_value=httpx.Response(504, json={"error": token}))
        out = await srv.start_print("plan-2", user_confirmed=True)
        assert out["error"] == token and "nothing was sent" in out["message"]
        assert "prepare_print again" in out["hint"]


# --- start_print with a client that can ask the user (MCP elicitation) -------------------------------


class Asker:
    """An MCP client's elicitation handler: records each question and gives a fixed answer."""

    def __init__(self, action="accept", confirm=True):
        self.action, self.confirm, self.questions = action, confirm, []

    async def __call__(self, context, params):
        self.questions.append(params.message)
        if self.action == "accept":
            return types.ElicitResult(action="accept", content={"confirm": self.confirm})
        return types.ElicitResult(action=self.action)


async def call(name, arguments, mode, asker=None):
    kwargs = {"mode": mode}
    if asker is not None:
        kwargs["elicitation_callback"] = asker
    async with Client(srv.mcp, **kwargs) as client:
        result = await client.call_tool(name, arguments)
    return json.loads(result.content[0].text)


MODES = ["legacy", "auto"]  # a request in the middle of the call (<= 2025-11-25), input-required rounds


@pytest.mark.parametrize("mode", MODES)
@respx.mock
async def test_the_client_asks_the_user_and_a_yes_starts_the_print(mode):
    srv._PRINT_PLANS["plan-1"] = {"printer_id": "p1", "summary": SENTENCE, "expires_at": 1e18}
    route = print_route()
    asker = Asker()
    out = await call("start_print", {"plan_id": "plan-1"}, mode, asker)
    assert asker.questions == ["Start this print? " + SENTENCE]
    assert out["upload_id"] == "u-1" and route.call_count == 1


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("action,confirm,how", [("decline", True, "said no"),
                                                ("cancel", True, "closed the confirmation"),
                                                ("accept", False, "did not tick the confirmation")])
@respx.mock
async def test_anything_but_a_ticked_yes_sends_nothing(mode, action, confirm, how):
    srv._PRINT_PLANS["plan-1"] = {"printer_id": "p1", "summary": SENTENCE, "expires_at": 1e18}
    route = print_route()
    # user_confirmed cannot override the user's own answer in the client's prompt
    out = await call("start_print", {"plan_id": "plan-1", "user_confirmed": True}, mode,
                     Asker(action, confirm))
    assert out["error"] == "not_confirmed" and how in out["message"]
    assert not route.called


@pytest.mark.parametrize("mode", MODES)
@respx.mock
async def test_a_client_that_cannot_ask_needs_user_confirmed(mode):
    srv._PRINT_PLANS["plan-1"] = {"printer_id": "p1", "summary": SENTENCE, "expires_at": 1e18}
    route = print_route()
    out = await call("start_print", {"plan_id": "plan-1"}, mode)
    assert out["error"] == "confirmation_required" and not route.called
    out = await call("start_print", {"plan_id": "plan-1", "user_confirmed": True}, mode)
    assert out["upload_id"] == "u-1"


@respx.mock
async def test_no_question_is_put_for_an_expired_plan():
    srv._PRINT_PLANS["old"] = {"printer_id": "p1", "summary": SENTENCE, "expires_at": 0.0}
    asker = Asker()
    out = await call("start_print", {"plan_id": "old"}, "legacy", asker)
    assert out["error"] == "plan_expired" and asker.questions == []


def test_a_url_only_client_cannot_show_the_form():
    class Ctx:
        def __init__(self, elicitation):
            self.client_capabilities = types.ClientCapabilities(elicitation=elicitation)
    assert srv._can_ask_user(Ctx(types.ElicitationCapability()))
    assert srv._can_ask_user(Ctx(types.ElicitationCapability(form=types.FormElicitationCapability())))
    assert not srv._can_ask_user(Ctx(types.ElicitationCapability(url=types.UrlElicitationCapability())))
    assert not srv._can_ask_user(Ctx(None))
    assert not srv._can_ask_user(None)


# --- uploads ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("reply,text", [
    ({"status": "exporting", "progress": 0, "bytes": [0, 0], "message": ""}, "Preparing the file"),
    ({"status": "uploading", "progress": 40, "bytes": [4, 10], "message": ""}, "Sending the file to the printer: 40%."),
    ({"status": "success", "progress": 100, "bytes": [10, 10], "message": ""}, "starts printing it"),
    ({"status": "failed", "progress": 10, "bytes": [1, 10], "message": "disk full"}, "failed: disk full"),
    ({"status": "cancelled", "progress": 10, "bytes": [1, 10], "message": ""}, "cancelled"),
])
@respx.mock
async def test_upload_status_summaries(reply, text):
    respx.get(f"{F}/uploads/u-1").mock(return_value=httpx.Response(200, json=reply))
    out = await srv.get_upload_status("u-1")
    assert out["status"] == reply["status"] and text in out["summary"]


@respx.mock
async def test_cancel_upload_and_too_late():
    route = respx.delete(f"{F}/uploads/u-1").mock(return_value=httpx.Response(200, json={"ok": True}))
    assert await srv.cancel_upload("u-1") == {"ok": True} and route.called
    respx.delete(f"{F}/uploads/u-1").mock(return_value=httpx.Response(409, json={"error": "too_late"}))
    out = await srv.cancel_upload("u-1")
    assert out["error"] == "too_late" and "stop_print" in out["hint"]


# --- pause / resume ------------------------------------------------------------------------------


@respx.mock
async def test_pause_and_resume():
    listing(printer("printing"))
    pause = respx.post(f"{F}/printers/p1/pause").mock(return_value=httpx.Response(200, json={"ok": True}))
    assert await srv.pause_print() == {"ok": True} and pause.calls.last.request.content == b""
    respx.post(f"{F}/printers/p1/resume").mock(return_value=httpx.Response(501, json={
        "error": "resume_unsupported", "hint": "resume on the printer's screen"}))
    out = await srv.resume_print()
    assert out["error"] == "resume_unsupported" and out["hint"] == "Resume it on the printer's screen."
    respx.post(f"{F}/printers/p1/resume").mock(return_value=httpx.Response(409, json={
        "error": "not_paused", "status": "printing"}))
    out = await srv.resume_print()
    assert out["error"] == "not_paused" and out["status"] == "printing" and "isn't paused" in out["message"]


@respx.mock
async def test_a_command_the_printer_did_not_confirm_is_reported_as_unknown():
    listing(printer("printing"))
    respx.post(f"{F}/printers/p1/pause").mock(return_value=httpx.Response(504, json={"error": "printer_timeout"}))
    out = await srv.pause_print()
    assert out["error"] == "printer_timeout" and "may still have been carried out" in out["message"]
    respx.post(f"{F}/printers/p1/pause").mock(side_effect=httpx.ReadTimeout("slow"))
    out = await srv.pause_print()
    assert out["error"] == "printer_timeout" and "get_printer_status" in out["hint"]


@respx.mock
async def test_pause_with_print_control_off():
    listing(printer("printing"))
    respx.post(f"{F}/printers/p1/pause").mock(return_value=httpx.Response(403, json={
        "error": "print_control_disabled", "hint": "Preferences > Remote API"}))
    out = await srv.pause_print()
    assert out["error"] == "print_control_disabled" and "Do not look for another way" in out["message"]


# --- stop_print ------------------------------------------------------------------------------------


PRINTING = printer("printing", task={"file": "helmet.gcode", "progress": 42})


@respx.mock
async def test_stop_needs_confirmation_and_sends_the_confirm_body():
    listing(PRINTING)
    route = respx.post(f"{F}/printers/p1/stop").mock(return_value=httpx.Response(200, json={"ok": True}))
    out = await srv.stop_print()
    assert out["error"] == "confirmation_required" and not route.called
    assert out["message"] == "Stop the print helmet.gcode on CC2? A stopped print can't be resumed."
    assert await srv.stop_print(user_confirmed=True) == {"ok": True}
    assert _sent(route) == {"confirm": "stop"}


@respx.mock
async def test_stop_when_nothing_prints_asks_nobody():
    listing(printer("idle"))
    route = respx.post(f"{F}/printers/p1/stop")
    asker = Asker()
    out = await call("stop_print", {}, "legacy", asker)
    assert out["error"] == "not_printing" and asker.questions == [] and not route.called
    listing(printer("idle", connected=False))
    assert (await srv.stop_print(user_confirmed=True))["error"] == "printer_offline"


@pytest.mark.parametrize("mode", MODES)
@respx.mock
async def test_stop_asks_the_user_through_the_client(mode):
    listing(PRINTING)
    route = respx.post(f"{F}/printers/p1/stop").mock(return_value=httpx.Response(200, json={"ok": True}))
    asker = Asker()
    assert await call("stop_print", {}, mode, asker) == {"ok": True}
    assert asker.questions == ["Stop the print helmet.gcode on CC2? A stopped print can't be resumed."]
    assert route.call_count == 1
    out = await call("stop_print", {"user_confirmed": True}, mode, Asker("decline"))
    assert out["error"] == "not_confirmed" and route.call_count == 1


BUSY = httpx.Response(504, json={"error": "ui_timeout"})
OK_PRINTING = httpx.Response(200, json={"printers": [PRINTING], "selected_id": "p1"})


@pytest.mark.parametrize("mode,replies", [
    # The question's printer read hits a busy window; the tool's own read works.
    ("legacy", [BUSY, OK_PRINTING, OK_PRINTING]),
    # Round 1 asks and the user says no; round 2's read for the question fails.
    ("auto", [OK_PRINTING, BUSY, OK_PRINTING, OK_PRINTING]),
])
@respx.mock
async def test_stop_never_takes_user_confirmed_in_place_of_the_prompt(mode, replies):
    respx.get(f"{F}/printers").mock(side_effect=replies)
    route = respx.post(f"{F}/printers/p1/stop").mock(return_value=httpx.Response(200, json={"ok": True}))
    out = await call("stop_print", {"user_confirmed": True}, mode, Asker("decline"))
    assert out["error"] == "not_confirmed" and not route.called


@respx.mock
async def test_stop_refuses_when_the_printer_started_while_asking():
    # The question saw an idle printer (no question), the tool sees it printing.
    idle = httpx.Response(200, json={"printers": [printer("idle")], "selected_id": "p1"})
    respx.get(f"{F}/printers").mock(side_effect=[idle, OK_PRINTING])
    route = respx.post(f"{F}/printers/p1/stop")
    asker = Asker()
    out = await call("stop_print", {"user_confirmed": True}, "legacy", asker)
    assert out["error"] == "not_confirmed" and "Call the tool again" in out["hint"]
    assert asker.questions == [] and not route.called


def test_print_tools_annotations():
    tools = srv.mcp._tool_manager._tools
    assert tools["prepare_print"].annotations.read_only_hint is True
    for name in ("start_print", "stop_print", "cancel_upload"):
        ann = tools[name].annotations
        assert ann.read_only_hint is False and ann.destructive_hint is True, name
    for name in ("prepare_print", "start_print", "stop_print", "pause_print", "get_upload_status"):
        assert tools[name].annotations.open_world_hint is True, name
