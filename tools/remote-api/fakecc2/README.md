# Fake Elegoo Centauri Carbon 2 (fakecc2)

A stand-in CC2 for testing ElegooSlicer's printer features (device tab, Send/Print, CANVAS
sync, pause/stop, the Remote API printer routes and the MCP printer tools) without hardware.
It speaks the protocol of the **elegoo-link SDK** the slicer links (commit `46c7b81`): every
topic, JSON field, method id and error code below comes from the SDK's CC2 client code, with
file:line references. Pure Python 3 standard library; nothing to install.

Verified on 2026-10-14 against the **real SDK library** (`elegoolink.lib` from the slicer's deps
build, linked into `sdk_check/`) and the **real Paho MQTT C++ 1.5.3** client (`paho_check/`):
both pass every check. See [Verification](#verification).

## Quick start

```bat
:: 1. start the fake (leave it running; Ctrl+C stops it)
py -3 -I tools\remote-api\fakecc2\fakecc2.py

:: 2. (optional) prove it works: 146 checks on private ports, ~35 s
py -3 -I tools\remote-api\fakecc2\selftest.py
```

## Using it with the slicer

1. Prepare a **test** data dir once (never `%APPDATA%\ElegooSlicer`):
   `py -3 -I tools\remote-api\phase1_setup.py <test-datadir>` (installs the CC2 printer model and the API).
2. With the app **closed**, register the fake printer:
   `py -3 -I tools\remote-api\fakecc2\add_fake_printer.py <test-datadir>`
   It writes `<test-datadir>\user\printer_list.json` (backup kept next to it) with one LAN
   printer: name "Fake CC2", model "Elegoo Centauri Carbon 2", host `127.0.0.1`, access code
   `FAKE42`, serial `FAKECC2SN0001`. Re-running replaces it; `--remove` deletes it. Once
   connected, the slicer takes the printer name from the fake (`--hostname`, see
   `PrinterManager.cpp:1234`).
3. Start the fake: `py -3 -I tools\remote-api\fakecc2\fakecc2.py` (add `--print-seconds 300` for slower prints).
4. Start the app: `build\ElegooSlicer\elegoo-slicer.exe --datadir <test-datadir>`.
   `PrinterManager`'s monitor loop connects saved printers every 10 s
   (`PrinterManager.cpp:948-1033`), so "Fake CC2" should turn online within ~10 s. The fake's
   console shows the connection, registration and every request. (Not yet tried with the app
   itself: tonight's checks drove the same SDK calls the app makes, see [Verification](#verification).)
5. Pick an "Elegoo Centauri Carbon 2 0.4 nozzle" printer preset, slice, Print/Send: the G-code
   lands in `tools\remote-api\fakecc2\received\` and the print is simulated (preheat -> leveling ->
   printing with progress -> completed).
6. Inspect or steer it: `curl http://127.0.0.1:18080/state`,
   `curl -X POST -d "{\"state\":\"busy\"}" http://127.0.0.1:18080/state` (see [Control endpoint](#control-endpoint)).

Two slicer behaviours that look like a broken fake but are not:
- **Start the fake with the same `--sn` and `--access-code` you gave `add_fake_printer.py`.** After
  connecting, `PrinterManager::connectToPrinter` compares the entry's serial number and mainboard id
  with the `sn` of the 1001 answer (`PrinterManager.cpp:1209-1222`); on a mismatch it marks the
  printer `ID_NOT_MATCH`, and a refused access code marks it `AUTH_ERROR` (`:1156-1163`). The
  monitor loop never retries a printer in either state (`:997-999`) until the app restarts (the
  state is not saved in `printer_list.json`) or the printer is edited in the device tab.
- Changing `access_code` through the control endpoint while the slicer is connected does nothing
  until the connection drops (`kick_clients`); the SDK then retries with the old code every 5 s and
  gets CONNACK 5 each time.

Adding it through the GUI should work too: the SDK's discovery finds the fake (verified with the
real SDK: UDP reaches it through the loopback adapter's broadcast address 127.255.255.255); type
access code `FAKE42`.

## Ports

All bound to `127.0.0.1` by default (`--host`; anything else exposes a printer with a fixed
access code on your network and prints a warning).

| Port | Service | Fixed by the SDK? |
|---|---|---|
| 1883/tcp | MQTT broker + CC2 logic | **Yes**: `tcp://<host>:1883` hard-coded (`elegoo_fdm_cc2_protocol.cpp:25`). Only one fake per machine. |
| 80/tcp | printer HTTP: `/system/info`, `PUT /upload`, `/download` | **No**: the SDK uses `UrlUtils::extractEndpoint(host)` (`utils.cpp:565-580`), which keeps a non-default port. Run `fakecc2.py --http-port 8081` and `add_fake_printer.py --http-port 8081` (host becomes `127.0.0.1:8081`). Verified end to end with the SDK. |
| 8080/tcp | MJPEG camera | Not in the SDK; the MCP's `get_camera_snapshot` uses `http://<host>:8080/` (CONTRACT.md). A host with `:port` must be stripped by whoever builds that URL. |
| 52700/udp | discovery | Yes (`elegoo_cc2_adapters.h:164`). |
| 18080/tcp | control/inspection (test helper, not a real printer port) | `--control-port` |

On 2026-10-14 `netstat -ano` showed 80, 1883, 8080, 52700 and 18080 all free on this machine, and
binding 127.0.0.1:80 works without admin rights on Windows. If something (IIS, a WSL port proxy)
takes port 80 later, use `--http-port` as above. The fake refuses to start when a port is taken
(it does not use SO_REUSEADDR on Windows, so a stale fake cannot silently share a port).

## Options

`py -3 -I tools\remote-api\fakecc2\fakecc2.py --help`

| Option | Default | Meaning |
|---|---|---|
| `--sn` | `FAKECC2SN0001` | serial number; MQTT topics are `elegoo/<sn>/...` |
| `--access-code` | `FAKE42` | MQTT password and HTTP `X-Token` (not the SDK default `123456`, so a missing code is caught) |
| `--hostname` / `--model` / `--firmware` | `Fake CC2` / `Centauri Carbon 2` / `02.01.00.00` | what 1001, `/system/info` and discovery report |
| `--print-seconds` | 60 | real seconds a print takes (durations are reported on the G-code's own time scale) |
| `--preheat-seconds` / `--leveling-seconds` | 4 / 3 | phase lengths (leveling only when the job asks for `printer_check`) |
| `--completed-hold-seconds` | 10 | "print completed" shown this long before idle; 0 keeps it |
| `--tick` | 0.5 | state machine and status push interval |
| `--max-clients` | 4 | registered client slots; more get `"too many clients"` |
| `--rate-limit` | off | emulate the firmware cooldown (3+ requests within 1 s -> answers dropped for 5 s) |
| `--received-dir` | `tools\remote-api\fakecc2\received` | where uploads are stored |
| `--no-camera`, `--no-discovery`, `--camera-fps`, `--verbose` | | `--verbose` logs every MQTT message and HTTP request |

## What is emulated

### MQTT (the printer is the broker)
- MQTT 3.1.1 and 3.1 (Paho's default first tries 3.1.1, then 3.1). User `elegoo`, password =
  access code (`elegoo_fdm_cc2_protocol.cpp:143-190`); wrong code -> CONNACK 5, which the SDK maps
  to `INVALID_ACCESS_CODE` (`error_handler.cpp:67-85`). Keep-alive 60 s, clean session, QoS 1
  subscribe/publish as the SDK does (`mqtt_protocol.cpp:79,160-161,225,259`). `+`/`#` wildcards,
  UNSUBSCRIBE, PINGREQ, client-id takeover, keep-alive timeout.
- Topics (`elegoo_fdm_cc2_protocol.cpp:192-211`): requests on `elegoo/<sn>/<client_id>/api_request`,
  answers on `elegoo/<sn>/<client_id>/api_response`, status pushes on `elegoo/<sn>/api_status`.
- Registration (`:218-307`): `elegoo/<sn>/api_register` `{"client_id","request_id"}` ->
  `elegoo/<sn>/<request_id>/register_response` `{"client_id","error":"ok"}` or
  `"too many clients"`. Requests from unregistered clients are not answered (as on the printer);
  registration ends when the MQTT connection closes.
- Heartbeat (`:319-355`): `{"type":"PING"}` on the request topic -> `{"type":"PONG"}` on the
  response topic.

### Requests
Envelope `{"id": int, "method": int, "params": {}}` -> `{"id", "method", "result": {..., "error_code"}}`;
the SDK matches by `id` and needs `method` present (`elegoo_fdm_cc2_message_adapter.cpp:234-358,420-452`).

| Method | Params (as the SDK builds them) | Answer |
|---|---|---|
| 1001 attributes | `{}` | `hostname, machine_model, sn, ip, protocol_version, software_version.ota_version` (`:453-545`) |
| 1002 status | `{}` | full status (below) |
| 1020 start print | `storage_media, filename, config{delay_video, printer_check, print_layout, bedlevel_force, slot_map[{t, canvas_id, tray_id}]}` (`:59-98`) | 0; 1009 busy (not idle and not "completed"); 1021 file not in `received\` (or storage not `local`); 1026 when `missing_bed_leveling` is set and `printer_check` without `bedlevel_force` (the SDK retries with the force flag, `ElegooLink.cpp:643-649`) |
| 1021 pause | `{}` | 0 when preheating/leveling/printing; else 1010 |
| 1022 stop | `{}` | 0 when a job is running or paused; else 1010 |
| 1023 resume | `{}` | 0 when paused; else 1010. **The SDK never sends it**: 1023 is commented out (`:1399`), so `resumePrint` returns `OPERATION_NOT_IMPLEMENTED` without touching the network (verified with the real SDK). The fake answers it for other clients (pycentauri). |
| 1042 video | `{}` | `{"url": "http://<host>:<camera>/?action=stream"}` (shape from pycentauri; commented out in the SDK) |
| 1043 rename | `{hostname}` | 0 (`:203-211`) |
| 2004 auto refill | `{auto_refill: bool}` | 0; 1003 when not a bool (`:176-183`) |
| 2005 canvas | `{}` | `canvas_info` (`:1216-1262`) |
| anything else | | 1001 "unknown interface" |

Every answer needs `result.error_code` (without it the SDK reports UNKNOWN_ERROR, `:337-341`);
the SDK maps 1009 -> PRINTER_BUSY, 1021 -> PRINTER_PRINT_FILE_NOT_FOUND, 1026 ->
PRINTER_MISSING_BED_LEVELING_DATA, 1000 -> PRINTER_ACCESS_DENIED, 1001/1003 ->
PRINTER_INVALID_PARAMETER and everything else (1010 included) to PRINTER_UNKNOWN_ERROR
(`:1425-1475`). SDK timeouts: 10 s for 1020, 15 s for 1021/1022, 3 s for 1001/1002/2004/2005/1043
(`base_printer.cpp:775-854`).

Answer timing: the fake answers at once. A real CC2 answers pause/stop/resume only after the
movement is done (pycentauri measured more than 15 s for a resume), so the SDK can report
OPERATION_TIMEOUT for a command that worked; `delay_next` (control endpoint) reproduces that.

### Status
- 1002 returns the full status in the shape pycentauri captured from a CC2 (`machine_status`,
  `print_status`, `extruder`, `heater_bed`, `ztemperature_sensor`, `fans`, `gcode_move`, `led`,
  `tool_head`, `external_device`, `canvas_info`, `exception`), the fields the SDK reads in
  `handlePrinterStatus` (`:547-1214`).
- Every change is pushed as `{"id": n, "method": 6000, "result": <only what changed>}` on
  `api_status`; ids are consecutive (the SDK counts gaps, `:549-577`); nested objects are diffed
  key by key and arrays/`exception_code` sent whole, matching the SDK's merge (`:1303-1348`).
- State machine (`machine_status` status/sub_status as mapped at `:639-962`):

  | Phase | status / sub_status | Slicer shows |
  |---|---|---|
  | idle | 1 / 0 | idle |
  | preheat (bed, then nozzle) | 2 / 1405, 2 / 1045 | printing |
  | auto leveling (if `printer_check`) | 2 / 2901 | printing |
  | printing, progress 0->100 | 2 / 2075 | printing |
  | pausing -> paused (1 s) | 2 / 2501 -> 2 / 2502 | pausing -> paused |
  | resuming (1 s) | 2 / 2401 | printing |
  | stopping -> stopped (1 s each) -> idle | 2 / 2503 -> 2 / 2504 -> 1 / 0 | printing |
  | completed, progress 100 -> idle after the hold | 2 / 2077 -> 1 / 0 | print completed |
  | busy (control only) | 10 / 2801 (homing) | homing |

- Progress, layers and times: `total_layer` and the estimated time are read from the G-code
  (`; total layer number:` in the header, `; estimated printing time (normal mode) =` which
  ElegooSlicer writes just before the CONFIG_BLOCK at the end of the file for non-Bambu printers,
  `GCode.cpp:3593-3606`); the job runs in `--print-seconds` but reports `print_duration`,
  `remaining_time_sec`, `total_duration` on the G-code's time scale.
- Types matter: the SDK's `JsonUtils::safeGetInt/safeGetInt64/safeGetBool/safeGetString` return
  their default when the JSON type is not exactly right (12.0 is not an integer, 1 is not a bool).
  Integers: `machine_status.status/sub_status/progress/exception_status[]`, `print_status.*_duration/
  remaining_time_sec/total_layer/current_layer`, every tray's `tray_id/min_nozzle_temp/
  max_nozzle_temp/status`, `active_canvas_id/active_tray_id`; bools: `auto_refill`,
  `external_device.camera/u_disk`. The self-test checks these on every status it sees.
- A job with a `slot_map` loads its first tray: `active_canvas_id/active_tray_id` set and that
  tray's status 1 -> 2 (loaded); back to 1 / -1 when the job ends.

### CANVAS (default)
One unit (`canvas_id` 0, connected), trays 0-3: ELEGOO PLA `#FF0000`, ELEGOO PLA `#FFFFFF`,
ELEGOO PLA Matte `#000000` (status 1 = in the slot), tray 3 empty (status 0). `auto_refill` false,
`active_tray_id` -1. Tray status: 0 empty, 1 preloaded, 2 loaded (`ElegooLink.cpp:740-757`).
Change it with the control endpoint (`trays`, `canvas_connected`, `canvas_info`, `auto_refill`).

How the slicer shows it: tray names come from the position, not from `tray_id`: unit 1 is A,
unit 2 is B, and the trays in `tray_list` order are A1, A2, ... (`PrinterMmsManager.cpp:266-280`),
so the defaults are A1..A3 preloaded and A4 empty. A tray without `filament_type` or
`filament_name` becomes "preloaded/loaded, unknown filament". Colours must be `#RRGGBB` (or 6 hex
digits without `#`, `StandardColorMatcher::parse_hex`) or the colour match fails. The send dialog
passes the tray's `tray_id` back in 1020's `slot_map`. Tray ids are 0-based and `active_tray_id`
-1 means none, as pycentauri captured from a CC2 with a 4-slot CANVAS; the SDK's header comment
("0 means no tray, 1-4 for trays", `types/printer.h:394,421`) disagrees and looks stale.

### HTTP (:80)
- `GET /system/info?X-Token=<code>` (the SDK sends query and header, `elegoo_fdm_cc2_protocol.cpp:52-134`)
  -> `{"error_code":0,"system_info":{"sn":...}}`; wrong code -> 401 (SDK: `INVALID_ACCESS_CODE`).
  Only used when the printer entry has no serial number. The fake accepts the token from the query
  or the header everywhere; pycentauri saw firmware 01.03.02.51 reject the header form on
  `/system/info` (harmless: the SDK sends both there, and only the header on uploads).
- `PUT /upload` (`elegoo_fdm_cc2_http_transfer.cpp:23-303`): raw body chunks of up to 1 MiB on one
  keep-alive connection, headers `Content-Range: bytes a-b/total`, `X-File-Name` (raw UTF-8),
  `X-File-MD5` (whole-file MD5, every chunk), `X-Token`. Answer `{"error_code":0}`; 401 +
  1000 for a wrong token; 9000 for an out-of-order chunk; 9004 when the finished file's MD5 does
  not match (file discarded). Chunks are kept as `<name>.part` until complete and verified.
- `GET /download?X-Token=<code>?file_name=<name>` (the SDK's URL really has two `?`, `:431-473`).

### Discovery (:52700/udp)
`{"id":0,"method":7000}` -> `{"id":0,"result":{"host_name","machine_model","sn","token_status":1,"lan_status":1}}`
(`elegoo_fdm_cc2_discovery_strategy.cpp:8-110`): the SDK then lists it as a CC2 at the sender
IP, auth `accessCode`, LAN mode, id `lan_<sn>`. The SDK broadcasts to every adapter's broadcast
address (`printer_discovery.cpp:433-493`, `utils.cpp:70-106`); on Windows that includes
127.255.255.255, which reaches a socket bound to 127.0.0.1 (verified with the real SDK).

### Camera (:8080)
`multipart/x-mixed-replace; boundary=frame` on any path, no auth, a 160x120 JPEG ("FAKE CC2 /
test camera", embedded in `camera_frame.py`) repeated at `--camera-fps`.

## Control endpoint

`GET http://127.0.0.1:18080/` prints this help; `GET /state` returns everything: phase,
`machine_status`, `expected_slicer_status`, `print_status`, task and last task, `canvas_info`,
MQTT connections, registered clients, received files (name, size, md5, sha256), uploads in
progress, **every MQTT request received** (`requests`: time, client, id, method, name, params,
error_code/outcome), ping and discovery counters, settings.

`POST /state` with a JSON object (keys applied in order, `reset` first):

| Key | Effect |
|---|---|
| `reset: true` | back to the start-up state, like a reboot: MQTT clients are dropped (the SDK reconnects and registers again); received files are kept |
| `offline: true/false` | drop and refuse MQTT, HTTP, UDP and camera like a powered-off printer |
| `kick_clients: true` | drop MQTT connections (the SDK reconnects by itself) |
| `state: idle/busy/printing/paused/completed` | jump to a state without a real job |
| `machine_status: {"status":n,"sub_status":m}` / `null` | raw override of machine_status |
| `progress: 0-100` | move the current print |
| `exceptions: [803, ...]` | `machine_status.exception_status` + `exception.exception_code` |
| `auto_refill`, `canvas_connected` | booleans |
| `trays: [{"tray_id":1, "canvas_id":0, "filament_color":"#00FF00", ...}]` | patch trays (fields as in 2005) |
| `canvas_info: {...}` | replace the whole 2005 `canvas_info` |
| `access_code`, `hostname`, `max_clients`, `rate_limit`, `missing_bed_leveling`, `camera` | settings |
| `fail_next: {"method":1020, "error_code":1009}` | answer that method's next request with the error |
| `drop_next: 1022` | leave that method's next request unanswered and NOT carried out (the SDK times out) |
| `delay_next: {"method":1022, "seconds":20}` | carry out that method's next request at once but answer it that much later (a real CC2 answers pause/stop after parking; past 15 s the SDK reports OPERATION_TIMEOUT although the printer did it) |
| `print_seconds`, `preheat_seconds`, `leveling_seconds`, `completed_hold_seconds` | timings |
| `clear_log: true`, `delete_files: ["name.gcode"]` | housekeeping |

Errors: the CC2 has no "error" machine state in the SDK's mapping; printer errors arrive as
exception codes, so use `{"exceptions": [<code>]}` (the slicer gets them as the printer's
exception list) or e.g. `{"machine_status": {"status": 14}}` for an emergency stop.

Example from cmd.exe: `curl -X POST -d "{\"offline\":true}" http://127.0.0.1:18080/state`
(from PowerShell: `Invoke-RestMethod -Method Post -Uri http://127.0.0.1:18080/state -Body '{"offline":true}'`).

## What is NOT emulated (and where the real printer may differ)

- **Retained messages, will messages, persistent sessions, MQTT 5, TLS**: the SDK uses none
  (clean session, no will, plain `tcp://`).
- **Registration expiry**: a real CC2 silently stops answering a client that stops sending PINGs
  for several minutes (pycentauri PROTOCOL.md). The SDK pings every 10 s
  (`elegoo_fdm_cc2_protocol.cpp:326`), so the fake keeps registrations until the connection closes.
- **Rate limiting** is off by default (`--rate-limit` / `rate_limit` turns it on).
- **Methods the SDK has commented out** (`elegoo_fdm_cc2_message_adapter.cpp:1399-1417`):
  1026-1031 (home, move, temperature, light, fans, speed), 1036 (history), 1044/1046/1047/1048
  (files, disk), 1061, plus 1057/1058 (printer downloads a file from a URL, cloud flow) answer
  `1001` here although a real printer answers some of them.
- **Storage**: only internal storage (`local`); u-disk/sd-card start requests answer 1021 and
  `/download/udisk|sdcard` 404.
- **Status during an upload**: the fake stays idle while receiving a file; a real CC2 may report
  file-transferring (status 11). The slicer checks "busy" right before uploading and before
  starting, so this matters only for those checks.
- **Field values the SDK does not read** are guesses: `print_status.state` words, the extra
  `system_info` fields, `ip`/`protocol_version` in 1001, temperatures (instant cool-down, linear
  heat-up), head positions. The real firmware sends positions in `gcode_move` (pycentauri), while
  the SDK reads `gcode_move_inf`, so the slicer shows 0,0,0 for both the real and the fake printer.
- **Where pushes go**: the fake publishes 6000 pushes on `elegoo/<sn>/api_status` only, which is
  all the SDK needs. pycentauri (subscribed to `elegoo/<sn>/#`) saw them on the clients'
  `api_response` topics as well. If the firmware really sends each push to both topics, the SDK
  (subscribed to both) gets every push twice and logs "Non-continuous status event detected" for
  each duplicate; the merged status is still right. Not emulated because unconfirmed.
- **Answer timing** of lifecycle commands: immediate unless `delay_next` is used (see Requests).
- **6008 attribute pushes** are never sent (the SDK handles them, `:396-406`; nothing in the
  slicer depends on them), and `chassis_camera.configured` (time-lapse support switch,
  `:1144-1163`) is not reported.
- **Discovery**: pycentauri found no broadcast discovery on CC2 firmware 01.03.02.51; the fake
  follows the SDK (method 7000), so newer firmware is assumed.
- **One printer per machine**: MQTT is always port 1883.

## Verification

| Check | What it proves | Result (2026-10-14) |
|---|---|---|
| `py -3 -I tools\remote-api\fakecc2\selftest.py` | 146 checks with an independent hand-written MQTT client following the SDK's sequence: discovery, `/system/info`, auth (3.1.1 and 3.1), subscribe, registration, 1001/1002/2005/2004/1043/1042, PING, upload (3 chunks, UTF-8 name, wrong token/MD5/offset), download (incl. wrong token, u-disk), start/pause/resume/stop/complete, busy/1010/1021/1026 errors, push continuity and delta merge equal to a fresh 1002, exception_code cleared by a push, JSON types of every field the SDK reads (idle, printing, paused, completed, 2005), trays as the slicer names them (A1..A4, loaded during a print), late answers (`delay_next`), fault injection, client slots and re-registration, client-id takeover, access code changed while connected, camera, malformed input on every port, keep-alive timeout, offline/kick/reset/reconnect, and `add_fake_printer.py` (keys and JSON types checked against the slicer's `convertPrinterNetworkInfoToJson`/`convertJsonToPrinterNetworkInfo`/`savePrinterList` sources, `getPrinterType` -> CC2, serial = mainboard id = the fake's sn, merge/replace/`--http-port`/`--no-serial`/`--remove`, refuses broken JSON and AppData). Starts its own fake on ports 21880-21883/52701; `--running` tests the fake on the default ports and cleans up after itself. | 146/146 (also with `--running` on ports 80/1883/8080/52700) |
| `py -3 -I tools\remote-api\fakecc2\paho_check\build_and_run.py` | the broker works with the real Paho MQTT C++ 1.5.3 library from the slicer's deps, configured like `MqttProtocol::Impl::doConnect` | 11/11 |
| `py -3 -I tools\remote-api\fakecc2\sdk_check\build_and_run.py` | **the real elegoo-link SDK** (`elegoolink.lib`, the slicer's) against the fake, with the parameters ElegooSlicer's wrapper builds: discovery, wrong code (both paths), connect via `/system/info`, attributes and capabilities, status, rename (1043) read back through 1001, CANVAS, auto refill, upload with progress, start, the print status as the SDK parses it (total layers, current + remaining time = the G-code's estimate, active tray and loaded tray from the CANVAS block), busy, pause, resume = not implemented, stop, a stop answered after 17 s (SDK: OPERATION_TIMEOUT, yet the printer stopped and the SDK keeps working), reconnect with the stored serial, fake offline -> disconnected event -> back online -> the SDK reconnects by itself (5 s loop). `--host 127.0.0.1:8081` (fake started with `--http-port 8081`) checks the non-default HTTP port. The SDK's own debug log is written next to the exe: no protocol warnings (no "Non-continuous status event", no "No cached full status"); the only warnings are the ones the checks provoke (busy, wrong code, timeout, offline). | 37/37 (host `127.0.0.1` and `127.0.0.1:8081`, 2026-10-14 after the verification pass) |

The two C++ checks need the slicer's deps build and the VS 2022 Build Tools (the same `vcvars64`
as `tools\remote-api\build.cmd`); they build into a temp dir and write nothing into the repositories.
Start the fake first (for `sdk_check`, `--print-seconds 20` or more so pause/stop happen mid-print;
it toggles the fake offline through the control port, `--control-port` if not 18080).

Side finding from the SDK log: elegoo-link logs the connect parameters with the **access code in
clear text** (`lan_service.cpp:344-347` masks printerId and serialNumber only), so the slicer's
`<datadir>/log/elegoolink.log` contains the real printer's access code.

## Files

| File | |
|---|---|
| `fakecc2.py` | entry point: options, binds the servers, runs the ticker |
| `mqtt_broker.py` | minimal MQTT 3.1.1 broker (asyncio) |
| `printer.py` | CC2 behaviour: registration, requests, state machine, status JSON, uploads, control |
| `http_servers.py` | printer HTTP (:80), camera (:8080), control (:18080) |
| `camera_frame.py` | the embedded JPEG |
| `add_fake_printer.py` | registers the fake in a test data dir's `user\printer_list.json` |
| `selftest.py` | protocol self-test (independent MQTT client) |
| `paho_check/`, `sdk_check/` | C++ cross-checks against the real Paho library and the real SDK |
| `received\` | uploaded files |
