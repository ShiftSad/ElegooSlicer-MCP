"""End-to-end check of the Elegoo printer, CANVAS and print-control routes against the fake CC2
(tools/remote-api/fakecc2). Nothing here talks to a real printer.

Usage:
  py -3 -I tools/remote-api/phase4_fakecc2_e2e.py off <token> <model.stl>   # print control disabled in Preferences
  py -3 -I tools/remote-api/phase4_fakecc2_e2e.py on  <token> <model.stl>   # remote_api_allow_print_control = true

Needs: the fake running on its default ports (py -3 -I tools/remote-api/fakecc2/fakecc2.py), registered in
the TEST data dir with tools/remote-api/fakecc2/add_fake_printer.py, and ElegooSlicer MCP started with that
--datadir on 127.0.0.1:13130. It deletes every object in that window.
"""
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:13130/api/v1"
FAKE = "http://127.0.0.1:18080/state"
MODE, TOKEN, MODEL = sys.argv[1], sys.argv[2], sys.argv[3]
# What must never appear in a printer reply: the fake's secrets and the SDK's secret keys.
SECRET_VALUES = ["FAKE42", "FAKECC2SN0001", "lan_FAKECC2"]
SECRET_KEYS = ['"accessCode"', '"access_code"', '"password"', '"pinCode"', '"pin_code"', '"extraInfo"',
               '"token"', '"serialNumber"', '"serial_number"', '"sn"', '"mainboardId"', '"mainboard_id"']
failed = []
printer_bodies = []


def call(method, path, body=None, timeout=150, raw=False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("X-Api-Token", TOKEN)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = r.read()
            status = r.status
    except urllib.error.HTTPError as e:
        payload, status = e.read(), e.code
    if raw:
        return status, payload
    text = payload.decode("utf-8", "replace")
    if "/printers" in path or "/uploads" in path:
        printer_bodies.append((method + " " + path, text))
    return status, json.loads(text or "{}")


def fake(body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(FAKE, data=data, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def check(name, cond, detail=""):
    if not cond:
        failed.append(name)
    print(("PASS  " if cond else "FAIL  ") + name + (f"  | {detail}" if detail else ""), flush=True)


def short(r):
    s = json.dumps(r, ensure_ascii=False)
    return s if len(s) < 500 else s[:500] + "..."


def wait_for(what, fn, limit=60, step=1.0):
    end = time.time() + limit
    last = None
    while time.time() < end:
        last = fn()
        if last:
            return last
        time.sleep(step)
    print(f"      (timed out waiting for {what})")
    return last


def slice_current(limit=300):
    s, r = call("POST", "/slice")
    if s == 200 and r.get("already_valid"):
        return "done"
    end = time.time() + limit
    while time.time() < end:
        st = call("GET", "/slice/status")[1].get("state")
        if st in ("done", "error"):
            return st
        time.sleep(1)
    return "timeout"


def printer():
    s, r = call("GET", "/printers")
    ps = r.get("printers", []) if s == 200 else []
    return ps[0] if len(ps) == 1 else None


def status_of(pid):
    return call("GET", f"/printers/{pid}")[1].get("status")


def reset_project():
    for o in call("GET", "/objects")[1].get("objects", []):
        call("DELETE", f"/objects/{o['id']}")
    while call("GET", "/plates")[1]["count"] > 1:
        call("DELETE", f"/plates/{call('GET', '/plates')[1]['count'] - 1}")


# ---------------------------------------------------------------- common: printers + CANVAS (read)
fake({"reset": True, "clear_log": True})
s, st = call("GET", "/status")
caps = st.get("capabilities", [])
for c in ("printers", "canvas", "print_control"):
    check(f"capability {c}", c in caps)
check(f"print_control_enabled is {MODE == 'on'}", st.get("print_control_enabled") is (MODE == "on"),
      str(st.get("print_control_enabled")))

p = wait_for("the fake printer to be listed and connected",
             lambda: (lambda x: x if x and x.get("connected") else None)(printer()), limit=60)
check("GET /printers lists the fake, connected", bool(p), short(p))
if not p:
    print("RESULT: cannot continue without the printer")
    sys.exit(1)
pid = p["id"]
s, r = call("GET", "/printers")
check("printer id is opaque (p + 16 hex)", len(pid) == 17 and pid[0] == "p" and all(c in "0123456789abcdef" for c in pid[1:]), pid)
check("selected_id is the fake", r.get("selected_id") == pid, str(r.get("selected_id")))
check("printer fields", p.get("network") == "lan" and p.get("model", "").endswith("Centauri Carbon 2")
      and p.get("supports", {}).get("canvas") is True and p.get("status") in ("idle", "print_completed"), short(p))
s, r = call("GET", f"/printers/{pid}")
check("GET /printers/{id}", s == 200 and r.get("id") == pid, f"{s}")
s, r = call("GET", f"/printers/{pid}/status")
check("GET /printers/{id}/status has exceptions + age_s", s == 200 and r.get("exceptions") == [] and "age_s" in r,
      f"{s} {short(r)}")
s, r = call("GET", f"/printers/{pid}/status?raw=1")
check("raw=1 on LAN -> 501 unsupported_on_lan", s == 501 and r.get("error") == "unsupported_on_lan", f"{s} {short(r)}")
for what in ("tasks", "files"):
    s, r = call("GET", f"/printers/{pid}/{what}")
    check(f"GET {what} on LAN -> 501 unsupported_on_lan", s == 501 and r.get("error") == "unsupported_on_lan", f"{s}")
s, r = call("GET", "/printers/p0000000000000000")
check("unknown printer -> 404 printer_not_found", s == 404 and r.get("error") == "printer_not_found", f"{s} {short(r)}")

fake({"exceptions": [803]})
time.sleep(2)
call("GET", f"/printers/{pid}/status")  # asks for a fresh status
ex = wait_for("exception 803 in the status", lambda: (lambda r: r if r.get("exceptions") else None)(
    call("GET", f"/printers/{pid}/status")[1]), limit=15)
check("exception codes reach /status", bool(ex) and any(str(e.get("code")) == "803" for e in ex.get("exceptions", [])),
      short(ex))
fake({"exceptions": []})

s, cv = call("GET", f"/printers/{pid}/canvas", timeout=60)
trays = [t for u in cv.get("units", []) for t in u.get("trays", [])]
check("GET canvas: 4 trays A1..A4", s == 200 and [t["id"] for t in trays] == ["A1", "A2", "A3", "A4"], f"{s} {short(cv)}")
if len(trays) == 4:
    check("tray A1 red PLA preloaded, A4 empty",
          trays[0]["color"].lower() == "#ff0000" and trays[0]["type"] == "PLA" and trays[0]["status"] in ("preloaded", "loaded")
          and trays[3]["status"] == "empty" and trays[3]["type"] is None, short(trays))
    check("trays matched to a preset", all(t.get("matched_preset") for t in trays[:3]), str([t.get("matched_preset") for t in trays]))
check("CANVAS: no temperature/humidity/external spool", cv.get("external_spool") is None and
      all(u.get("temperature_c") is None and u.get("humidity") is None for u in cv.get("units", [])))

reset_project()
check("load model", call("POST", "/model", {"path": MODEL})[0] == 200)
s, r = call("POST", f"/printers/{pid}/canvas/sync", {"mode": "teleport"})
check("sync bad mode -> 422 bad_mode", s == 422 and r.get("error") == "bad_mode", f"{s} {short(r)}")
s, r = call("POST", f"/printers/{pid}/canvas/sync", {}, timeout=90)
check("CANVAS sync", s == 200 and r.get("synced") == 3 and [x.get("tray") for x in r.get("slots", [])] == ["A1", "A2", "A3"]
      and any(u.get("tray") == "A4" and u.get("reason") == "empty" for u in r.get("unknown", [])), f"{s} {short(r)}")
s, fl = call("GET", "/filaments")
check("filament slots follow the trays", s == 200 and fl.get("count") == 3 and
      [x.get("color", "").lower() for x in fl.get("slots", [])] == ["#ff0000", "#ffffff", "#000000"], f"{s} {short(fl)}")
s, r = call("GET", f"/printers/{pid}/canvas/mapping")
check("mapping before slicing", s == 200 and r.get("sliced") is False and r.get("filaments")
      and r["filaments"][0].get("tray", {}).get("tray") == "A1", f"{s} {short(r)}")
check("slice", slice_current() == "done")
s, mp = call("GET", f"/printers/{pid}/canvas/mapping")
check("mapping after slicing: ok, slot 1 -> A1, weight", s == 200 and mp.get("ok") is True and mp.get("sliced") is True
      and mp["filaments"][0]["tray"]["tray"] == "A1" and (mp["filaments"][0].get("weight_g") or 0) > 0, f"{s} {short(mp)}")
s, r = call("GET", f"/printers/{pid}/canvas/mapping?plate=7")
check("mapping unknown plate -> 404 unknown_plate", s == 404 and r.get("error") == "unknown_plate", f"{s} {short(r)}")

# ---------------------------------------------------------------- print control disabled
if MODE == "off":
    for method, path, body in (("POST", f"/printers/{pid}/print/prepare", {}),
                               ("POST", f"/printers/{pid}/print", {"plan_id": "x"}),
                               ("GET", "/uploads/x", None), ("DELETE", "/uploads/x", None),
                               ("POST", f"/printers/{pid}/pause", {}), ("POST", f"/printers/{pid}/resume", {}),
                               ("POST", f"/printers/{pid}/stop", {"confirm": "stop"})):
        s, r = call(method, path, body)
        check(f"{method} {path.replace(pid, '{id}')} -> 403 print_control_disabled",
              s == 403 and r.get("error") == "print_control_disabled" and r.get("hint"), f"{s} {short(r)}")
    reqs = [q.get("method") for q in fake().get("requests", [])]
    check("the fake got no print command", not any(m in (1020, 1021, 1022, 1023) for m in reqs), str(reqs))

# ---------------------------------------------------------------- print control enabled
if MODE == "on":
    s, r = call("POST", f"/printers/{pid}/print/prepare", {"leveling": False, "colour": 1})
    check("prepare unknown field -> 400 unknown_field", s == 400 and r.get("error") == "unknown_field", f"{s} {short(r)}")
    s, r = call("POST", f"/printers/{pid}/print/prepare", {"mapping": [{"slot": 1, "unit": "0", "tray": "A4"}]})
    check("prepare mapping to the empty tray -> 422 bad_mapping", s == 422 and r.get("error") == "bad_mapping", f"{s} {short(r)}")
    s, r = call("POST", f"/printers/{pid}/print/prepare", {"plate": 3})
    check("prepare unknown plate -> 404 unknown_plate", s == 404 and r.get("error") == "unknown_plate", f"{s} {short(r)}")

    fake({"state": "busy"})
    wait_for("the slicer to see the printer busy", lambda: status_of(pid) not in ("idle", "print_completed"), limit=20)
    s, r = call("POST", f"/printers/{pid}/print/prepare", {})
    check("prepare while busy -> 409 printer_busy", s == 409 and r.get("error") == "printer_busy", f"{s} {short(r)}")
    fake({"state": "idle"})
    wait_for("idle again", lambda: status_of(pid) == "idle", limit=20)

    # A plan expires when the project changes after it was made.
    s, r = call("POST", f"/printers/{pid}/print/prepare", {"leveling": False})
    check("prepare (to expire)", s == 200 and r.get("plan_id"), f"{s} {short(r)}")
    call("PUT", "/config", {"sparse_infill_density": "21%"})
    s, r2 = call("POST", f"/printers/{pid}/print", {"plan_id": r.get("plan_id", "")})
    check("print after a setting change -> 410 plan_expired", s == 410 and r2.get("error") == "plan_expired", f"{s} {short(r2)}")
    call("PUT", "/config", {"sparse_infill_density": "15%"})
    check("slice again", slice_current() == "done")

    s, plan = call("POST", f"/printers/{pid}/print/prepare", {"leveling": False})
    sm = plan.get("summary", {})
    check("prepare", s == 200 and plan.get("plan_id") and plan.get("expires_in_s") == 300, f"{s} {short(plan)}")
    check("summary", sm.get("printer_id") == pid and sm.get("file_name", "").endswith(".gcode") and sm.get("layers", 0) > 0
          and sm.get("estimated_time_s", 0) > 0 and sm.get("mapping") and sm["mapping"][0]["tray"]["tray"] == "A1"
          and sm.get("leveling") is False, short(sm))
    s, r = call("POST", f"/printers/{pid}/print", {})
    check("print without plan_id -> 400 bad_plan_id", s == 400 and r.get("error") == "bad_plan_id", f"{s} {short(r)}")
    s, up = call("POST", f"/printers/{pid}/print", {"plan_id": plan.get("plan_id")})
    check("print -> 202 upload_id", s == 202 and up.get("upload_id"), f"{s} {short(up)}")
    uid = up.get("upload_id", "x")
    u = wait_for("the upload to finish", lambda: (lambda r: r if r.get("status") in ("success", "failed", "cancelled") else None)(
        call("GET", f"/uploads/{uid}")[1]), limit=90)
    check("upload success", bool(u) and u.get("status") == "success" and u.get("bytes", [0, 1])[0] == u.get("bytes", [0, 1])[1],
          short(u))
    s, gcode = call("GET", "/gcode", raw=True)
    want = hashlib.sha256(gcode).hexdigest()
    st = fake()
    got = [f for f in st.get("received_files", st.get("files", [])) if f.get("name") == sm.get("file_name")]
    check("the printer got the slicer's G-code, byte for byte", s == 200 and got and got[0].get("sha256") == want,
          f"want {want[:16]} name {sm.get('file_name')} got {[ (f.get('name'), (f.get('sha256') or '')[:16]) for f in st.get('received_files', st.get('files', []))]}")
    starts = [q for q in st.get("requests", []) if q.get("method") == 1020]
    check("the fake was asked to start that file once", len(starts) == 1 and starts[0].get("params", {}).get("filename") == sm.get("file_name"),
          short(starts))
    if starts:
        cfg = starts[0].get("params", {}).get("config", {})
        check("start params: no leveling, slot map to tray 0", cfg.get("printer_check") is False and
              [(x.get("t"), x.get("tray_id")) for x in cfg.get("slot_map", [])] == [(0, 0)], short(cfg))
    s, r = call("POST", f"/printers/{pid}/print", {"plan_id": plan.get("plan_id")})
    check("a plan is used once -> 404 unknown_plan", s == 404 and r.get("error") == "unknown_plan", f"{s} {short(r)}")
    s, r = call("DELETE", f"/uploads/{uid}")
    check("cancel after the transfer -> 409 too_late", s == 409 and r.get("error") == "too_late", f"{s} {short(r)}")
    s, r = call("GET", "/uploads/nope")
    check("unknown upload -> 404 unknown_upload", s == 404 and r.get("error") == "unknown_upload", f"{s} {short(r)}")

    wait_for("the print to run", lambda: status_of(pid) == "printing", limit=30)
    s, r = call("POST", f"/printers/{pid}/print/prepare", {})
    check("prepare while printing -> 409 printer_busy", s == 409 and r.get("error") == "printer_busy", f"{s} {short(r)}")
    s, r = call("POST", f"/printers/{pid}/pause", {})
    check("pause", s == 200 and r.get("ok") is True, f"{s} {short(r)}")
    wait_for("paused", lambda: status_of(pid) == "paused", limit=20)
    check("the fake is paused", fake().get("phase") in ("paused", "pausing"), str(fake().get("phase")))
    s, r = call("POST", f"/printers/{pid}/resume", {})
    check("resume -> 501 resume_unsupported", s == 501 and r.get("error") == "resume_unsupported", f"{s} {short(r)}")
    s, r = call("POST", f"/printers/{pid}/stop", {})
    check("stop without confirm -> 400 confirm_required", s == 400 and r.get("error") == "confirm_required", f"{s} {short(r)}")
    s, r = call("POST", f"/printers/{pid}/stop", {"confirm": "stop"})
    check("stop", s == 200 and r.get("ok") is True, f"{s} {short(r)}")
    wait_for("idle after stop", lambda: status_of(pid) == "idle", limit=30)
    check("the fake stopped", fake().get("phase") in ("idle", "stopping", "stopped"), str(fake().get("phase")))
    s, r = call("POST", f"/printers/{pid}/stop", {"confirm": "stop"})
    check("stop while idle -> 409 not_printing", s == 409 and r.get("error") == "not_printing", f"{s} {short(r)}")
    s, r = call("POST", f"/printers/{pid}/pause", {})
    check("pause while idle -> 409 not_printing", s == 409 and r.get("error") == "not_printing", f"{s} {short(r)}")

    # A stop the printer answers late (a real CC2 answers after parking): the SDK gives up at 15 s.
    s, plan = call("POST", f"/printers/{pid}/print/prepare", {"leveling": False})
    s, up = call("POST", f"/printers/{pid}/print", {"plan_id": plan.get("plan_id")})
    wait_for("second upload", lambda: call("GET", f"/uploads/{up.get('upload_id', 'x')}")[1].get("status") == "success", limit=60)
    wait_for("the second print to run", lambda: status_of(pid) == "printing", limit=30)
    fake({"delay_next": {"method": 1022, "seconds": 17}})
    t0 = time.time()
    s, r = call("POST", f"/printers/{pid}/stop", {"confirm": "stop"}, timeout=60)
    took = time.time() - t0
    check("late stop answer -> 504 printer_timeout with a hint, within 25 s", s == 504 and r.get("error") == "printer_timeout"
          and r.get("hint") and took < 26, f"{s} {took:.1f}s {short(r)}")
    wait_for("idle after the late stop", lambda: status_of(pid) == "idle", limit=40)
    check("the fake stopped anyway", fake().get("phase") in ("idle", "stopping", "stopped"), str(fake().get("phase")))

    # Printer offline.
    fake({"offline": True})
    off = wait_for("the slicer to see the printer offline", lambda: status_of(pid) == "offline", limit=60, step=2)
    check("the printer goes offline", bool(off))
    s, r = call("POST", f"/printers/{pid}/print/prepare", {})
    check("prepare while offline -> 409 printer_offline", s == 409 and r.get("error") == "printer_offline", f"{s} {short(r)}")
    s, r = call("GET", f"/printers/{pid}/canvas")
    check("canvas while offline -> 409 printer_offline", s == 409 and r.get("error") == "printer_offline", f"{s} {short(r)}")
    fake({"offline": False})
    back = wait_for("the printer to come back", lambda: (printer() or {}).get("connected"), limit=60, step=2)
    check("the printer comes back", bool(back))

# ---------------------------------------------------------------- secrets
leaks = [(where, v) for where, text in printer_bodies for v in SECRET_VALUES + SECRET_KEYS if v in text]
check(f"no secret in {len(printer_bodies)} printer replies", not leaks, str(leaks[:5]))

print("RESULT:", "ALL PASS" if not failed else f"{len(failed)} FAILED: {failed}")
sys.exit(1 if failed else 0)
