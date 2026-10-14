# Overnight contract (2026-10-14): new Remote API routes + MCP tools

Single source of truth for the C++ lines and the Python lines working in parallel. Base spec:
`ElegooSlicer/PORT_PLAN.md` section 6 (routes) and 7 (MCP tools); this file pins the details and
the decisions Victor approved ("tudo que for de fase de aprovação pode seguir"). Where this file
and PORT_PLAN.md disagree, this file wins. Conventions: everything under `/api/v1`, JSON, errors
`{"error": "snake_case", ...details}` (optional `detail`/`hint` strings), filament slots 1-based,
plate indices 0-based.

## Approved decisions
- **422 config writes get an error token**: `PUT /config`, `PUT /objects/{id}/config`,
  `PUT /objects/{id}/volumes/{vid}/config` (and any other atomic config write that answers 422
  with `errors`) add `"error": "config_rejected"` next to `applied`/`errors`. Nothing else changes.
- **D14 (SDK reality)**: for LAN printers `GET /printers/{id}/tasks`, `/files` and
  `/status?raw=1` answer `501 {"error":"unsupported_on_lan"}` *without calling the SDK* (those SDK
  calls are cloud-only). WAN printers call through. CANVAS `temperature_c`, `humidity`,
  `bed_temp`, `external_spool` have no data source: always `null`. `resume` returns
  `501 {"error":"resume_unsupported","hint":"resume on the printer's screen"}` when the SDK says
  OPERATION_NOT_IMPLEMENTED (CC2 LAN in SDK 46c7b81). `DELETE /uploads/{id}` after the transfer
  finished: `409 too_late`. Tray mappings are NOT persisted to `filament_mms_mapping.json`.
- **Phase 4 route**: printer control (pause/resume/stop) calls the SDK singleton
  `elink::ElegooLink::getInstance()` from `RemoteAPIElegoo.cpp` (no edits to the Elegoo
  wrapper/IPC files), only in the `MultiInstanceCoordinator` master instance; a secondary
  instance answers `409 {"error":"secondary_instance","hint":"close the other ElegooSlicer windows"}`
  for every print-control route. Uploads go through the existing `PrinterUploadManager`.
- **D15**: slice breakdown gains purge fields (see "Slice breakdown").
- **D8 part 2**: Orca-inherited modal dialogs reachable from the API are suppressed while
  `api_ui_task_active()` (turned into a notification, or the request refused with 422/409 and a
  reason) - one generic commit per dialog family.
- **D9/D10/D13** are documentation/packaging: telemetry is collected as in the official app and
  uploaded only when logged in (recommend staying logged out); the build is not LAN-only; the
  portable zip uses its own data dir.

## Capabilities (`GET /status` -> `capabilities`)
Existing ones stay. New names, each listed only by builds that serve the routes:
`printers`, `canvas` (incl. sync + mapping), `filaments`, `split`, `undo`, `layer_changes`,
`calibration`, `print_control`, `breakdown_purge`. `GET /status` also gains
`"print_control_enabled": bool` (the preference below).

## Printers (capability `printers`) - RemoteAPIElegoo.cpp
Network/SDK calls never run on the GUI thread or block it; they run on a worker with a timeout
(default 10 s, 20 s for canvas) -> `504 {"error":"printer_timeout"}`. Reads come from
`PrinterManager` cache. `refreshPrinterStatus(id)` at most once per 2 s per printer.

- `GET /printers` -> `{"printers": [Printer], "selected_id": str|null}`.
- `GET /printers/{id}` -> `Printer`; unknown id -> `404 {"error":"printer_not_found"}`.
- `GET /printers/{id}/status` -> `Printer` + `"exceptions": []` (LAN: always empty) +
  `"age_s"` (seconds since the cache was last updated, null if unknown). `?raw=1`: LAN -> 501.
- `GET /printers/{id}/tasks?page=1&size=20`, `GET /printers/{id}/files?page=1&size=20`:
  LAN -> 501; WAN -> `{"items": [...], "total": n}` from the SDK.

`Printer` (WHITELIST - built field by field, never from `convertPrinterNetworkInfoToJson`):
`id, name, model, vendor, host, port (int|null), network ("lan"|"wan"), firmware, connected (bool),
status (string), status_code (int), task, supports`.
- `status`: `offline` when not connected, else from `PrinterStatus`: idle, printing, paused,
  pausing, canceled, self_checking, auto_leveling, pid_calibrating, resonance_testing, updating,
  file_copying, file_transferring, homing, preheating, filament_operating, extruder_operating,
  print_completed, rfid_recognizing, video_composing, emergency_stop, power_loss_recovery,
  initializing, busy, error, id_not_match, auth_error, unknown.
- `task`: `{name: null (LAN), file, progress (0-100), current_s, total_s, estimated_s, begin_ts: null}`
  or `null` when there is no task.
- `supports`: `{canvas, timelapse, auto_leveling, bed_switching, auto_refill, filament_mapping}`.
- Never present anywhere in any response or log: `password, token, accessCode, pinCode,
  extraInfo, username, serialNumber, mainboardId, webUrl`.

## CANVAS (capability `canvas`) - RemoteAPIElegoo.cpp
- `GET /printers/{id}/canvas` -> `Canvas` + `age_s`. Cached >= 5 s per printer and serialised
  per printer (CC2 rate limit: 3+ back-to-back MQTT requests trigger a silent cooldown).
  `409 canvas_unsupported` (no multi-filament support), `409 printer_offline`, `504 printer_timeout`.
- `Canvas`: `{connected, auto_refill, active: {unit, tray}|null, units: [Unit], external_spool: null}`.
- `Unit`: `{id, name, model, connected, temperature_c: null, humidity: null, trays: [Tray]}`.
- `Tray`: `{id ("A1".."A4", "B1".. by unit order), unit, index, status, type, name, vendor,
  color ("#rrggbb"), nozzle_temp: [min, max]|null, bed_temp: null, matched_preset, matched_alias}`.
  `status`: `empty | preloaded | loaded | unknown_filament | error | unknown`.
- `POST /printers/{id}/canvas/sync` body `{"mode"?: "filament_and_color"|"color_only"
  (default filament_and_color, as the GUI), "discard_changes"?: bool}` ->
  `{"synced": n, "slots": [{slot, preset, type, color, tray}], "unknown": [{tray, type, reason}]}`.
  Same result as the GUI's Sync button (UI-free core extracted from `PlaterExt::sync_mms_filament`;
  the GUI keeps its dialogs). A dirty filament preset would open `UnsavedChangesDialog`:
  `409 {"error":"unsaved_filament_changes"}` unless `discard_changes: true`. Marks every plate unsliced.
- `GET /printers/{id}/canvas/mapping[?plate=i]` -> `{"plate", "sliced", "ok", "filaments": [{slot,
  preset, type, color, weight_g|null, tray: {unit, tray, type, color}|null, issues: [...]}]}` with
  issues from `no_tray | type_mismatch | color_mismatch | tray_not_ready`. Same mapping logic as the
  send dialog (shared helper extracted from `PrintSendDialogEx::preparePrintTask`, not copied).

## Filament slots (capability `filaments`) - generic, RemoteAPIProject.cpp
- `GET /filaments` -> `{"count", "max", "slots": [{slot, preset, type, color, used_on_current_plate}]}`.
- `PUT /filaments` `{"count": n, "force"?: bool}` -> same body as GET. Same path as the sidebar
  +/- buttons. Removing a slot used by an object/volume/paint -> `409 {"error":"slot_in_use",
  "used_by": [{object_id, name, volume_id?}]}` unless `force`. `422 bad_count`.
- `PUT /filaments/{slot}` `{"preset"?: str, "color"?: "#rrggbb"}` -> the slot. Preset via the same
  path as the sidebar combo (no dialog); colour via `filament_colour`; flush recomputed when
  `auto_calculate_flush` is on. `422 unknown_preset | incompatible_preset | bad_color`,
  `404 unknown_slot`. Marks every plate unsliced.

## Split (capability `split`) - generic
- `POST /objects/{id}/split` `{"to": "objects"|"parts"}` -> `{"ids": [...]}` (new object ids, or
  the object's volume ids for parts). `422 not_splittable`, `404 unknown_object`. No dialogs.

## Undo / redo (capability `undo`) - generic
- `POST /undo`, `POST /redo` -> `{"ok": bool, "can_undo": bool, "can_redo": bool}`
  (`ok: false` when there was nothing to undo/redo; still 200). `409 slicing`, `409 gizmo_editing`.

## Layer changes (capability `layer_changes`) - generic
- `GET /plates/{i}/layer_changes` -> `{"plate": i, "items": [{z, type, slot?, message?}]}`
  (`type`: `tool_change | color_change | pause | custom`, read-only for `custom`).
- `POST /plates/{i}/layer_changes` `{z, type, slot?, message?}` -> `201` with the full list.
  `z` must be within the plate's objects' height (`422 bad_z`), `slot` must exist (`422 bad_slot`),
  `422 bad_type`; same z replaces. Same data as the preview slider (`plates_custom_gcodes[i]`).
- `DELETE /plates/{i}/layer_changes?z=<mm>` -> full list; `404 no_change_at_z` (tolerance 1e-3 mm).
- `404 unknown_plate`, `409 slicing`. Every change marks that plate unsliced.

## Calibration (capability `calibration`) - generic
- `POST /calibration` `{"type": "flow_rate|pressure_advance|temperature|retraction|
  max_volumetric_speed|vfa", "params": {...}, "discard_project"?: bool}` -> `{"type", "objects":
  [{id, name}]}`. Uses `Plater::calib_*` (no slicing). Creates a new project: when the current one
  has unsaved changes -> `409 {"error":"unsaved_project"}` unless `discard_project: true`.
  `422 bad_type | bad_params` (detail names the param). Params per type are documented in
  docs/remote-api/README.md by the implementer (mirror the calibration dialogs' fields).

## Slice breakdown (capability `breakdown_purge`) - generic
`stats.breakdown` (and the top-level `breakdown` of `GET /slice/status`) gains
`"purge": {"filament_changes": n, "filament_change_time_s": s, "flush_g": g, "flush_mm": mm}`
from the processor results (M6211 tool changes on CC2). Zeros for single-filament prints.

## Print control (capability `print_control`) - RemoteAPIElegoo.cpp
Preference `remote_api_allow_print_control` ("Allow sending and controlling prints"), off by
default, in the Remote API preferences tab with a tooltip explaining the risk. When off every
route below -> `403 {"error":"print_control_disabled","hint":"Preferences > Remote API > Allow
sending and controlling prints"}`. Every route posts an in-app notification naming printer and
file, regardless of `remote_api_notify`. Never accepts a file or path from the client.
- `POST /printers/{id}/print/prepare` body `{"plate"?: i (default current), "bed_side"?: "A"|"B",
  "leveling"?: bool, "timelapse"?: bool, "auto_refill"?: bool, "mapping"?: [{slot, unit, tray}]}`.
  Defaults from the send dialog's recent keys (`printsend_*`); bed side A = btPTE, B = btPC (as
  `PrintHost.cpp` sends `bedType=1` only for btPC). Checks: plate sliced (`409 not_sliced`),
  printer connected (`409 printer_offline`) and idle (`409 printer_busy` + `status`), CANVAS
  connected when >1 filament is used (`409 canvas_disconnected`), mapping complete
  (`422 mapping_incomplete` + `issues`). -> `{"plan_id", "expires_in_s": 300, "summary": {printer,
  plate, file_name, estimated_time_s, filament_g, layers, bed_side, leveling, timelapse,
  auto_refill, mapping, warnings}}`. The plan is bound to a fingerprint of the plate's slice
  result; re-slice/config change/plate change expires it.
- `POST /printers/{id}/print` `{"plan_id"}` -> `202 {"upload_id"}`. Copies the plate's own sliced
  G-code (same bytes the GUI's Send uploads, post-processing as `prepare_upload` does), builds
  `PrinterNetworkParams` like `PrintHost.cpp`, `PrinterUploadManager::startAsyncUpload` with
  upload-and-start. `410 plan_expired`, `404 unknown_plan`.
- `GET /uploads/{upload_id}` -> `{"status": "exporting|uploading|success|failed|cancelled",
  "progress": 0-100, "bytes": [up, total], "message"}` (API keeps its own state: the SDK's task
  read is read-once).
- `DELETE /uploads/{upload_id}` -> `{"ok": true}`; `409 too_late` once the transfer finished.
- `POST /printers/{id}/pause`, `POST /printers/{id}/resume` -> `{"ok": true}`;
  `POST /printers/{id}/stop` needs body `{"confirm": "stop"}` (else `400 confirm_required`).
  `409 printer_offline | not_printing`, `504 printer_timeout` (lifecycle commands may take long:
  treat timeout as unknown and tell the client to check status).

## MCP tools (orcaslicer-mcp branch `elegoo`)
Per PORT_PLAN section 7. Every tool: docstring (when to use, fields, needed capability), `title`,
read-only/destructive hints in `_TOOL_ANNOTATIONS`, errors `{"error","message"?,"hint"?}`, a
missing capability reported as "needs an ElegooSlicer MCP build that lists '<cap>' in get_status
capabilities". Names: `list_printers`, `get_canvas`, `sync_canvas_to_project`,
`preview_filament_mapping`, `list_filament_slots`, `set_filament_slot`, `set_filament_count`,
`split_object`, `add_layer_change`, `list_layer_changes`, `remove_layer_change`, `undo`, `redo`,
`run_calibration`, `prepare_print`, `start_print` (MCP elicitation when the client supports it,
else `user_confirmed=True` required), `get_upload_status`, `cancel_upload`, `pause_print`,
`resume_print`, `stop_print`, `get_camera_snapshot` (first full JPEG from MJPEG
`http://<host>:8080/` for CC2, 10 s timeout, 8 MB cap, LAN only). Existing printer tools
(`get_printer_status`, `wait_for_printer`, `list_print_history`, `check_printer_match`) get the
Elegoo backend (`printer/elegoo.py`, via the slicer routes above) when the active printer profile
has `host_type: elegoolink` or `/status` lists `printers`.
