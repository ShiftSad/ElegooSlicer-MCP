"""Runtime check for the transform/load fixes (Victor's Claude Desktop findings).

Usage: py -3 -I tools/remote-api/phase1_rotation_check.py <token> <model.stl>
Needs a running ElegooSlicer MCP with the API on 127.0.0.1:13130 (use a test --datadir).
"""
import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:13130/api/v1"
TOKEN, MODEL = sys.argv[1], sys.argv[2]
ok_all = True


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("X-Api-Token", TOKEN)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def check(name, cond, detail=""):
    global ok_all
    ok_all &= bool(cond)
    print(("PASS  " if cond else "FAIL  ") + name + (f"  | {detail}" if detail else ""))


s, r = call("POST", "/model", {"path": "C:/does/not/exist/model.stl"})
check("missing file -> 404 file_not_found", s == 404 and r.get("error") == "file_not_found", f"{s} {r}")

for o in call("GET", "/objects")[1].get("objects", []):
    call("DELETE", f"/objects/{o['id']}")
s, r = call("POST", "/model", {"path": MODEL})
check("load real model", s == 200, f"{s}")
oid = call("GET", "/objects")[1]["objects"][0]["id"]


def state():
    # rotation in radians (transform.rotation); world bbox of the first copy
    o = [x for x in call("GET", "/objects")[1]["objects"] if x["id"] == oid][0]
    size = [hi - lo for lo, hi in zip(o["bbox_min"], o["bbox_max"])]
    return o["transform"]["rotation"], size, o["bbox_min"][2]


rot0, size0, _ = state()
s1, _ = call("POST", f"/objects/{oid}/transform", {"rotate": [0, -90, 0]})
rot1, size1, zmin1 = state()
s2, _ = call("POST", f"/objects/{oid}/transform", {"rotate": [0, 90, 0]})
rot2, size2, zmin2 = state()
print("rotation before/mid/after:", rot0, rot1, rot2)
print("size     before/mid/after:", size0, size1, size2)
check("both rotate calls succeeded", s1 == 200 and s2 == 200, f"{s1} {s2}")
check("Y -90 then +90 returns to the original rotation", all(abs(a - b) < 1e-4 for a, b in zip(rot2, rot0)), f"{rot2}")
check("after Y -90 the X and Z extents swapped", abs(size1[0] - size0[2]) < 0.05 and abs(size1[2] - size0[0]) < 0.05,
      f"{size0} -> {size1}")
check("mid: part rests on the bed (min z = 0)", abs(zmin1) < 0.01, f"min z = {zmin1}")
check("after: part rests on the bed (min z = 0)", abs(zmin2) < 0.01, f"min z = {zmin2}")

# A part lifted 5 mm drops back onto the bed, as a drag in the window does.
call("POST", f"/objects/{oid}/transform", {"translate": [0, 0, 5]})
check("translate up 5 mm: dropped back onto the bed", abs(state()[2]) < 0.01, f"min z = {state()[2]}")
print("RESULT:", "ALL PASS" if ok_all else "SOME FAILED")
sys.exit(0 if ok_all else 1)
