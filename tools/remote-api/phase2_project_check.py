"""Runtime check of the generic project routes (tools/remote-api/CONTRACT.md): config_rejected
token, filament slots, split, undo/redo, layer changes, calibration, slice breakdown purge.

Usage: py -3 -I tools/remote-api/phase2_project_check.py <token> <model.stl>
Needs a running ElegooSlicer MCP on 127.0.0.1:13130 started with a TEST --datadir.
It deletes every object in that window.
"""
import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:13130/api/v1"
TOKEN, MODEL = sys.argv[1], sys.argv[2]
failed = []


def call(method, path, body=None, timeout=150):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("X-Api-Token", TOKEN)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def check(name, cond, detail=""):
    if not cond:
        failed.append(name)
    print(("PASS  " if cond else "FAIL  ") + name + (f"  | {detail}" if detail else ""))


def short(r):
    s = json.dumps(r, ensure_ascii=False)
    return s if len(s) < 400 else s[:400] + "..."


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


def wait_jobs(limit=120):
    end = time.time() + limit
    while time.time() < end:
        if call("GET", "/jobs/status")[1].get("idle"):
            return True
        time.sleep(0.5)
    return False


caps = call("GET", "/status")[1].get("capabilities", [])
for c in ("filaments", "split", "undo", "layer_changes", "calibration", "breakdown_purge"):
    check(f"capability {c}", c in caps)

for o in call("GET", "/objects")[1].get("objects", []):
    call("DELETE", f"/objects/{o['id']}")
while call("GET", "/plates")[1]["count"] > 1:
    call("DELETE", f"/plates/{call('GET', '/plates')[1]['count'] - 1}")
check("load model", call("POST", "/model", {"path": MODEL})[0] == 200)
oid = call("GET", "/objects")[1]["objects"][0]["id"]

# --- config_rejected token ---------------------------------------------------------
s, r = call("PUT", "/config", {"no_such_key": 1})
check("PUT /config bad key -> 422 config_rejected", s == 422 and r.get("error") == "config_rejected"
      and r.get("applied") == [] and "no_such_key" in r.get("errors", {}), f"{s} {short(r)}")
s, r = call("PUT", f"/objects/{oid}/config", {"no_such_key": 1})
check("PUT object config bad key -> 422 config_rejected", s == 422 and r.get("error") == "config_rejected",
      f"{s} {short(r)}")

# --- filament slots ----------------------------------------------------------------
s, r = call("GET", "/filaments")
check("GET /filaments", s == 200 and r.get("count", 0) >= 1 and len(r.get("slots", [])) == r.get("count"),
      f"{s} {short(r)}")
s, r = call("PUT", "/filaments", {"count": 2})
check("PUT /filaments count 2", s == 200 and r.get("count") == 2, f"{s} {short(r)}")
s, r = call("PUT", "/filaments/2", {"color": "#ff0000"})
slot2 = r if "color" in r else next((x for x in r.get("slots", []) if x.get("slot") == 2), {})
check("PUT /filaments/2 colour", s == 200 and str(slot2.get("color", "")).lower() == "#ff0000", f"{s} {short(r)}")
s, r = call("PUT", "/filaments/2", {"color": "red"})
check("bad colour -> 422 bad_color", s == 422 and r.get("error") == "bad_color", f"{s} {short(r)}")
s, r = call("PUT", "/filaments/9", {"color": "#00ff00"})
check("unknown slot -> 404 unknown_slot", s == 404 and r.get("error") == "unknown_slot", f"{s} {short(r)}")
s, r = call("PUT", "/filaments/2", {"preset": "No Such Filament Preset"})
check("unknown preset -> 422 unknown_preset", s == 422 and r.get("error") == "unknown_preset", f"{s} {short(r)}")
s, r = call("PUT", f"/objects/{oid}/config", {"extruder": 2})
check("object uses slot 2", s == 200, f"{s} {short(r)}")
s, r = call("PUT", "/filaments", {"count": 1})
check("remove used slot -> 409 slot_in_use", s == 409 and r.get("error") == "slot_in_use" and r.get("used_by"),
      f"{s} {short(r)}")
s, r = call("PUT", "/filaments", {"count": 1, "force": True})
check("remove used slot with force", s == 200 and r.get("count") == 1, f"{s} {short(r)}")

# --- undo / redo -------------------------------------------------------------------
def offset():
    o = [x for x in call("GET", "/objects")[1]["objects"] if x["id"] == oid]
    return o[0]["transform"]["offset"] if o else [float("nan")] * 3


before = offset()
call("POST", f"/objects/{oid}/transform", {"translate": [20, 0, 0]})
moved = offset()
s, r = call("POST", "/undo")
check("undo", s == 200 and r.get("ok") is True and "can_redo" in r, f"{s} {short(r)}")
check("undo put the object back", abs(offset()[0] - before[0]) < 0.01, f"{before} {moved} -> {offset()}")
s, r = call("POST", "/redo")
check("redo", s == 200 and r.get("ok") is True, f"{s} {short(r)}")
check("redo moved it again", abs(offset()[0] - moved[0]) < 0.01, f"-> {offset()}")

n_before = call("GET", "/objects")[1]["count"]
inst_before = call("GET", "/objects")[1]["objects"][0].get("instances")
call("POST", f"/objects/{oid}/duplicate")
inst_dup = call("GET", "/objects")[1]["objects"][0].get("instances")
s, r = call("POST", "/undo")
inst_after = call("GET", "/objects")[1]["objects"][0].get("instances")
check("undo a duplicate", s == 200 and inst_dup != inst_before and inst_after == inst_before,
      f"{inst_before} -> {inst_dup} -> undo {inst_after}")
call("DELETE", f"/objects/{oid}")
s, r = call("POST", "/undo")
check("undo a delete brings the object back", s == 200 and call("GET", "/objects")[1]["count"] == n_before, f"{s} {short(r)}")

if not [x for x in call("GET", "/objects")[1]["objects"] if x["id"] == oid]:  # undo went too far
    for o in call("GET", "/objects")[1].get("objects", []):
        call("DELETE", f"/objects/{o['id']}")
    call("POST", "/model", {"path": MODEL})
    oid = call("GET", "/objects")[1]["objects"][0]["id"]

# --- layer changes -----------------------------------------------------------------
call("PUT", "/filaments", {"count": 2})  # a tool change needs 2 or more slots
s, r = call("GET", "/plates/0/layer_changes")
check("GET layer_changes", s == 200 and r.get("items") == [], f"{s} {short(r)}")
s, r = call("POST", "/plates/0/layer_changes", {"z": 3.0, "type": "pause", "message": "insert magnet"})
check("add pause at 3 mm -> 201", s == 201 and len(r.get("items", [])) == 1, f"{s} {short(r)}")
s, r = call("POST", "/plates/0/layer_changes", {"z": 999, "type": "pause"})
check("z above the objects -> 422 bad_z", s == 422 and r.get("error") == "bad_z", f"{s} {short(r)}")
s, r = call("POST", "/plates/0/layer_changes", {"z": 2.0, "type": "tool_change", "slot": 7})
check("missing slot -> 422 bad_slot", s == 422 and r.get("error") == "bad_slot", f"{s} {short(r)}")
s, r = call("POST", "/plates/0/layer_changes", {"z": 2.0, "type": "teleport"})
check("bad type -> 422 bad_type", s == 422 and r.get("error") == "bad_type", f"{s} {short(r)}")
s, r = call("DELETE", "/plates/0/layer_changes?z=3")
check("delete the pause", s == 200 and r.get("items") == [], f"{s} {short(r)}")
s, r = call("DELETE", "/plates/0/layer_changes?z=3")
check("delete again -> 404 no_change_at_z", s == 404 and r.get("error") == "no_change_at_z", f"{s} {short(r)}")
s, r = call("GET", "/plates/9/layer_changes")
check("unknown plate -> 404 unknown_plate", s == 404 and r.get("error") == "unknown_plate", f"{s} {short(r)}")

# A tool change at a height really changes filament in the G-code (M6211 on the CC2) and shows
# up in the purge breakdown.
s, r = call("POST", "/plates/0/layer_changes", {"z": 10.0, "type": "tool_change", "slot": 2})
check("add tool change at 10 mm", s == 201, f"{s} {short(r)}")
check("slice with the tool change", slice_current() == "done")
req = urllib.request.Request(BASE + "/gcode", headers={"X-Api-Token": TOKEN})
gcode = urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "replace")
m6211 = [l for l in gcode.splitlines() if l.startswith("M6211")]
check("G-code changes filament (M6211 at start + the change)", len(m6211) >= 2, str(m6211[:4]))
purge = (call("GET", "/slice/status")[1].get("breakdown") or {}).get("purge") or {}
check("purge counts the change", purge.get("filament_changes", 0) >= 1 and purge.get("flush_g", 0) > 0, str(purge))
call("DELETE", "/plates/0/layer_changes?z=10")

# --- breakdown purge ---------------------------------------------------------------
st = slice_current()
s, r = call("GET", "/slice/status")
purge = (r.get("breakdown") or {}).get("purge") or (r.get("stats", {}).get("breakdown") or {}).get("purge")
check("slice breakdown has purge", st == "done" and isinstance(purge, dict) and
      {"filament_changes", "filament_change_time_s", "flush_g"} <= set(purge), f"{st} {purge}")

# --- split -------------------------------------------------------------------------
s, r = call("POST", f"/objects/{oid}/split", {"to": "parts"})
check("split to parts: 200 ids or 422 not_splittable", (s == 200 and isinstance(r.get("ids"), list)) or
      (s == 422 and r.get("error") == "not_splittable"), f"{s} {short(r)}")
s, r = call("POST", f"/objects/{oid}/split", {"to": "pieces"})
check("split bad target -> 4xx", 400 <= s < 500, f"{s} {short(r)}")

# --- calibration (last: it replaces the project) ------------------------------------
s, r = call("POST", "/calibration", {"type": "flow_rate", "params": {}})
check("calibration with unsaved project -> 409 unsaved_project", s == 409 and r.get("error") == "unsaved_project",
      f"{s} {short(r)}")
s, r = call("POST", "/calibration", {"type": "flow_rate", "params": {}, "discard_project": True})
check("flow-rate calibration project", s == 200 and r.get("objects"), f"{s} {short(r)}")
wait_jobs()
s, r = call("POST", "/calibration", {"type": "warp_drive", "params": {}, "discard_project": True})
check("bad calibration type -> 422 bad_type", s == 422 and r.get("error") == "bad_type", f"{s} {short(r)}")

print("RESULT:", "ALL PASS" if not failed else f"{len(failed)} FAILED: {failed}")
sys.exit(1 if failed else 0)
