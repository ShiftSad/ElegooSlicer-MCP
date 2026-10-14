"""Turn the Remote API's "Allow sending and controlling prints" preference on or off in a TEST
data dir (the app must be closed; it rewrites the .conf on exit).

Usage: py -3 -I tools/remote-api/set_print_control.py <datadir> on|off
"""
import hashlib
import json
import os
import sys

datadir, state = os.path.abspath(sys.argv[1]), sys.argv[2]
if "appdata" in datadir.lower():
    sys.exit("refusing to touch a profile under AppData")
if state not in ("on", "off"):
    sys.exit("state must be on or off")

conf_path = os.path.join(datadir, "ElegooSlicer.conf")
text = open(conf_path, encoding="utf-8").read()
conf = json.loads(text[: text.rfind("}") + 1])  # drop the trailing checksum line
app = conf.setdefault("app", {})
if state == "on":
    app["remote_api_allow_print_control"] = True
else:
    app.pop("remote_api_allow_print_control", None)

# Same layout and checksum line as tools/remote-api/phase1_setup.py.
body = json.dumps(conf, indent="\t", ensure_ascii=False)
md5 = hashlib.md5(body.encode("utf-8")).hexdigest().upper()
with open(conf_path, "w", encoding="utf-8", newline="\n") as f:
    f.write(body + "\n# MD5 checksum " + md5 + "\n")
print(f"remote_api_allow_print_control {state} in {conf_path}")
