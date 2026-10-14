# ElegooSlicer MCP Remote API

A token-authenticated HTTP + WebSocket API embedded in ElegooSlicer MCP (ElegooSlicer
plus the Remote API ported from [MaxEllis/OrcaSlicer](https://github.com/MaxEllis/OrcaSlicer)
@ `fa3c9ee`, AGPL-3.0). It exposes the
**running** application for automation: read status, read/write config live, kick
off slicing, and subscribe to a live event stream.

- **Off by default.** Enable it explicitly in Preferences.
- **Single API version:** `v1`, all paths under `/api/v1`.
- **Machine-readable spec:** [`openapi.yaml`](./openapi.yaml).

## Enabling

Preferences → **Remote API**:

| Setting | Meaning |
|---|---|
| **Enable** | Start the server (applied immediately on closing Preferences). |
| **Bind LAN** | Off = listen on `127.0.0.1` only. On = listen on `0.0.0.0` (reachable from other machines on your network). |
| **Port** | Default `13130`. |
| **Token** | 32 hex chars, auto-generated on first run. **Regenerate** invalidates existing clients. |

> Binding to the LAN exposes live control of your slicer to every machine on the
> network. Keep the token secret and prefer localhost unless you need remote access.

## Authentication

- **HTTP:** send the header `X-Api-Token: <token>` on every request. Missing/wrong
  token → `401 {"error":"unauthorized"}`.
- **WebSocket:** pass `?token=<token>` in the URL (browsers can't set WS headers).

## Endpoints

Base URL: `http://<host>:13130/api/v1`

| Method | Path | Purpose |
|---|---|---|
| GET | `/status` | App/project/preset status + slice validity |
| GET | `/config[?keys=a,b]` | Read merged config (secrets redacted) |
| PUT | `/config` | Apply config edits live (atomic) |
| POST | `/slice` | Start slicing the current plate |
| GET | `/slice/status` | Current/last slice state + stats + warnings |
| WS | `/events?token=…` | Live event stream |
| POST | `/model` | Load a model file onto the plate |
| GET | `/plates` | List the plates: index, name, current, locked, objects on each |
| POST | `/plates` | Add a plate (`{"select": true}` by default) |
| POST | `/plates/{i}/select` | Make plate `i` the current one |
| DELETE | `/plates/{i}` | Delete plate `i` (its objects are parked beside the bed, not deleted) |
| POST | `/objects/{id}/plate` | Move an object onto plate `i` |
| POST | `/objects/{id}/transform` | Move, rotate and/or scale an object |
| POST | `/orient` | Auto-orient every object, or one object with `{"object_id": id}` |
| POST | `/view` | Show the Prepare or Preview tab |
| GET | `/objects/{id}/volumes` | List an object's parts, modifiers, negative volumes and support enforcers/blockers |
| POST | `/objects/{id}/volumes` | Add a box/cylinder/sphere modifier, negative volume or support enforcer/blocker |
| PUT | `/objects/{id}/volumes/{vid}/config` | Set one volume's own settings (atomic) |
| DELETE | `/objects/{id}/volumes/{vid}` | Remove a volume |

### GET /status

```bash
curl -H "X-Api-Token: $TOK" http://localhost:13130/api/v1/status
```
```json
{
  "app": "ElegooSlicer", "app_version": "1.5.3.5", "orca_compatible_version": "2.4.2",
  "mcp_build": 1, "api_version": "1.0",
  "capabilities": ["status","config","slice","events"],
  "project": "",
  "objects": [{"name":"cube","size_mm":[20,20,20]}],
  "presets": {"printer":"...","print":"...","filaments":["..."]},
  "modified": {"print":["layer_height"],"filament":[],"printer":[]},
  "slice_result_valid": false,
  "view": "prepare",
  "slicing": false
}
```
`view` is the main-window tab on screen: `prepare`, `preview` or `other` (home,
device and other pages).

### GET /config

```bash
curl -H "X-Api-Token: $TOK" "http://localhost:13130/api/v1/config?keys=layer_height,wall_loops"
```
```json
{ "config": { "layer_height": "0.2", "wall_loops": "2" } }
```
Omit `?keys=` to get the full merged config. Values are canonical strings (same
form as `.ini`/`.3mf`).

### PUT /config  (atomic)

```bash
curl -X PUT -H "X-Api-Token: $TOK" -H "Content-Type: application/json" \
  -d '{"layer_height":0.28,"wall_loops":3}' \
  http://localhost:13130/api/v1/config
```
```json
{ "applied": ["layer_height","wall_loops"], "errors": {} }
```
Applied through the GUI's own edit path (dirty markers + live refresh + slice
invalidation). **All-or-nothing:** if any key is unknown or invalid, nothing is
applied and you get `422` with per-key reasons:
```json
{ "applied": [], "errors": { "layer_height": "value out of range", "nope": "unknown_key" } }
```
Other errors: `400 body_must_be_object`, `400 invalid_json`, `504 ui_timeout`.

#### Per-filament keys

`GET /config` returns the **merged** config, so a vector-valued filament key holds
one run of values per loaded filament slot:

```json
{ "config": { "hot_plate_temp": "70,100,70,70,35" } }
```

`PUT /config`, by contrast, writes the single filament preset open in the Filament
tab. Those two shapes are not interchangeable, so **writing a merged value back is
rejected**:

```json
{ "applied": [], "errors": { "hot_plate_temp": "per_filament_length_mismatch: the filament preset open in the Filament tab holds 1 value(s), got 5. ..." } }
```

Pass only that preset's own value (`{"hot_plate_temp": 70}`), and change the other
slots by selecting their presets in turn. Writing a key back unchanged still works
on a single-filament setup, where merged and per-preset shapes coincide.

Earlier builds accepted the merged string and stored all of it in the one preset.
The next merge then spliced that whole list in at *every* slot using that preset,
so the value grew on each write (5 entries → 13 → …) and the per-slot settings were
lost. If you hit that, re-select the filament preset to restore it.

#### Project-scope keys (multi-material / CFS)

Most keys resolve against the edited print, filament or printer preset. A few
belong to the *project* instead, and only these seven are writable:

| Key | Shape | Constraint |
|---|---|---|
| `flush_volumes_matrix` | `,`-separated floats | exactly `filaments² × nozzles` values, each in `[0, 20000]` |
| `flush_multiplier` | `,`-separated floats | one per nozzle, each in `[0, 3]` |
| `filament_colour` | `;`-separated `#RRGGBB` | recolour only — the count is fixed by the filament presets |
| `curr_bed_type` | enum name | `"Default Plate"` is rejected |
| `prime_volume_mode` | enum name | — |
| `wipe_tower_x`, `wipe_tower_y` | float | applies to the current plate only |

```bash
curl -X PUT -H "X-Api-Token: $TOK" -H "Content-Type: application/json" \
  -d '{"flush_multiplier":"0.3","flush_volumes_matrix":"0,280,280,280,280,0,280,280,280,280,0,280,280,280,280,0"}' \
  http://localhost:13130/api/v1/config
```

The size and range checks are not decoration: these vectors are indexed by the
slicer with arithmetic bounded by a *different* vector, so a wrong-length write
is an out-of-bounds access rather than a validation error. Values are also vetted
as text before parsing, because the vector deserializers turn an unparsable token
into `0` and report success — which would silently mean "no purge at all".

The remaining project keys stay read-only (they appear in `GET /config` but not
in `PUT`) and report why: `values_are_unchecked_extruder_indices` (`filament_map`,
`filament_volume_map`, `filament_nozzle_map`), `tied_to_filament_map`
(`filament_map_mode`, `nozzle_volume_type`), `indexed_unchecked_by_the_colour_picker`
(`filament_colour_type`, `filament_multi_colour`), `derived_from_flush_volumes_matrix`
(`flush_volumes_vector`), `live_printer_state_not_a_setting`
(`has_filament_switcher`, `enable_filament_dynamic_map`).

### POST /slice

```bash
curl -X POST -H "X-Api-Token: $TOK" http://localhost:13130/api/v1/slice
```
- `202 {"started":true}` — slicing began (watch events or poll `/slice/status`).
- `200 {"started":false,"already_valid":true}` — plate already sliced.
- `409 {"error":"already_slicing"}` — a slice is in progress.
- `422 {"error":"nothing_to_slice"}` — no objects on the plate.

### GET /slice/status

```json
{
  "state": "done", "percent": 100, "message": "",
  "stats": { "estimated_time": "27m 23s", "estimated_time_seconds": 1643,
             "filament_used_mm": 1582, "filament_used_g": 4.72, "total_cost": 0.094 },
  "warnings": [ { "level": 3, "message": "bed temp too high…", "code": "1000C001" } ]
}
```
`state` is one of `idle | slicing | done | error`. `stats` appears once done.

### WS /events

```
ws://<host>:13130/api/v1/events?token=<token>
```
Each message is a JSON object with an `event` field:

| event | payload |
|---|---|
| `slice.started` | `state,percent,message` |
| `slice.progress` | `state:"slicing",percent,message` (stage text) |
| `slice.done` | `state:"done",percent:100,stats{…}` |
| `slice.error` | `state:"error",message` |
| `slice.cancelled` | `state:"idle",message:"cancelled"` |
| `slice.plate_changed` | the current plate's state after a plate route: `done` + `stats`, or `idle` + `message` |
| `config.changed` | `tabs:[print\|filament\|printer\|sla_print\|sla_material\|other]` |
| `project.opened` | `project:"<filename>"` |

> `slice.*` events carry `stats` (on done) but not the slice `warnings` array —
> call `GET /slice/status` for warnings.

### POST /model

```bash
curl -X POST -H "X-Api-Token: $TOK" -H "Content-Type: application/json" \
  -d '{"path": "C:/models/part.stl"}' http://localhost:13130/api/v1/model
```
```json
{ "loaded": true, "objects": [ { "index": 0, "name": "part.stl", "size_mm": [20, 20, 10] } ] }
```
Loads a `.stl`, `.obj`, `.3mf`, `.step` or `.stp` file from a path on the machine
running the slicer, as an import in the window does. Errors: `400 missing_path`,
`404 file_not_found` (the file does not exist; the body echoes the path:
`{"error":"file_not_found","path":"C:/models/part.stl"}`), `422 unsupported_format |
load_failed`, `400 invalid_json`. A bare `404 not_found` only ever means an unknown
route, never a missing file.

## Plates

`slice`, `gcode`, `plate/render` and `jobs/status` all act on the **current** plate.
`arrange` is the toolbar's "Arrange all": it re-packs the objects of every unlocked
plate together and deletes plates left empty at the end, so it undoes a
one-part-per-plate layout (locked plates are left alone). The plate
routes let a client build a multi-plate project the way the toolbar does: every
mutation goes through the same `Plater` calls as a click, so undo, the sidebar plate
list, the background process and the preview follow along. `GET /status` reports
`plates: {count, current}` and lists `plates` in `capabilities`; `GET /objects` adds
a `plate` field per object (0-based, `-1` when it sits off every plate).

`GET /slice/status` follows the current plate. After a plate is selected, added
with `select`, deleted, or an object moves, it reports the current plate's own
result (`done` with its stats when that plate is still sliced, otherwise `idle`
with the message `plate N is not sliced`), and `POST /slice` on a plate that is
already sliced (`already_valid`) refreshes it the same way. Without that, the
status would keep describing whichever plate was sliced last.

### GET /plates

```bash
curl -H "X-Api-Token: $TOK" http://localhost:13130/api/v1/plates
```
```json
{
  "plates": [
    { "index": 0, "name": "", "current": true, "locked": false, "empty": false,
      "slice_result_valid": false,
      "origin": [0,0,0], "bbox_min": [0,0,0], "bbox_max": [256,256,250],
      "objects": [ { "id": 794, "instance": 0, "name": "box_base.stl" } ] },
    { "index": 1, "name": "", "current": false, "locked": false, "empty": true,
      "slice_result_valid": false,
      "origin": [281,0,0], "bbox_min": [281,0,0], "bbox_max": [537,256,250],
      "objects": [] }
  ],
  "count": 2, "current": 0, "max": 36, "can_add": true, "can_delete": true
}
```
Indices are 0-based and shift down when an earlier plate is deleted, exactly as in
the GUI (plate "2" on screen is index 1).

### POST /plates

```bash
curl -X POST -H "X-Api-Token: $TOK" -H "Content-Type: application/json" \
  -d '{"select": true}' http://localhost:13130/api/v1/plates
```
```json
{ "created": true, "index": 1, "count": 2, "current": 1, "plate": { "...": "as in GET /plates" } }
```
The body is optional; `select` defaults to `true` (the new plate becomes current,
as with the toolbar button). Errors: `422 max_plates` (the limit is reported in
`max`), `409 slicing`.

### POST /plates/{i}/select

```json
{ "selected": true, "current": 1, "plate": { "...": "as in GET /plates" } }
```
Errors: `404 unknown_plate`, `409 slicing`, `422 select_failed`.

### DELETE /plates/{i}

```json
{ "deleted": true, "index": 1, "count": 1, "current": 0, "objects_moved_off_plate": 2 }
```
The objects the plate held are **not** deleted: `Plater::delete_plate` parks them
beside the bed, as the toolbar does, and `objects_moved_off_plate` says how many.
Errors: `404 unknown_plate`, `422 last_plate` (the only plate cannot be deleted),
`409 slicing`.

### POST /objects/{id}/plate

```bash
curl -X POST -H "X-Api-Token: $TOK" -H "Content-Type: application/json" \
  -d '{"plate": 1}' http://localhost:13130/api/v1/objects/832/plate
```
```json
{ "moved": true, "id": 832, "instance": 0, "plate": 1, "from_plate": 0, "offset": [409,128,6] }
```
`instance` (default 0) picks the copy when the object has several. An object coming
from another plate keeps its position relative to that plate (front-left of plate 1
lands front-left of plate 2); one coming from off-plate goes to the target's centre.
Already on that plate: `200 {"moved": false, "already_on_plate": true}`. The plate
it left is marked unsliced, as a drag in the window does. If keeping the relative
position would put it outside the target (it straddled the old plate's edge), it is
centred instead. `POST /objects/{id}/transform` and `/duplicate` also re-check which
plate the object (or the new copy) sits on, so an object moved onto another plate by
coordinates is sliced with that plate. A plate's deleted objects are parked where the
next plate appears, so `POST /plates` straight after a delete picks them back up. Errors:
`404 unknown_object | unknown_plate | unknown_instance`, `422 plate_locked` (the
target or the plate it sits on is locked; `plate` names which) `| move_failed`,
`409 slicing`, `400 missing_plate`.

### POST /objects/{id}/transform

```bash
curl -X POST -H "X-Api-Token: $TOK" -H "Content-Type: application/json" \
  -d '{"rotate": [0, -90, 0]}' http://localhost:13130/api/v1/objects/832/transform
```
```json
{ "id": 832, "transform": { "offset": [128,128,10], "rotation": [0,-1.5708,0], "scale": [1,1,1] } }
```
Send any of `translate`, `rotate`, `scale`, each an `[x, y, z]` array. They are
applied to the object's first copy in this order:
- `translate` (mm) is added to the current position.
- `rotate` (degrees) turns the object from its current orientation about the
  **world** axes, as the rotate gizmo does: X first, then Y, then Z, around the
  object's origin (the rotation itself does not move `offset`, but the drop onto the
  bed described below may then change its Z). Calls add up like gizmo drags, so
  `[0,-90,0]` followed by `[0,90,0]` puts the object back the way it started.
- `scale` (factor) is absolute, along the object's own axes (`[1,1,1]` = original size).

Then the object is put back on the bed as a drag in the window does: if it ends up
floating it drops onto the bed (so a pure upward `translate` does not lift it); if it
ends up below the bed it is lifted back up, unless the request was a pure `translate`
(sinking it on purpose) or it was already below the bed before the `rotate`/`scale`.

The response gives the resulting `offset` (mm), `rotation` (radians: the X, Y, Z
angles of the new orientation, in the order above) and `scale`. Errors:
`400 no_transform | body_must_be_object | invalid_json | bad_object_id`,
`404 unknown_object | no_instance`.

### POST /orient

```bash
curl -X POST -H "X-Api-Token: $TOK" -H "Content-Type: application/json" \
  -d '{"object_id": 832}' http://localhost:13130/api/v1/orient
```
```json
{ "started": true, "object_id": 832 }
```
Starts the window's auto-orient job and answers `202` at once; poll
`GET /jobs/status` until `idle`. With `{"object_id": id}` only that object is turned:
it is selected in the 3D view, as a click on it does, and the job orients the
selection (the object stays selected afterwards). Without a body, or with `{}`, every
object is oriented and any selection in the window is cleared first. Objects on a
locked plate are left alone. Errors: `404 unknown_object`, `409 job_running`,
`422 empty` (no objects), `400 bad_object_id | body_must_be_object | invalid_json`.

### POST /view

```bash
curl -X POST -H "X-Api-Token: $TOK" -H "Content-Type: application/json" \
  -d '{"view": "preview"}' http://localhost:13130/api/v1/view
```
```json
{ "view": "preview" }
```
Switches the main window to the `prepare` (3D editor) or `preview` tab, as the tab
buttons do; the reply names the tab now showing. Opening Preview on an unsliced plate
behaves as in the window. With a G-code file open the window is preview-only, and
the Prepare button would ask whether to close that file, so `prepare` is refused
with `409 preview_only`. Errors: `400 missing_view | invalid_json`, `422 bad_view`.

## Part volumes

The rows under an object in the object list: its parts, modifiers, negative volumes
and support enforcers/blockers. `{id}` is the object id of `GET /objects`, `{vid}`
the volume id listed here (ids, not positions: adding or removing a volume reorders
`index`). Sizes and positions are world millimetres measured on the object's first
copy. Every change marks the plates holding the object unsliced.

### GET /objects/{id}/volumes

```json
{ "object": 832, "volumes": [
  { "id": 833, "index": 0, "name": "cube", "type": "part", "extruder": 0,
    "size_mm": [20,20,20], "position_mm": [128,128,10], "config": {} },
  { "id": 901, "index": 1, "name": "Generic-Cube", "type": "modifier", "extruder": 0,
    "size_mm": [30,30,5], "position_mm": [128,128,12.5], "config": {"extruder": "0", "sparse_infill_density": "40%"} } ] }
```
`type` is `part | modifier | negative | support_enforcer | support_blocker`;
`extruder` 0 means the volume uses the object's filament; `position_mm` is the centre
of the volume's bounding box; `config` holds the volume's own overrides. `404 unknown_object`.

### POST /objects/{id}/volumes

```bash
curl -X POST -H "X-Api-Token: $TOK" -H "Content-Type: application/json" \
  -d '{"type":"modifier","shape":"box","size_mm":[30,30,5],"position_mm":[128,128,12.5],"config":{"sparse_infill_density":"40%"}}' \
  http://localhost:13130/api/v1/objects/832/volumes
```
`201 {"volume": {...}}` with the fields above. Adds the shape the way right-click >
Add modifier / Add negative volume / Support enforcer / Support blocker > Cube,
Cylinder or Sphere does, minus its tips dialog: the shape stays aligned with the bed
whatever the object's rotation, its centre goes to `position_mm` and its size is
`size_mm` (a cylinder or sphere may be elliptic). `name` defaults to the GUI's
("Generic-Cube"...). `config` is checked like `PUT /objects/{id}/config` before
anything is added; a refused key answers `422 {"error":"config_rejected","errors":{...}}`
and adds nothing. Errors: `422 bad_type | bad_shape | bad_size | bad_position`
(size: each side > 0 and <= 10000 mm), `404 unknown_object | no_instance`,
`400 body_must_be_object | name_must_be_string | config_must_be_object | invalid_json`.

### PUT /objects/{id}/volumes/{vid}/config

Same body, atomic semantics and key policy as `PUT /objects/{id}/config`
(`422` with `errors` and nothing applied if any key is refused; sensitive keys need
the Preferences switch, read-only keys are refused). `"extruder": n` picks the
volume's filament slot (0 = the object's) and updates the object list's filament
column. Reply: `{"applied": [...], "errors": {}, "object": "<name>", "volume": {...}}`.
`404 unknown_object | unknown_volume`.

### DELETE /objects/{id}/volumes/{vid}

`200 {"ok": true}`, through the object list's own delete. Refused where the window
would open a dialog: `409 last_part` (the object's last solid part) and
`409 cut_object` (a part or negative volume of a cut object). `404 unknown_object |
unknown_volume`.

## Notes & limits

- **Single-client oriented.** The server uses one I/O thread and marshals most
  calls onto the GUI thread with a 10s timeout (`504 ui_timeout`). Heavy or
  concurrent calls serialize; a stuck GUI stalls the API for up to 10s.
- **Token strength.** 32 hex chars (128 bits) from OpenSSL `RAND_bytes` — fine for
  a LAN token, not intended as a cryptographic secret store.
- **Survives GUI recreate.** Changing language/skin rebuilds the GUI; the server
  keeps running and slice-event subscriptions rebind onto the new window, so WS
  clients stay connected.
- **One instance owns the port.** On Windows the listener uses `SO_EXCLUSIVEADDRUSE`,
  so only one ElegooSlicer window can hold it; another window logs the failure and
  shows "Remote API could not listen on port ..." (rebinding right after a restart
  still works). On Linux/macOS it sets `SO_REUSEADDR`, so a quick restart rebinds even
  if a prior socket is in `TIME_WAIT`.
