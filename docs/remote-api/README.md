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
| POST | `/project/save` | Save the project as a .3mf (current file, or a new path) |
| POST | `/objects/{id}/paint` | Paint support / seam / color on the triangles inside a box |
| GET | `/filaments` | List the filament slots: preset, type, colour, used on the current plate |
| PUT | `/filaments` | Set the number of filament slots (the sidebar's + / - buttons) |
| PUT | `/filaments/{slot}` | Set one slot's filament preset and/or colour |
| POST | `/objects/{id}/split` | Split an object into objects, or a part into parts |
| POST | `/undo` | Undo the last model edit (the window's Ctrl+Z) |
| POST | `/redo` | Redo the last undone model edit (the window's Ctrl+Y) |
| GET | `/plates/{i}/layer_changes` | List a plate's changes at a height (filament change, pause, custom G-code) |
| POST | `/plates/{i}/layer_changes` | Add a filament change or a pause at a height |
| DELETE | `/plates/{i}/layer_changes?z=<mm>` | Remove the change at a height |
| POST | `/calibration` | Replace the project with a calibration test (the Calibration menu) |

### GET /status

```bash
curl -H "X-Api-Token: $TOK" http://localhost:13130/api/v1/status
```
```json
{
  "app": "ElegooSlicer", "app_version": "1.5.3.5", "orca_compatible_version": "2.4.2",
  "mcp_build": 1, "api_version": "1.0",
  "capabilities": ["status","config","slice","events","...","plates",
                   "orient_object","view","volumes","project_save","paint"],
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
applied and you get `422` with the error token `config_rejected` and per-key reasons:
```json
{ "error": "config_rejected", "applied": [],
  "errors": { "layer_height": "value out of range", "nope": "unknown_key" } }
```
`PUT /objects/{id}/config` and `PUT /objects/{id}/volumes/{vid}/config` answer a
refused key the same way.
On `PUT /config`, print or printer settings the window would question (spiral vase with
more than one wall, firmware retraction with wipe, ...) are refused the same way, with
the question under the key `_question`
(`errors` then holds the window's question, not a key); a value the window
corrects with a message (a layer height of 0 becomes 0.2) is corrected and the message
is listed in `skipped_dialogs` (see [Dialogs](#dialogs)).
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
{ "error": "config_rejected", "applied": [], "errors": { "hot_plate_temp": "per_filament_length_mismatch: the filament preset open in the Filament tab holds 1 value(s), got 5. ..." } }
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
route, never a missing file. Where the window would show a message or ask a question
while loading (an object in meters, too large, without geometry...), the file is
loaded as it is and the reply lists the message under `skipped_dialogs` (see
[Dialogs](#dialogs)).

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

Each plate's `slice_result_valid` (`GET /plates`) goes false as soon as its G-code is
out of date: a change to the global config (`PUT /config`, `PUT /preset`,
`POST /preset/save` under a new name, `DELETE /preset` of a filament still in use)
marks **every** plate unsliced, and a per-object change (object or volume config,
layer height, height ranges, volumes, paint, transform, `POST /orient` with
`object_id`) marks the plates holding a copy of that object; `POST /orient` of every
object marks every plate when the job starts. (On its own, Orca re-checks only the
current plate when a setting changes; the API marks the others too, and the window
shows the same flags.)

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
`GET /jobs/status` until `idle` (`{"idle": true, "last_error": null}`; `last_error` is
the error of the last arrange/orient job the API started, see [Dialogs](#dialogs)). With `{"object_id": id}` only that object is turned:
it is selected in the 3D view, as a click on it does, and the job orients the
selection (the object stays selected afterwards). Without a body, or with `{}`, every
object is oriented and any selection in the window is cleared first. The plates of
the objects that may turn are marked unsliced when the job starts. Objects on a
locked plate and unprintable objects are left alone; asking for an unprintable object
by id answers `409 object_not_printable` (the job would orient every other object
instead), and `409 select_failed` when the 3D view could not select it. Errors:
`404 unknown_object`, `409 job_running | object_not_printable | select_failed`,
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
(`422 config_rejected` with `errors` and nothing applied if any key is refused; sensitive keys need
the Preferences switch, read-only keys are refused). `"extruder": n` picks the
volume's filament slot (0 = the object's) and updates the object list's filament
column. Reply: `{"applied": [...], "errors": {}, "object": "<name>", "volume": {...}}`.
`404 unknown_object | unknown_volume`.

### DELETE /objects/{id}/volumes/{vid}

`200 {"ok": true}`, through the object list's own delete. Refused where the window
would open a dialog: `409 last_part` (the object's last solid part) and
`409 cut_object` (a part or negative volume of a cut object). `404 unknown_object |
unknown_volume`.

### POST /project/save

```bash
curl -X POST -H "X-Api-Token: $TOK" -H "Content-Type: application/json" \
  -d '{"path": "C:/Users/me/Documents/helmet.3mf", "overwrite": false}' \
  http://localhost:13130/api/v1/project/save
```
```json
{ "path": "C:/Users/me/Documents/helmet.3mf" }
```
Saves the project with the window's own Save code, without its dialogs. With no body
(or no `path`) it saves over the current project file (`409 project_unnamed` when the
project was never saved). With `path` it saves there, like Save As, and the project
takes that name: the path must be absolute, in an existing folder, and end in `.3mf`
(`422 not_3mf | bad_path`); an existing file is replaced only with `"overwrite": true`
(`409 file_exists`). The project is marked saved and its crash-recovery backup is
dropped, as in the window. Waits up to 120 s for big projects. Like the window's
greyed-out Save, it refuses while a G-code file or an exported `.gcode.3mf` is open
(`409 preview_only`: saving would overwrite that sliced file with a plain project) and
while a tool in the 3D view is mid-edit (`409 gizmo_editing`). Errors also:
`500 save_failed`, `400 body_must_be_object | path_must_be_string |
overwrite_must_be_boolean | invalid_json`.

### POST /objects/{id}/paint

```bash
# support enforcers under the overhangs (45 degrees or steeper) of the lower 10 mm
curl -X POST -H "X-Api-Token: $TOK" -H "Content-Type: application/json" \
  -d '{"layer":"support","state":"enforce","box_mm":[[0,0,0],[256,256,10]],"max_overhang_deg":45}' \
  http://localhost:13130/api/v1/objects/832/paint
```
```json
{ "painted_triangles": 412, "layer": "support", "cleared": false }
```
Paints into the same data the paint gizmos edit, so the result shows (and can be
retouched) in the window's Support painting, Seam painting and Color painting tools.
- `layer`: `support` (enforcers/blockers), `seam` or `color`.
- `state` (`support`, `seam`): `enforce`, `block` or `none` (erase).
  `slot` (`color`): 1-based filament slot, at most the project's filament count.
- `box_mm` (optional): `[[x0,y0,z0],[x1,y1,z1]]` in world mm. A triangle is painted
  when its centroid, measured on the object's first copy, is inside the box (edges
  included). Without a box every triangle of the object is a candidate.
- `max_overhang_deg` (optional, `0 <= a <= 90`): only triangles whose outward normal
  points **down** at least `a` degrees below horizontal are painted: with the
  world-space unit normal `n`, `n.z < 0` and `-n.z >= sin(a)`. 90 keeps only flat
  undersides, 45 the usual overhangs that need support, 0 every face that points down
  at all (vertical walls never count).
- `clear` (optional): `true` first erases that layer on the whole object; without
  `state`/`slot` that is all it does. It takes no `box_mm` (`422 clear_takes_no_box`):
  to erase only inside a box, send `"state": "none"` with the box (support, seam).

Only the object's solid parts are painted (as in the gizmos), whole triangles at a
time: big triangles crossing the box edge are painted or not as a whole. An open paint
tool is closed first so it cannot write its old copy back. The object's plates are
marked unsliced and the change can be undone in the window. Errors:
`422 bad_layer | bad_state | bad_slot | bad_box | bad_overhang | clear_takes_no_box`,
`404 unknown_object | no_instance`, `400 body_must_be_object | clear_must_be_boolean |
invalid_json`.

## Filament slots

The sidebar's filament list. Slots are numbered from 1, like the object list's filament
column and the `extruder` setting (`0` there means "the default"). `GET /status` lists
`filaments` in `capabilities`. While a G-code file or an exported `.gcode.3mf` is open
the changes are refused with `409 preview_only` (the window would first ask to close
that file).

### GET /filaments

```json
{ "count": 2, "max": 32, "slots": [
  { "slot": 1, "preset": "Elegoo PLA @ECC2", "type": "PLA", "color": "#00c1ae", "used_on_current_plate": true },
  { "slot": 2, "preset": "Elegoo PLA @ECC2", "type": "PLA", "color": "#ed1c24", "used_on_current_plate": false } ] }
```
`color` is the slot's colour as `#rrggbb`; `used_on_current_plate` is true when an
object, part, height range, colour paint or a layer tool change of the current plate
prints with that slot.

### PUT /filaments

```bash
curl -X PUT -H "X-Api-Token: $TOK" -H "Content-Type: application/json" \
  -d '{"count": 4}' http://localhost:13130/api/v1/filaments
```
Adds or removes slots at the end of the list, one at a time, with the sidebar's own
+ and - buttons: a new slot copies the last slot's preset and gets the next colour of
the window's palette (and its flush volumes are calculated, as the + button does); a
removed slot is the last one. Every plate is marked unsliced. The reply is the same as
`GET /filaments`.

Removing a slot that something still prints with answers
`409 {"error": "slot_in_use", "used_by": [...]}` and removes nothing, unless the body
has `"force": true`. Each `used_by` entry names the object (`object_id`, `name`), the
part when it is one (`volume_id`), the `slot` and the `setting` that uses it (`extruder`,
`support_filament`, `outer_wall_filament_id`... or `color_paint`), plus `height_range`
for a height-range setting. With `force` the window's delete runs as it is: objects and
parts on a removed slot move to filament 1, support and per-feature filament settings
(`support_filament`, `outer_wall_filament_id`...) on it go back to "default", colour
paint in a removed colour is erased (that area prints with the part's filament) and
layer tool changes to a removed slot are dropped.

Errors: `422 bad_count` (an integer from 1 to `max`; also when the printer preset has
one slot per extruder, so the sidebar has no + / - buttons), `409 slot_in_use`,
`409 unsaved_filament_changes` (the Filament tab has unsaved edits to a preset only a
removed slot uses: the window would ask what to do with them), `409 preview_only`,
`400 body_must_be_object | force_must_be_boolean | invalid_json`.

### PUT /filaments/{slot}

```bash
curl -X PUT -H "X-Api-Token: $TOK" -H "Content-Type: application/json" \
  -d '{"preset": "Elegoo PETG @ECC2", "color": "#1e90ff"}' http://localhost:13130/api/v1/filaments/2
```
Reply: the slot, as in `GET /filaments`. `preset` goes through the steps of the slot's
combo box in the sidebar and `color` through its colour picker, without their dialogs:
- `preset`: a filament preset name (as in `GET /presets`) compatible with the selected
  printer. A newly picked preset brings its default colour, as in the combo box, unless
  `color` is given too.
- `color`: `#rrggbb`.

The flush volumes are recalculated as the window's "Auto flush after changing..."
preference says: after a colour change unless it is `disabled`, after a preset change
only when it is `all`. A change marks every plate unsliced. With a single slot the
combo box also switches the Filament tab, which would ask about unsaved edits there:
then `409 unsaved_filament_changes`. Errors: `404 unknown_slot` (with `count`),
`422 unknown_preset | incompatible_preset | bad_color`, `409 preview_only |
unsaved_filament_changes`, `400 missing_fields | preset_must_be_string |
bad_slot_index | invalid_json`.

## Split

### POST /objects/{id}/split

```bash
curl -X POST -H "X-Api-Token: $TOK" -H "Content-Type: application/json" \
  -d '{"to": "objects"}' http://localhost:13130/api/v1/objects/832/split
```
```json
{ "ids": [901, 905, 909] }
```
Right-click > Split > To objects / To parts, without the dialogs they can open
(capability `split`). The split can be undone in the window.
- `"to": "objects"`: an object with several parts becomes one object per solid part
  (modifiers and other non-solid volumes are left out, as in the window); a single part
  is cut into its separate shells. `ids` are the new object ids; the original object is
  gone. When a piece would float, the window asks whether to keep the pieces' heights;
  the API does not ask: with `"drop_to_bed": true` (default) every piece is put on the
  bed, with `false` every piece keeps its height and stops auto-dropping (the
  question's "Yes"). The new objects are left selected, as in the window.
- `"to": "parts"`: one part is cut into its separate shells, which stay parts of the
  object. An object with a single part needs nothing else; with several parts, give
  `"volume_id"` (from `GET /objects/{id}/volumes`). `ids` are the object's volume ids
  afterwards, and `object_id` is echoed. The part (or object) is left selected.

The plates holding the result are marked unsliced. Errors: `422 not_splittable`
(a single solid piece; `detail` says why, also when an object with several parts gets
`"to": "parts"` without `volume_id`) `| bad_to`, `404 unknown_object | unknown_volume
| no_instance`, `409 job_running` (an arrange/orient job is running) `| gizmo_editing`
(a paint, cut or other editing tool is open in the 3D view) `| select_failed`,
`400 body_must_be_object | bad_volume_id | drop_to_bed_must_be_boolean | bad_object_id |
invalid_json`.

## Undo / redo

### POST /undo, POST /redo

```json
{ "ok": true, "can_undo": true, "can_redo": true }
```
The window's Ctrl+Z / Ctrl+Y on its undo history (capability `undo`): object, part,
plate and paint edits, whether made in the window or through the API. Presets,
settings and filament slots are not in that history, as in the window. `ok` is false
(still `200`) when there was nothing to undo or redo; `can_undo` / `can_redo` say
whether another call would do something. After a step every plate is marked unsliced
and `GET /slice/status` follows the current plate (a step can add or remove plates).
Errors: `409 slicing | job_running` (an arrange/orient job) `| gizmo_editing` (a paint,
cut or other editing tool is open in the 3D view; the move, rotate and scale tools are
fine).

## Layer changes

What the preview's layer slider adds at a layer (the "+" on a layer): a filament change,
a pause or custom G-code. They are stored per plate in the same place the slider keeps
them, so a change made here shows on the slider and the other way round, and they are
saved with the project (capability `layer_changes`). `{i}` is the 0-based plate index.
Every change marks that plate unsliced; a preview on screen reloads.

### GET /plates/{i}/layer_changes

```json
{ "plate": 0, "items": [
  { "z": 4.2, "type": "tool_change", "slot": 2 },
  { "z": 10.0, "type": "pause" },
  { "z": 15.0, "type": "custom", "gcode": "M117 hello" } ] }
```
`type` is `tool_change` (with `slot`, the filament to change to), `pause` (with
`message` when one was stored), `custom` (custom or template G-code from the window,
with its `gcode`; read-only here) or `color_change` (with `slot`; only in projects made
elsewhere). Sorted by `z` (mm). `404 unknown_plate`.

### POST /plates/{i}/layer_changes

```bash
curl -X POST -H "X-Api-Token: $TOK" -H "Content-Type: application/json" \
  -d '{"z": 4.2, "type": "tool_change", "slot": 2}' http://localhost:13130/api/v1/plates/0/layer_changes
```
`201` with the plate's whole list, as in GET. A change at the same height (within
0.001 mm) is replaced.
- `z`: height in mm, above 0 and below the top of the plate's tallest object. The
  change applies from the layer at that height.
- `type`: `tool_change` or `pause`.
  - `tool_change` needs `slot` (1-based filament slot). As on the slider it needs 2 or
    more filament slots, a plate whose objects print with a single filament (parts,
    paint or height ranges in other filaments already change filament), and no spiral
    vase mode. On the CC2 it becomes a CANVAS filament change (`M6211`).
  - `pause`: the printer pauses before that layer (`machine_pause_gcode`). `message`
    (optional) is stored with it in the project; this slicer writes no message into
    the G-code. A `slot` is ignored.
  - `color_change` is refused: ElegooSlicer writes no G-code for it (the slider does
    not offer it either); use `tool_change`. Custom G-code is added in the window.

Errors: `422 bad_z` (out of range, an empty plate, or a custom G-code already at that
height: remove it first) `| bad_type` (also when the plate prints object by object, or
the `tool_change` conditions above are not met; `detail` says which) `| bad_slot`,
`404 unknown_plate`, `409 slicing | preview_only`, `400 body_must_be_object |
message_must_be_string | bad_plate_index | invalid_json`.

### DELETE /plates/{i}/layer_changes?z=<mm>

Removes the change at that height (within 0.001 mm), whatever its type, custom G-code
included, as the slider's delete does. Reply: the plate's remaining list. Errors:
`404 unknown_plate | no_change_at_z`, `409 slicing | preview_only`,
`400 missing_z | bad_z`.

## Slice breakdown: purge

Once a plate is sliced, `GET /slice/status` reports `stats.breakdown` (also at the top
level as `breakdown`): the per-feature times and filament of the slice. Builds that
list `breakdown_purge` in `capabilities` add what changing filament costs:

```json
"purge": { "filament_changes": 12, "filament_change_time_s": 1043.5,
           "flush_g": 38.2, "flush_mm": 12804.1 }
```
- `filament_changes`: how many times the print changes filament.
- `filament_change_time_s`: the time those changes take (filament load + unload + tool
  change), the "Tool changes" time of the preview's legend. It is part of the total
  time but of no feature in `roles`. On the Centauri Carbon 2 each CANVAS change
  (`M6211`) is timed by ElegooSlicer's own estimate.
- `flush_g`, `flush_mm`: filament purged by those changes, all filaments together (the
  legend's "Flushed" column), from each filament's diameter and density.

All four are 0 when nothing loads or changes filament. On the Centauri Carbon 2 the
start G-code loads the first filament with an `M6211` that purges about 200 mm, so even
a single-filament print reports that load's time and purge, with `filament_changes` 0.

## Calibration

### POST /calibration

```bash
curl -X POST -H "X-Api-Token: $TOK" -H "Content-Type: application/json" \
  -d '{"type": "pressure_advance", "params": {"method": "line", "end": 0.08}}' \
  http://localhost:13130/api/v1/calibration
```
```json
{ "type": "pressure_advance", "objects": [ { "id": 812, "name": "pressure_advance_test.drc" } ] }
```
The window's Calibration menu without its dialogs (capability `calibration`): the
current project is replaced by a new one holding the test model, and the print,
filament and printer settings are changed the way the test needs (unsaved preset
edits, as in the window; `GET /status` lists them under `modified`). Nothing is
sliced: slice it as usual. `objects` are the new project's objects.

Replacing the project would make the window ask about unsaved work. When the project
has unsaved changes, or the print/filament/printer settings have unsaved edits, the
reply is `409 {"error":"unsaved_project","project_unsaved":true,"presets_unsaved":["print"]}`
unless the body has `"discard_project": true`; then those changes are dropped and the
reply lists them in `discarded_changes` (`"project"`, `"print"`, `"filament"`,
`"printer"`). A calibration itself leaves the settings edited and the project unsaved,
so a second calibration in a row needs `discard_project` too. The
`max_volumetric_speed` and `vfa` tests turn spiral vase on in the print settings;
`max_volumetric_speed` sets one wall, no top layers and no infill on the test object
only, and `vfa` leaves supports as they were. So afterwards a `PUT /config` that
changes print settings can be refused with the window's spiral vase question until the
print settings agree with it, which is when the window would ask too (see
[Dialogs](#dialogs)).

`params` holds the dialog's fields; everything is optional and defaults to the
dialog's own default. An unknown name, a wrong type or a value the dialog would refuse
gives `422 bad_params` with a `detail` that names the parameter.

| `type` | `params` (default) | Checks (as the dialog) |
|---|---|---|
| `pressure_advance` | `method`: `tower` \| `line` \| `pattern` (`tower`); `extruder`: `dde` \| `bowden` (`dde`, only picks the defaults below); `start` (0); `end` (tower/line 0.1, pattern 0.08; bowden 1.0); `step` (tower/line 0.002, pattern 0.005; bowden 0.02, pattern 0.05); `print_numbers`: bool, line only (true; pattern always prints numbers, the tower never); `accelerations`, `speeds`: lists of positive whole numbers in mm/s² and mm/s, pattern only (none: the outer wall acceleration and an optimal speed; the pattern prints one test per acceleration × speed) | `start` >= 0, `step` >= 0.001, `end` >= `start` + `step`; every acceleration > every speed |
| `flow_rate` | `test`: `yolo` \| `yolo_perfectionist` \| `pass1` \| `pass2` (`yolo`); `pattern`: top surface `archimedean_chords` \| `monotonic` (`archimedean_chords`) | — |
| `temperature` | `start` (230), `end` (190), °C; the tower goes from `start` at the bottom down to `end`, 5 °C per block. The dialog's presets: PLA 230/190, ABS/ASA 270/230, PETG 250/230, PCTG 280/240, TPU 240/210, PA-CF and PET-CF 320/280 | multiples of 5 between 155 and 500, `start` >= `end` + 5 |
| `max_volumetric_speed` | `start` (5), `end` (20), `step` (0.5), mm³/s | `start` > 0, `step` > 0, `end` >= `start` + `step` |
| `vfa` | `start` (40), `end` (200), `step` (10), mm/s | `start` > 10, `step` > 0, `end` >= `start` + `step` |
| `retraction` | `start` (0), `end` (2), `step` (0.1), mm of retraction length | `start` >= 0, `step` > 0, `end` >= `start` + `step` |

Other errors: `422 bad_type`, `409 job_running` (an arrange/orient job is running) `|
slicing`, `500 calibration_failed` (the test model did not load), `400 body_must_be_object |
discard_project_must_be_boolean | invalid_json`. Allow up to 60 s: the test model is
loaded and cut to height.

## Dialogs

An API request never opens a dialog in the window: nobody may be at the machine to
close it, and a modal dialog would block the window, and every later API call, until
someone did. Where the window would show a message box (an error, a warning, a note),
the request shows the same text as a notification in the window, starting with
"Remote API:", and lists it in its reply under `skipped_dialogs` (only present when
something was skipped):

```json
{ "loaded": true, "objects": [ ... ],
  "skipped_dialogs": [ "The 3mf is not supported by ElegooSlicer, load geometry data only." ] }
```

Where the window would ask a question, the route either refuses the request with the
question as the reason (each route below says when), or takes the answer that leaves
things as they are; the entry then ends with `(answered No)`.

| Where | What the window would show | What the API does |
|---|---|---|
| any route | an error box (`show_error`) or an info box (`show_info`), e.g. "Failed loading file" on `POST /model`, the "load geometry data only" note for a PrusaSlicer 3MF | notification + `skipped_dialogs` |
| `PUT /config` | the Print/Filament tab's check of the new values corrects one and says so ("Too small layer height. Reset to 0.2.", ironing spacing, first layer height, XY compensation, elephant foot, seam slope start height, lock depth, max volumetric speed < 0.5) | the correction is made, notification + `skipped_dialogs` |
| `PUT /config` | the Print tab's check asks a question: spiral vase needs one wall, no top layers, no infill, no support...; alternate extra wall with "ensure vertical shell thickness: All"; the Extrusion/Combined fuzzy skin modes need the Arachne wall generator | refused before anything is applied: `422 config_rejected` with the question in `errors._question`; send settings that agree, in the same request |
| `PUT /config` of printer settings | the Printer tab's check, whenever its Extruder page is on screen: "The Retract before wipe option could be only 100% when using the Firmware Retraction mode ..." | refused before anything is applied, whether that page is open or not: `422 config_rejected` with the question in `errors._question`; send `wipe` off or `retract_before_wipe` at 100% with it |
| other routes that reload print or printer settings (`PUT /preset` to a preset saved with such a combination, per-object settings) | the same checks | boxes as above; a question is answered No (the setting that conflicts is turned off: spiral vase, firmware retraction...) |
| `POST /model` | "Objects with zero volume removed"; "The file does not contain any geometry data." (then `422 load_failed`); an object over 10000 times the bed is scaled down with a note | notification + `skipped_dialogs` |
| `POST /model` | "The object ... is too small, and maybe in meters or inches. Do you want to scale to millimeters?"; "Your object appears to be too large, do you want to scale it down ...?" | answered No: the file is loaded as it is; scale it with `POST /objects/{id}/transform` |
| `POST /model` | an OBJ file with colours: the window asks which filament prints each colour | loaded without its colours (the dialog's Cancel), with a `skipped_dialogs` note |
| `POST /model` | several objects at different heights: "load as a single object with multiple parts?" | loaded as separate objects, the dialog's No (as in MaxEllis/OrcaSlicer; no `skipped_dialogs` entry) |
| `POST /preset/save` of a filament preset | "Temperature Safety Check": the first-layer and other-layer temperatures differ by more than 30 °C (nozzle) or 15 °C (a bed type); "Continue anyway?" | refused, nothing saved: `422 {"error":"temperature_safety_check","detail":"<the question>"}`. Add `"ignore_temperature_check": true` to the body to save anyway (answered Yes) |
| `GET /plate/render?view=preview` | the G-code preview cannot start its OpenGL renderer (an error box) | notification + `skipped_dialogs`; the render then most likely fails with `500 render_failed` |
| `POST /arrange`, `POST /orient` (the job ends after the reply) | "Arrange failed. Found some exceptions when processing object geometries.", "An unexpected error occurred: ..." | notification, and the text in `GET /jobs/status` as `last_error` (null when the last job the API started had no error); jobs started in the window keep their dialog |
| `POST /slice` (the slice ends after the reply) | a critical slicing error in an error box | an error notification like the window's other slicing errors; `GET /slice/status` has `state: "error"` and the message, and WS sends `slice.error`. Slices started in the window keep their dialog |

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
