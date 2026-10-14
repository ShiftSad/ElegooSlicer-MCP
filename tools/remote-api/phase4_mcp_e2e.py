"""MCP end-to-end check of the printer, CANVAS, print and camera tools: MCP stdio -> ElegooSlicer MCP
(TEST --datadir, print control allowed) -> the fake CC2 (tools/remote-api/fakecc2). No real printer.

Run with the MCP server's own venv (it has the `mcp` package):
  mcp\\.venv\\Scripts\\python.exe tools\\remote-api\\phase4_mcp_e2e.py <token> <model.stl>
"""
import asyncio
import json
import os
import sys
import time
import urllib.request

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

TOKEN, MODEL = sys.argv[1], sys.argv[2]
SERVER = os.path.join(os.path.dirname(sys.executable), "orcaslicer-mcp.exe")
FAKE = "http://127.0.0.1:18080/state"
SECRETS = ["FAKE42", "FAKECC2SN0001", "accessCode", "access_code", "pinCode", "password", "extraInfo"]
failures = []


def fake(body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(FAKE, data=data, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def starts():
    return [q for q in fake().get("requests", []) if q.get("method") == 1020]


def body_of(res):
    structured = getattr(res, "structured_content", None) or getattr(res, "structuredContent", None)
    if isinstance(structured, dict):
        return structured.get("result", structured)
    texts = [c.text for c in res.content if c.type == "text"]
    try:
        return json.loads(texts[0]) if texts else {}
    except ValueError:
        return {"text": texts[0]}


class Asker:
    def __init__(self, action, confirm=True):
        self.action, self.confirm, self.questions = action, confirm, []

    async def __call__(self, context, params):
        self.questions.append(params.message)
        if self.action == "accept":
            return types.ElicitResult(action="accept", content={"confirm": self.confirm})
        return types.ElicitResult(action=self.action)


def check(name, cond, detail=""):
    if not cond:
        failures.append(name)
    print(("PASS  " if cond else "FAIL  ") + name + (f"  | {detail[:400]}" if detail else ""), flush=True)


async def with_session(asker, body):
    env = dict(os.environ, ORCA_API_TOKEN=TOKEN, ORCA_API_URL="http://127.0.0.1:13130")
    async with stdio_client(StdioServerParameters(command=SERVER, args=[], env=env)) as (read, write):
        kwargs = {"elicitation_callback": asker} if asker else {}
        async with ClientSession(read, write, **kwargs) as s:
            await s.initialize()

            async def run(name, args=None):
                res = await s.call_tool(name, args or {})
                text = json.dumps([getattr(c, "text", c.type) for c in res.content], ensure_ascii=False)
                leaked = [x for x in SECRETS if x in text]
                if leaked:
                    failures.append(f"secret in {name}")
                    print(f"FAIL  secret {leaked} in {name}")
                return res, body_of(res)

            await body(s, run)


async def main_session(s, run):
    names = {t.name for t in (await s.list_tools()).tools}
    want = ["list_printers", "get_canvas", "sync_canvas_to_project", "preview_filament_mapping", "prepare_print",
            "start_print", "get_upload_status", "cancel_upload", "pause_print", "resume_print", "stop_print",
            "get_camera_snapshot", "list_filament_slots", "set_filament_slot", "set_filament_count", "split_object",
            "undo", "redo", "add_layer_change", "list_layer_changes", "remove_layer_change", "run_calibration"]
    check("all new tools listed", all(n in names for n in want), str([n for n in want if n not in names]))

    _, r = await run("list_printers")
    ps = r.get("printers", [])
    check("list_printers", len(ps) == 1 and ps[0].get("connected") and r.get("summary"), json.dumps(r)[:300])
    pid = ps[0]["id"] if ps else None
    _, r = await run("get_canvas")
    trays = [t["id"] for u in r.get("units", []) for t in u.get("trays", [])]
    check("get_canvas", trays == ["A1", "A2", "A3", "A4"] and r.get("summary"), json.dumps(r)[:300])
    for o in (await run("list_objects"))[1].get("objects", []):
        await run("delete_object", {"object_id": o["id"]})
    _, r = await run("load_model", {"path": MODEL})
    check("load_model", not r.get("error"), json.dumps(r)[:200])
    _, r = await run("sync_canvas_to_project", {})
    check("sync_canvas_to_project", r.get("synced") == 3, json.dumps(r)[:300])
    _, r = await run("list_filament_slots")
    check("list_filament_slots", r.get("count") == 3, json.dumps(r)[:200])
    _, r = await run("slice_and_wait", {"timeout": 50})
    check("slice_and_wait", r.get("state") == "done" or r.get("status") == "done" or r.get("done") is True, json.dumps(r)[:200])
    _, r = await run("preview_filament_mapping", {})
    check("preview_filament_mapping", r.get("ok") is True and r.get("table_markdown"), json.dumps(r)[:300])
    _, r = await run("get_printer_status")
    check("get_printer_status (Elegoo backend)", r.get("state") in ("idle", "finished", "ready") or "idle" in json.dumps(r).lower(),
          json.dumps(r)[:300])

    n0 = len(starts())
    _, plan = await run("prepare_print", {"leveling": False})
    check("prepare_print", plan.get("plan_id") and plan.get("summary"), json.dumps(plan)[:300])
    _, r = await run("start_print", {"plan_id": plan.get("plan_id", "")})
    check("start_print without confirmation is refused", r.get("error") and len(starts()) == n0, json.dumps(r)[:300])
    _, r = await run("start_print", {"plan_id": plan.get("plan_id", ""), "user_confirmed": True})
    if r.get("error") in ("unknown_plan", "plan_expired"):
        _, plan = await run("prepare_print", {"leveling": False})
        _, r = await run("start_print", {"plan_id": plan.get("plan_id", ""), "user_confirmed": True})
    check("start_print with user_confirmed", r.get("upload_id"), json.dumps(r)[:300])
    uid = r.get("upload_id", "x")
    end = time.time() + 60
    u = {}
    while time.time() < end:
        _, u = await run("get_upload_status", {"upload_id": uid})
        if u.get("status") in ("success", "failed", "cancelled"):
            break
        await asyncio.sleep(1)
    check("get_upload_status -> success", u.get("status") == "success", json.dumps(u)[:300])
    check("the fake got exactly one start", len(starts()) == n0 + 1)
    _, r = await run("wait_for_printer", {"until": "printing", "timeout_s": 40})
    check("wait_for_printer(printing)", r.get("met") is True, json.dumps(r)[:300])
    res, _ = await run("get_camera_snapshot", {})
    imgs = [c for c in res.content if c.type == "image"]
    check("get_camera_snapshot returns a JPEG", imgs and getattr(imgs[0], "mime_type", getattr(imgs[0], "mimeType", None)) == "image/jpeg", str([c.type for c in res.content]))
    _, r = await run("pause_print", {})
    check("pause_print", r.get("ok") is True, json.dumps(r)[:300])
    await asyncio.sleep(3)
    _, r = await run("resume_print", {})
    check("resume_print -> resume_unsupported", r.get("error") == "resume_unsupported" and r.get("hint"), json.dumps(r)[:300])
    _, r = await run("stop_print", {})
    check("stop_print without confirmation is refused", r.get("error"), json.dumps(r)[:300])
    _, r = await run("stop_print", {"user_confirmed": True})
    check("stop_print with user_confirmed", r.get("ok") is True, json.dumps(r)[:300])
    end = time.time() + 30
    while time.time() < end and fake().get("phase") != "idle":
        await asyncio.sleep(1)
    check("the fake is idle again", fake().get("phase") == "idle", str(fake().get("phase")))


async def decline_session(s, run):
    n0 = len(starts())
    _, plan = await run("prepare_print", {"leveling": False})
    _, r = await run("start_print", {"plan_id": plan.get("plan_id", ""), "user_confirmed": True})
    check("a declined prompt beats user_confirmed=True", r.get("error") and len(starts()) == n0
          and DECLINE.questions, json.dumps(r)[:300])


async def accept_session(s, run):
    n0 = len(starts())
    _, plan = await run("prepare_print", {"leveling": False})
    _, r = await run("start_print", {"plan_id": plan.get("plan_id", "")})
    check("an accepted prompt starts the print", r.get("upload_id") and ACCEPT.questions, json.dumps(r)[:300])
    end = time.time() + 60
    while time.time() < end and len(starts()) == n0:
        await asyncio.sleep(1)
    end = time.time() + 30
    while time.time() < end and fake().get("phase") not in ("printing",):
        await asyncio.sleep(1)
    await asyncio.sleep(4)  # the slicer's cache follows the printer's pushes
    _, r = await run("stop_print", {})
    check("stop_print after an accepted prompt", r.get("ok") is True and len(ACCEPT.questions) == 2, json.dumps(r)[:300])
    end = time.time() + 30
    while time.time() < end and fake().get("phase") != "idle":
        await asyncio.sleep(1)


DECLINE = Asker("decline")
ACCEPT = Asker("accept", True)


async def main():
    fake({"reset": True, "clear_log": True})
    await asyncio.sleep(12)  # the slicer reconnects after the reset
    await with_session(None, main_session)
    await asyncio.sleep(3)
    await with_session(DECLINE, decline_session)
    await with_session(ACCEPT, accept_session)
    print("RESULT:", "ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}")
    sys.exit(1 if failures else 0)


asyncio.run(main())
