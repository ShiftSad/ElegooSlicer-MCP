#!/usr/bin/env python3
"""Runtime checks for the three Remote API fixes (orcaslicer-mcp #9, #10, #11), plus read-only
checks of the ElegooSlicer MCP routes the build advertises in GET /status capabilities.

Usage: ORCA_BASE=http://127.0.0.1:13131 ORCA_TOKEN=... python3 orca-runtime-test.py
(ElegooSlicer MCP listens on 13130: ORCA_BASE=http://127.0.0.1:13130.)
Read-mostly: every write is restored, and each check asserts the BEFORE state too,
so a check cannot pass by accident on an already-broken value. The ElegooSlicer MCP part
only reads (and, while print control is off in Preferences, checks that two print routes
refuse); a route whose capability is missing is reported as SKIP, not as a failure.
"""
import json, os, sys, urllib.parse, urllib.request, urllib.error

BASE  = os.environ.get("ORCA_BASE", "http://127.0.0.1:13131")
TOKEN = os.environ["ORCA_TOKEN"]
results = []

def call(method, path, body=None):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-Api-Token", TOKEN)
    if data: req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try: return e.code, json.loads(raw or "{}")
        except Exception: return e.code, {"_raw": raw}

def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS  " if ok else "FAIL  ") + name + (("  | " + detail) if detail else ""))

def get_key(k):
    s, j = call("GET", "/api/v1/config?keys=" + k)
    return j.get("config", {}).get(k)

print("=== connectivity ===")
s, st = call("GET", "/api/v1/status")
check("GET /status reachable", s == 200, "status=%d" % s)
if s != 200:
    sys.exit("cannot reach OrcaSlicer Remote API at " + BASE)
print(json.dumps({k: st.get(k) for k in ("presets", "modified") if k in st}, indent=2)[:800])

# ---------------------------------------------------------------- #9
print("\n=== #9 per-filament length guard ===")
KEY = "hot_plate_temp"
before = get_key(KEY)
check("baseline readable", before is not None, "%s=%r" % (KEY, before))
n = len(before.split(",")) if before else 0

# a write with one MORE entry than the preset holds must be refused
bad = ",".join((before.split(",") + ["70"])) if before else "70,70"
s, j = call("PUT", "/api/v1/config", {KEY: bad})
rejected_reply = j  # the ElegooSlicer MCP block below checks its error token
errs = j.get("errors", {})
check("over-long write rejected with 422", s == 422, "status=%d" % s)
check("error names per_filament_length_mismatch",
      "per_filament_length_mismatch" in str(errs.get(KEY, "")), str(errs.get(KEY, ""))[:160])
check("nothing applied", j.get("applied") == [], repr(j.get("applied")))
after = get_key(KEY)
check("value UNCHANGED after rejected write", after == before, "before=%r after=%r" % (before, after))

# a single-value write to the edited preset must still work (N==1 case)
if n == 1:
    s, j = call("PUT", "/api/v1/config", {KEY: before})
    check("write-back of unchanged single value still accepted", s == 200, "status=%d" % s)
    check("value still unchanged", get_key(KEY) == before, "")
else:
    s, j = call("PUT", "/api/v1/config", {KEY: before})
    check("merged %d-entry write-back rejected (the #9 bug)" % n, s == 422,
          "status=%d err=%s" % (s, str(j.get("errors", {}).get(KEY, ""))[:120]))
    check("value UNCHANGED after merged write-back", get_key(KEY) == before, "")

# ---------------------------------------------------------------- #10
print("\n=== #10 select_preset reports discarded groups ===")
PK = "wall_loops"
orig_wl = get_key(PK)
s, j = call("PUT", "/api/v1/config", {PK: str(int(orig_wl) + 1)})
check("print-group override applied", s == 200, "status=%d %s" % (s, j.get("applied")))
s, st2 = call("GET", "/api/v1/status")
mod_print = (st2.get("modified") or {}).get("print", [])
check("status shows print dirty BEFORE select", PK in mod_print, repr(mod_print)[:160])

cur_fil = ((st2.get("presets") or {}).get("filaments") or [None])[0]
check("current filament preset known", bool(cur_fil), repr(cur_fil))
s, j = call("PUT", "/api/v1/preset", {"type": "filament", "name": cur_fil})
check("select_preset succeeded", s == 200, "status=%d" % s)
dc = j.get("discarded_changes")
check("response carries discarded_changes", dc is not None, repr(dc))
check("discarded_changes names the print group", bool(dc) and "print" in dc, repr(dc))

s, st3 = call("GET", "/api/v1/status")
check("print overrides were in fact discarded",
      PK not in ((st3.get("modified") or {}).get("print", [])), "")
now_wl = get_key(PK)
if now_wl != orig_wl:
    call("PUT", "/api/v1/config", {PK: orig_wl})
check("wall_loops restored", get_key(PK) == orig_wl, "orig=%r now=%r" % (orig_wl, get_key(PK)))

# ---------------------------------------------------------------- ElegooSlicer MCP
# Read-only checks of the routes only ElegooSlicer MCP has. Each block runs only when GET
# /status lists its capability, so an older build (or stock OrcaSlicer MCP) skips them.
# Nothing here writes, slices, or sends anything to a printer.
caps = st.get("capabilities") or []
print("\n=== ElegooSlicer MCP read-only routes ===")
print("capabilities: " + ", ".join(caps))
skipped = []

def skip(name, why):
    skipped.append(name)
    print("SKIP  " + name + "  | " + why)

# Keys that must never appear in any printer reply (whitelist on the slicer side).
SECRET_KEYS = {"password", "token", "accesscode", "access_code", "pincode", "pin_code",
               "extrainfo", "extra_info", "username", "user_name", "serialnumber",
               "serial_number", "mainboardid", "mainboard_id", "weburl", "web_url"}

def secret_keys_in(value, path="$"):
    found = []
    if isinstance(value, dict):
        for k, v in value.items():
            if str(k).lower() in SECRET_KEYS:
                found.append(path + "." + str(k))
            found += secret_keys_in(v, path + "." + str(k))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            found += secret_keys_in(v, "%s[%d]" % (path, i))
    return found

HEX = "0123456789abcdefABCDEF"
def is_hex_colour(c):
    return isinstance(c, str) and len(c) == 7 and c[0] == "#" and all(x in HEX for x in c[1:])

current_plate = ((st.get("plates") or {}).get("current"))

if "filaments" in caps:
    # Builds that list tonight's capabilities also name a refused config write.
    check("refused config write has error config_rejected",
          rejected_reply.get("error") == "config_rejected", repr(rejected_reply.get("error")))
else:
    skip("config_rejected token", "build predates it (no 'filaments' capability)")

if "print_control" in caps:
    check("status has print_control_enabled (bool)",
          isinstance(st.get("print_control_enabled"), bool), repr(st.get("print_control_enabled")))
else:
    skip("print_control_enabled in /status", "no 'print_control' capability")

if "filaments" in caps:
    s, j = call("GET", "/api/v1/filaments")
    slots = j.get("slots") or []
    check("GET /filaments 200", s == 200, "status=%d" % s)
    check("filaments count == number of slots", j.get("count") == len(slots),
          "count=%r slots=%d" % (j.get("count"), len(slots)))
    check("filaments max >= count", isinstance(j.get("max"), int) and j["max"] >= len(slots),
          "max=%r" % j.get("max"))
    check("slots numbered 1..n", [x.get("slot") for x in slots] == list(range(1, len(slots) + 1)),
          repr([x.get("slot") for x in slots]))
    check("every slot has preset, type, #rrggbb colour and used_on_current_plate",
          all(x.get("preset") and "type" in x and is_hex_colour(x.get("color"))
              and isinstance(x.get("used_on_current_plate"), bool) for x in slots),
          json.dumps(slots)[:200])
else:
    skip("GET /filaments", "no 'filaments' capability")

if "layer_changes" in caps and current_plate is not None:
    s, j = call("GET", "/api/v1/plates/%d/layer_changes" % current_plate)
    items = j.get("items")
    check("GET /plates/{current}/layer_changes 200", s == 200, "status=%d" % s)
    check("layer_changes names its plate", j.get("plate") == current_plate, repr(j.get("plate")))
    check("layer_changes items are {z, type}",
          isinstance(items, list) and all(isinstance(x.get("z"), (int, float)) and x.get("type") in
              ("tool_change", "color_change", "pause", "custom") for x in items),
          json.dumps(items)[:200])
    s, j = call("GET", "/api/v1/plates/9999/layer_changes")
    check("layer_changes of a missing plate -> 404 unknown_plate",
          s == 404 and j.get("error") == "unknown_plate", "status=%d %r" % (s, j.get("error")))
else:
    skip("GET /plates/{i}/layer_changes", "no 'layer_changes' capability")

if "breakdown_purge" in caps:
    s, j = call("GET", "/api/v1/slice/status")
    bd = j.get("breakdown")
    if s == 200 and bd:
        purge = bd.get("purge") or {}
        check("breakdown has purge {filament_changes, filament_change_time_s, flush_g, flush_mm}",
              all(k in purge for k in ("filament_changes", "filament_change_time_s", "flush_g",
                                       "flush_mm")), json.dumps(purge)[:200])
    else:
        skip("breakdown.purge", "no breakdown in /slice/status (slice the plate first)")
else:
    skip("breakdown.purge", "no 'breakdown_purge' capability")

printers = []
if "printers" in caps:
    s, j = call("GET", "/api/v1/printers")
    printers = j.get("printers") or []
    check("GET /printers 200 with a list", s == 200 and isinstance(j.get("printers"), list),
          "status=%d" % s)
    check("GET /printers has no secrets", not secret_keys_in(j), repr(secret_keys_in(j))[:200])
    s, j = call("GET", "/api/v1/printers/no-such-printer")
    check("unknown printer -> 404 printer_not_found",
          s == 404 and j.get("error") == "printer_not_found", "status=%d %r" % (s, j.get("error")))
    if printers:
        p = printers[0]
        pid = urllib.parse.quote(str(p.get("id")), safe="")
        print("first printer: %s (%s, %s)" % (p.get("name"), p.get("model"), p.get("network")))
        s, j = call("GET", "/api/v1/printers/%s/status" % pid)
        check("GET /printers/{id}/status 200", s == 200, "status=%d" % s)
        check("printer status has no secrets", not secret_keys_in(j), repr(secret_keys_in(j))[:200])
        if p.get("network") == "lan":
            for route in ("tasks", "files"):
                s, j = call("GET", "/api/v1/printers/%s/%s" % (pid, route))
                check("LAN printer /%s -> 501 unsupported_on_lan" % route,
                      s == 501 and j.get("error") == "unsupported_on_lan",
                      "status=%d %r" % (s, j.get("error")))
    else:
        skip("per-printer routes", "no printer added in the slicer")
else:
    skip("GET /printers", "no 'printers' capability")

if "canvas" in caps and printers:
    pid = urllib.parse.quote(str(printers[0].get("id")), safe="")
    s, j = call("GET", "/api/v1/printers/%s/canvas" % pid)
    if s == 200:
        trays = [t for u in j.get("units") or [] for t in u.get("trays") or []]
        check("canvas trays have ids A1.. and #rrggbb colours",
              all(str(t.get("id", ""))[:1].isalpha() and (t.get("color") is None or is_hex_colour(t.get("color")))
                  for t in trays), json.dumps(trays)[:200])
        check("canvas has no secrets", not secret_keys_in(j), repr(secret_keys_in(j))[:200])
    else:
        check("GET /printers/{id}/canvas answers 200 or a known 409/504",
              s in (409, 504) and j.get("error") in ("canvas_unsupported", "printer_offline",
                                                    "printer_timeout"),
              "status=%d %r" % (s, j.get("error")))
    s, j = call("GET", "/api/v1/printers/%s/canvas/mapping" % pid)
    check("GET /printers/{id}/canvas/mapping answers",
          (s == 200 and isinstance(j.get("filaments"), list)) or (s in (409, 504) and j.get("error")),
          "status=%d %r" % (s, j.get("error")))
elif "canvas" in caps:
    skip("GET /printers/{id}/canvas", "no printer added in the slicer")
else:
    skip("GET /printers/{id}/canvas", "no 'canvas' capability")

if "print_control" in caps and st.get("print_control_enabled") is False:
    # Off (the default): every print route must refuse before doing anything, so these calls
    # are safe. With print control on, nothing here touches the print routes.
    s, j = call("POST", "/api/v1/printers/no-such-printer/print/prepare", {})
    check("print control off: prepare -> 403 print_control_disabled",
          s == 403 and j.get("error") == "print_control_disabled", "status=%d %r" % (s, j.get("error")))
    s, j = call("GET", "/api/v1/uploads/no-such-upload")
    check("print control off: uploads -> 403 print_control_disabled",
          s == 403 and j.get("error") == "print_control_disabled", "status=%d %r" % (s, j.get("error")))
elif "print_control" in caps:
    skip("print routes", "print control is on in Preferences: the check leaves the printer alone")
else:
    skip("print routes", "no 'print_control' capability")

# ---------------------------------------------------------------- summary
print("\n=== summary ===")
bad_n = sum(1 for _, ok, _ in results if not ok)
for nme, ok, d in results:
    if not ok: print("  FAILED: " + nme + "  | " + d)
print("%d checks, %d failed, %d skipped" % (len(results), bad_n, len(skipped)))
sys.exit(1 if bad_n else 0)
