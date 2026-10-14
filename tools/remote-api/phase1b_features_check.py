"""Runtime check for the pulled-forward features (PORT_NOTES.md section 11.2):
stale per-plate slice state, POST /orient {"object_id"}, POST /view, part volumes,
POST /objects/{id}/paint and POST /project/save.

Usage: py -3 -I tools/remote-api/phase1b_features_check.py <token> <cube.stl> <outdir>
Needs a running ElegooSlicer MCP on 127.0.0.1:13130 started with a TEST --datadir.
It deletes every object in that window and saves .3mf files into <outdir>.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:13130/api/v1"
TOKEN, MODEL, OUT = sys.argv[1], sys.argv[2], os.path.abspath(sys.argv[3])
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


def wait_jobs(limit=120):
    end = time.time() + limit
    while time.time() < end:
        if call("GET", "/jobs/status")[1].get("idle"):
            return True
        time.sleep(0.5)
    return False


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


def plates_valid():
    return [p["slice_result_valid"] for p in call("GET", "/plates")[1]["plates"]]


def obj(oid):
    return [o for o in call("GET", "/objects")[1]["objects"] if o["id"] == oid][0]


def close(a, b, tol=1e-2):
    return all(abs(x - y) < tol for x, y in zip(a, b))


os.makedirs(OUT, exist_ok=True)

# --- clean state: one plate, two copies of the model, one per plate ---------------
for o in call("GET", "/objects")[1].get("objects", []):
    call("DELETE", f"/objects/{o['id']}")
while call("GET", "/plates")[1]["count"] > 1:
    call("DELETE", f"/plates/{call('GET', '/plates')[1]['count'] - 1}")
for _ in range(2):
    check("load model", call("POST", "/model", {"path": MODEL})[0] == 200)
objs = call("GET", "/objects")[1]["objects"]
a, b = objs[0]["id"], objs[1]["id"]
call("POST", "/plates", {"select": False})
s, r = call("POST", f"/objects/{b}/plate", {"plate": 1})
check("object B moved to plate 1", s == 200, f"{s} {r}")

# --- 1. stale slice state ---------------------------------------------------------
call("POST", "/plates/0/select")
st0 = slice_current()
call("POST", "/plates/1/select")
st1 = slice_current()
check("both plates sliced", st0 == "done" and st1 == "done" and plates_valid() == [True, True],
      f"{st0} {st1} {plates_valid()}")
s, r = call("PUT", "/config", {"sparse_infill_density": "25%"})
check("global PUT /config marks every plate unsliced", s == 200 and plates_valid() == [False, False],
      f"{s} {plates_valid()}")
st1 = slice_current()
call("POST", "/plates/0/select")
st0 = slice_current()
s, r = call("PUT", f"/objects/{a}/config", {"wall_loops": 4})
check("per-object config marks only that object's plate", s == 200 and plates_valid() == [False, True],
      f"{s} {plates_valid()}")

# --- 2. view switch ---------------------------------------------------------------
s, r = call("POST", "/view", {"view": "preview"})
check("POST /view preview", s == 200 and r.get("view") == "preview" and
      call("GET", "/status")[1].get("view") == "preview", f"{s} {r}")
s, r = call("POST", "/view", {"view": "prepare"})
check("POST /view prepare", s == 200 and call("GET", "/status")[1].get("view") == "prepare", f"{s} {r}")
s, r = call("POST", "/view", {"view": "sideways"})
check("POST /view bad view -> 422 bad_view", s == 422 and r.get("error") == "bad_view", f"{s} {r}")

# --- 3. orient one object ---------------------------------------------------------
# Tilt both copies 45 degrees about X; orienting A alone must lay A flat again (its
# world bbox back to the untilted size) and leave B tilted.
def world_size(oid):
    o = obj(oid)
    return sorted(round(hi - lo, 2) for lo, hi in zip(o["bbox_min"], o["bbox_max"]))


flat = world_size(a)
for oid in (a, b):
    call("POST", f"/objects/{oid}/transform", {"rotate": [45, 0, 0]})
tilted_b, rot_b = world_size(b), obj(b)["transform"]["rotation"]
s, r = call("POST", "/orient", {"object_id": a})
check("POST /orient {object_id} -> 202", s == 202 and r.get("object_id") == a, f"{s} {r}")
check("orient job finished", wait_jobs())
check("object A laid flat again", close(world_size(a), flat, 0.1) and obj(a).get("on_plate"),
      f"{flat} -> {world_size(a)}")
check("object B untouched", close(world_size(b), tilted_b, 0.1) and close(obj(b)["transform"]["rotation"], rot_b),
      f"{tilted_b} -> {world_size(b)}")
s, r = call("POST", "/orient", {"object_id": 999999})
check("orient unknown object -> 404 unknown_object", s == 404 and r.get("error") == "unknown_object", f"{s} {r}")
call("POST", f"/objects/{b}/transform", {"rotate": [-45, 0, 0]})

# --- 4. part volumes --------------------------------------------------------------
s, r = call("GET", f"/objects/{a}/volumes")
check("list volumes: one part", s == 200 and len(r.get("volumes", [])) == 1 and
      r["volumes"][0]["type"] == "part", f"{s} {r}")
part_id = r["volumes"][0]["id"] if s == 200 else 0
center = r["volumes"][0]["position_mm"] if s == 200 else [128, 128, 10]
s, r = call("POST", f"/objects/{a}/volumes", {"type": "modifier", "shape": "box", "size_mm": [10, 10, 5],
                                               "position_mm": center, "config": {"sparse_infill_density": "40%"}})
check("add modifier box -> 201", s == 201 and r.get("volume", {}).get("type") == "modifier", f"{s} {r}")
mod_id = r.get("volume", {}).get("id")
if s == 201:
    check("modifier size/position", close(r["volume"]["size_mm"], [10, 10, 5], 0.05) and
          close(r["volume"]["position_mm"], center, 0.05), f"{r['volume']}")
s, r = call("POST", f"/objects/{a}/volumes", {"type": "support_blocker", "shape": "cylinder",
                                               "size_mm": [8, 8, 8], "position_mm": center})
check("add support blocker cylinder -> 201", s == 201, f"{s} {r}")
s, r = call("PUT", f"/objects/{a}/volumes/{mod_id}/config", {"sparse_infill_density": "60%"})
check("PUT volume config", s == 200 and r.get("applied") == ["sparse_infill_density"], f"{s} {r}")
s, r = call("PUT", f"/objects/{a}/volumes/{mod_id}/config", {"no_such_key": 1})
check("PUT volume config bad key -> 422, nothing applied", s == 422 and r.get("applied") == [], f"{s} {r}")
s, r = call("POST", f"/objects/{a}/volumes", {"type": "lamp", "shape": "box", "size_mm": [1, 1, 1],
                                               "position_mm": center})
check("add bad type -> 422 bad_type", s == 422 and r.get("error") == "bad_type", f"{s} {r}")
s, r = call("DELETE", f"/objects/{a}/volumes/{part_id}")
check("delete last part -> 409 last_part", s == 409 and r.get("error") == "last_part", f"{s} {r}")
s, r = call("DELETE", f"/objects/{a}/volumes/{mod_id}")
check("delete modifier", s == 200 and r.get("ok") is True, f"{s} {r}")
s, r = call("GET", f"/objects/{a}/volumes")
check("two volumes left (part + blocker)", s == 200 and len(r.get("volumes", [])) == 2, f"{s} {r}")
s, r = call("DELETE", f"/objects/{a}/volumes/999999")
check("delete unknown volume -> 404 unknown_volume", s == 404 and r.get("error") == "unknown_volume", f"{s} {r}")

# --- 5. paint ---------------------------------------------------------------------
s, r = call("POST", f"/objects/{a}/paint", {"layer": "support", "state": "enforce", "max_overhang_deg": 45})
check("paint support under overhangs", s == 200 and r.get("painted_triangles", 0) > 0, f"{s} {r}")
s, r = call("POST", f"/objects/{a}/paint", {"layer": "seam", "state": "enforce",
                                             "box_mm": [[0, 0, 0], [400, 400, 3]]})
check("paint seam in a box", s == 200 and r.get("painted_triangles", 0) > 0, f"{s} {r}")
s, r = call("POST", f"/objects/{a}/paint", {"layer": "color", "slot": 1,
                                             "box_mm": [[0, 0, 5], [400, 400, 400]]})
check("paint color slot 1", s == 200, f"{s} {r}")
s, r = call("POST", f"/objects/{a}/paint", {"layer": "color", "slot": 99})
check("paint bad slot -> 422 bad_slot", s == 422 and r.get("error") == "bad_slot", f"{s} {r}")
s, r = call("POST", f"/objects/{a}/paint", {"layer": "support", "state": "enforce", "max_overhang_deg": 100})
check("paint bad overhang -> 422 bad_overhang", s == 422 and r.get("error") == "bad_overhang", f"{s} {r}")
s, r = call("POST", f"/objects/{a}/paint", {"layer": "support", "clear": True,
                                             "box_mm": [[0, 0, 0], [1, 1, 1]]})
check("clear with box -> 422 clear_takes_no_box", s == 422 and r.get("error") == "clear_takes_no_box", f"{s} {r}")
s, r = call("POST", f"/objects/{a}/paint", {"layer": "support", "clear": True})
check("clear support paint", s == 200 and r.get("cleared") is True, f"{s} {r}")
check("paint marked plate 0 unsliced", plates_valid()[0] is False, f"{plates_valid()}")

# --- 6. project save --------------------------------------------------------------
proj = os.path.join(OUT, "phase1b_project.3mf").replace("\\", "/")
if os.path.exists(proj):
    os.remove(proj)
s, r = call("POST", "/project/save", {"path": os.path.join(OUT, "x.stl")})
check("save as .stl -> 422 not_3mf", s == 422 and r.get("error") == "not_3mf", f"{s} {r}")
s, r = call("POST", "/project/save", {"path": "relative.3mf"})
check("save relative path -> 422 bad_path", s == 422 and r.get("error") == "bad_path", f"{s} {r}")
s, r = call("POST", "/project/save", {"path": proj})
check("save to a new path", s == 200 and os.path.getsize(proj) > 1000, f"{s} {r}")
s, r = call("POST", "/project/save", {"path": proj})
check("same path without overwrite -> 409 file_exists", s == 409 and r.get("error") == "file_exists", f"{s} {r}")
s, r = call("POST", "/project/save", {"path": proj, "overwrite": True})
check("same path with overwrite", s == 200, f"{s} {r}")
s, r = call("POST", "/project/save")
check("save with no body -> current file", s == 200 and r.get("path", "").lower().endswith("phase1b_project.3mf"),
      f"{s} {r}")

print("RESULT:", "ALL PASS" if not failed else f"{len(failed)} FAILED: {failed}")
sys.exit(1 if failed else 0)
