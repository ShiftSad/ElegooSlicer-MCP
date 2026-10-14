"""Prepare a throw-away ElegooSlicer data dir for automated Remote API tests.

Usage: py -3 -I tools/remote-api/phase1_setup.py <datadir> [port]

- enables the Remote API (localhost only) with a fresh token, written to <datadir>/api_token.txt
- marks the Beginner Guide as shown (its modal would block the GUI during unattended tests)
- installs the Elegoo Centauri Carbon 2 (0.4 nozzle) printer model, like the setup wizard would
- recomputes the "# MD5 checksum" line ElegooSlicer appends to the .conf

Never point this at %APPDATA%\\ElegooSlicer (the user's real profile).
"""
import hashlib
import json
import os
import secrets
import sys

datadir = sys.argv[1]
port = sys.argv[2] if len(sys.argv) > 2 else "13130"
if "appdata" in os.path.abspath(datadir).lower():
    sys.exit("refusing to touch a profile under AppData")

conf_path = os.path.join(datadir, "ElegooSlicer.conf")
text = open(conf_path, encoding="utf-8").read()
conf = json.loads(text[: text.rfind("}") + 1])  # drop the trailing checksum line

token_path = os.path.join(datadir, "api_token.txt")
token = open(token_path).read().strip() if os.path.exists(token_path) else secrets.token_hex(16)

app = conf.setdefault("app", {})
app["beginner_guide_shown"] = True
app["remote_api_enabled"] = True
app["remote_api_bind_lan"] = False
app["remote_api_notify"] = True
app["remote_api_port"] = port
app["remote_api_token"] = token

models = conf.setdefault("models", [])
if not any(m.get("model") == "Elegoo Centauri Carbon 2" for m in models):
    models.append({"vendor": "Elegoo", "model": "Elegoo Centauri Carbon 2", "nozzle_diameter": "0.4"})

body = json.dumps(conf, indent="\t", ensure_ascii=False)
md5 = hashlib.md5(body.encode("utf-8")).hexdigest().upper()
with open(conf_path, "w", encoding="utf-8", newline="\n") as f:
    f.write(body + "\n# MD5 checksum " + md5 + "\n")
with open(token_path, "w") as f:
    f.write(token)
print("configured", conf_path, "port", port)
