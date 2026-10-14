"""Runtime check of the D8 modal-dialog guards and the calibration types: requests that would
make the window ask a question or show a message box must answer without opening any window.

Usage: py -3 -I tools/remote-api/phase5_dialogs_check.py <token> <app pid> <scratch dir>
Needs a running ElegooSlicer MCP (TEST --datadir) on 127.0.0.1:13130. It replaces the project.
After each step it lists the app's visible top-level windows (tools/remote-api/winshot.ps1 logic via
PowerShell): only the main window may be there.
"""
import json
import os
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:13130/api/v1"
TOKEN, PID, OUT = sys.argv[1], int(sys.argv[2]), os.path.abspath(sys.argv[3])
failed = []

WINDOWS_PS = r"""
Add-Type @"
using System; using System.Collections.Generic; using System.Runtime.InteropServices; using System.Text;
public static class W {
  public delegate bool P(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] public static extern bool EnumWindows(P cb, IntPtr l);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out R r);
  [StructLayout(LayoutKind.Sequential)] public struct R { public int L, T, Ri, B; }
  public static List<string> List(uint pid) { var o = new List<string>();
    EnumWindows((h, l) => { uint p; GetWindowThreadProcessId(h, out p);
      if (p == pid && IsWindowVisible(h)) { R r; GetWindowRect(h, out r);
        if (r.Ri - r.L >= 50 && r.B - r.T >= 50) { var s = new StringBuilder(256); GetWindowText(h, s, 256); o.Add(s.ToString()); } }
      return true; }, IntPtr.Zero); return o; } }
"@
[W]::List(%d) | ForEach-Object { $_ }
"""


def windows():
    r = subprocess.run(["pwsh", "-NoProfile", "-Command", WINDOWS_PS % PID], capture_output=True, text=True)
    return [l for l in r.stdout.splitlines() if l.strip() != "" or True]


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
    print(("PASS  " if cond else "FAIL  ") + name + (f"  | {detail}" if detail else ""), flush=True)


def short(r):
    s = json.dumps(r, ensure_ascii=False)
    return s if len(s) < 400 else s[:400] + "..."


def only_main_window(step):
    time.sleep(1.5)
    w = windows()
    check(f"no dialog after {step}", len(w) == 1, str(w))


def cube_stl(path, size):
    """A binary STL cube of edge `size` (in file units)."""
    v = [(x, y, z) for x in (0, size) for y in (0, size) for z in (0, size)]
    faces = [(0, 1, 3), (0, 3, 2), (4, 6, 7), (4, 7, 5), (0, 4, 5), (0, 5, 1),
             (2, 3, 7), (2, 7, 6), (0, 2, 6), (0, 6, 4), (1, 5, 7), (1, 7, 3)]
    with open(path, "wb") as f:
        f.write(b"\0" * 80 + struct.pack("<I", len(faces)))
        for a, b, c in faces:
            f.write(struct.pack("<3f", 0, 0, 0))
            for i in (a, b, c):
                f.write(struct.pack("<3f", *v[i]))
            f.write(b"\0\0")


def reset():
    for o in call("GET", "/objects")[1].get("objects", []):
        call("DELETE", f"/objects/{o['id']}")


os.makedirs(OUT, exist_ok=True)
only_main_window("start")

# Model-load questions: a 1.5 unit cube (volume < 8 mm3: the window asks "too small, maybe inches?")
# and a 3000 unit cube (over 10x the bed: asks "too large, scale down?"). The API answers No and lists them.
tiny, huge = os.path.join(OUT, "tiny_cube.stl"), os.path.join(OUT, "huge_cube.stl")
cube_stl(tiny, 1.5)
cube_stl(huge, 3000)
reset()
s, r = call("POST", "/model", {"path": tiny})
check("load a tiny model -> 200 with skipped_dialogs", s == 200 and r.get("skipped_dialogs"), f"{s} {short(r)}")
only_main_window("tiny model")
reset()
s, r = call("POST", "/model", {"path": huge})
check("load a huge model -> 200 with skipped_dialogs", s == 200 and r.get("skipped_dialogs"), f"{s} {short(r)}")
only_main_window("huge model")
reset()

# A settings combination the Print tab questions (spiral vase with several walls): refused whole.
before = call("GET", "/config?keys=spiral_mode,wall_loops")[1]
s, r = call("PUT", "/config", {"spiral_mode": True, "wall_loops": 3})
check("spiral vase + 3 walls -> 422 config_rejected with the question", s == 422 and r.get("error") == "config_rejected"
      and "_question" in r.get("errors", {}) and r.get("applied") == [], f"{s} {short(r)}")
after = call("GET", "/config?keys=spiral_mode,wall_loops")[1]
check("nothing changed", before == after, f"{before} -> {after}")
only_main_window("spiral question")

# Every calibration type, discarding the project, without a dialog.
for kind in ("pressure_advance", "temperature", "retraction", "max_volumetric_speed", "vfa", "flow_rate"):
    end = time.time() + 60
    while time.time() < end and not call("GET", "/jobs/status")[1].get("idle", True):
        time.sleep(0.5)
    s, r = call("POST", "/calibration", {"type": kind, "params": {}, "discard_project": True})
    check(f"calibration {kind}", s == 200 and r.get("objects"), f"{s} {short(r)}")
    only_main_window(f"calibration {kind}")
    s, r = call("POST", "/slice")
    end = time.time() + 240
    st = None
    while time.time() < end:
        st = call("GET", "/slice/status")[1].get("state")
        if st in ("done", "error"):
            break
        time.sleep(1)
    check(f"calibration {kind} slices", st == "done", str(st))
    only_main_window(f"slicing {kind}")

s, r = call("POST", "/calibration", {"type": "temperature", "params": {"start": "hot"}, "discard_project": True})
check("bad calibration param -> 422 bad_params", s == 422 and r.get("error") == "bad_params", f"{s} {short(r)}")

print("RESULT:", "ALL PASS" if not failed else f"{len(failed)} FAILED: {failed}")
sys.exit(1 if failed else 0)
