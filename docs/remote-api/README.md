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
`GET /jobs/status` until `idle`. With `{"object_id": id}` only that object is turned:
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

## Elegoo printers and CANVAS

Access to the printers of the Device tab and to the CANVAS multi-material unit of a
Centauri Carbon 2 (capabilities `printers` and `canvas`): read-only, except the CANVAS
sync, which changes the project's filaments like the GUI's Sync button. Nothing here opens
a window or a dialog, and nothing is sent to the printer except status and CANVAS
requests.

| Method | Path | Purpose |
|---|---|---|
| GET | `/printers` | Every printer, and the one the send dialog would pick |
| GET | `/printers/{id}` | One printer |
| GET | `/printers/{id}/status[?raw=1]` | One printer + its exception codes + the age of the data |
| GET | `/printers/{id}/tasks?page=1&size=20` | Print history (cloud printers only) |
| GET | `/printers/{id}/files?page=1&size=20` | Files stored on the printer (cloud printers only) |
| GET | `/printers/{id}/canvas` | CANVAS units and trays, with the filament preset matched to each tray |
| POST | `/printers/{id}/canvas/sync` | Make the CANVAS trays the project's filaments (the GUI's Sync button) |
| GET | `/printers/{id}/canvas/mapping[?plate=i]` | The tray each filament of a plate would print from, with problems |

- **Where the data comes from.** ElegooSlicer keeps every printer in a cache that the
  printers' own status messages keep up to date; the printer routes read it. The
  CANVAS state is not cached by ElegooSlicer: each read is a request to the printer.
- **Printer ids** are opaque (`"p"` + 16 hex digits) and stay the same for a printer
  across restarts. They are not ElegooSlicer's internal ids, which for a discovered CC2
  or a cloud printer contain the serial number. Take them from `GET /printers`.
- **No secrets.** Printers are written field by field: access codes, PINs, passwords,
  tokens, user names, serial numbers, mainboard ids and web URLs never appear, and a
  user name/password inside a host URL is cut off.
- **Threads and timeouts.** Printer calls never run on the GUI thread. A call that does
  not finish in 10 s (CANVAS: 20 s) answers `504 printer_timeout`.
- **Errors** (every route): `404 printer_not_found`, `409 printer_offline`,
  `504 printer_timeout`, `503 printers_unavailable` (ElegooSlicer is closing, rebuilding
  its window, or a second ElegooSlicer window lost its link to the first one; retry),
  `502 {"error": "printer_error", "code": n}` for any other failure of the printer layer
  (`n` is ElegooSlicer's `PrinterNetworkErrorCode`).

### GET /printers

```json
{
  "printers": [{
    "id": "p3f9c2a71d04e8b65", "name": "Centauri Carbon 2", "model": "Elegoo Centauri Carbon 2",
    "vendor": "Elegoo", "host": "192.168.1.50", "port": null, "network": "lan",
    "firmware": "1.0.2", "connected": true, "status": "printing", "status_code": 1,
    "task": {"name": null, "file": "helmet.gcode", "progress": 42, "current_s": 3120,
             "total_s": 7400, "estimated_s": 4280, "begin_ts": null},
    "supports": {"canvas": true, "timelapse": true, "auto_leveling": true,
                 "bed_switching": true, "auto_refill": true, "filament_mapping": true}
  }],
  "selected_id": "p3f9c2a71d04e8b65"
}
```
(Values illustrative.) `selected_id` is the printer the Print dialog would preselect:
the last printer a print was sent to if it is of the project's printer model, else the
first printer of that model, else the last printer sent to, else the first printer
(`null` when there are none).
- `network`: `lan` (added by IP / discovered) or `wan` (cloud).
- `status`: `offline` when ElegooSlicer is not connected to the printer, else one of
  `idle, printing, paused, pausing, canceled, self_checking, auto_leveling,
  pid_calibrating, resonance_testing, updating, file_copying, file_transferring, homing,
  preheating, filament_operating, extruder_operating, print_completed,
  rfid_recognizing, video_composing, emergency_stop, power_loss_recovery,
  initializing, busy, error, id_not_match, auth_error, unknown`. `status_code` is the
  matching ElegooSlicer `PrinterStatus` value (`-1` for offline).
- `task`: the print in progress (`null` when idle or offline). `name` and `begin_ts`
  are always `null`: ElegooSlicer does not keep them for the current print.
- `supports.canvas`: the printer reports multi-filament support.

### GET /printers/{id}

One `Printer` as above.

### GET /printers/{id}/status

The `Printer` plus:
- `exceptions`: `[{"code": "803", "level": null, "time": null}]`, the exception codes the
  printer reports in its current status (only the code is kept by ElegooSlicer).
- `age_s`: seconds since the cache last heard from (or about) the printer, `null` if
  unknown.

Each call also asks the printer to send its status again (at most once every 2 s per
printer, without waiting): the answer updates the cache for the next call.
`?raw=1` adds `raw` (the SDK's raw status JSON, with every secret-named key removed)
and `raw_age_s`; it is read at most once every 2 s per printer. The SDK has it for
cloud printers only: a LAN printer answers `501 unsupported_on_lan`.

### GET /printers/{id}/tasks, GET /printers/{id}/files

`{"items": [...], "total": n}`, one page (`page` from 1, `size` 1-100, default 1 and 20;
else `400 bad_page | bad_size`). In the SDK both lists come from Elegoo's cloud only, so
a LAN printer answers `501 unsupported_on_lan` without asking. Items:
- tasks: `{id, name, begin_ts, end_ts, status_code}`;
- files: `{name, size_bytes, print_time_s, layers, layer_height_mm, created_ts,
  filament_g, filament_mm, print_count, last_print_ts}`.

Thumbnails are left out (they are links into Elegoo's cloud storage).

### GET /printers/{id}/canvas

```json
{
  "connected": true, "auto_refill": false,
  "active": {"unit": "0", "tray": "A1"},
  "units": [{
    "id": "0", "name": null, "model": null, "connected": true,
    "temperature_c": null, "humidity": null,
    "trays": [
      {"id": "A1", "unit": "0", "index": 0, "status": "loaded", "type": "PLA",
       "name": "PLA", "vendor": "Elegoo", "color": "#ffffff", "nozzle_temp": [190, 230],
       "bed_temp": null, "matched_preset": "Elegoo PLA @ECC2", "matched_alias": "Elegoo PLA"},
      {"id": "A2", "unit": "0", "index": 1, "status": "empty", "type": null, "name": null,
       "vendor": null, "color": null, "nozzle_temp": null, "bed_temp": null,
       "matched_preset": null, "matched_alias": null}
    ]
  }],
  "external_spool": null,
  "age_s": 0
}
```
(Values illustrative.) The same read as the GUI's CANVAS sync and Print dialogs:
- `units` and `trays` keep the printer's order. A tray's `id` is the name the GUI shows:
  `A1`..`A4` for the first unit, `B1`.. for the second. `unit` is the unit's id, `index`
  the tray's position in its unit (from 0).
- `status`: `empty | preloaded | loaded | unknown_filament | error | unknown`. The
  filament fields of an empty tray are `null`.
- `matched_preset` / `matched_alias`: the system filament preset ElegooSlicer matched to
  a loaded tray (by filament name and vendor, for the project printer's nozzle), the
  one the CANVAS sync starts from; `null` when the tray is not ready or nothing matched.
- `active`: the tray feeding the extruder, `null` when none.
- `temperature_c`, `humidity`, `bed_temp` and `external_spool` are always `null`: the
  printer layer does not provide them.

A reply is reused for 5 s (`age_s` tells how old it is), and while one request is
asking a printer, the other requests for that printer wait for its answer: the CC2
silently ignores requests for a while when it gets 3 or more in a row. Errors:
`409 canvas_unsupported` (the printer reports no multi-filament support),
`409 printer_offline`, `504 printer_timeout`.

### POST /printers/{id}/canvas/sync

Body (optional): `{"mode": "filament_and_color" | "color_only", "discard_changes": bool}`.

The GUI's "Sync filaments with MMS" without its dialogs: the CANVAS read of
`GET .../canvas` (a reply is reused for 5 s), then the same code as the Sync button.
With `filament_and_color` (the default, as the GUI: it ignores the
`sync_ams_filament_mode` preference) the project's filament slots become the synced
trays, in tray order, with the preset matched to each tray and its colour; `color_only`
keeps the slots' presets and copies the trays' colours to the first slots. Flush volumes
are recalculated as in the GUI, every plate is marked unsliced, and the printer becomes
the one the Print dialog preselects.

```json
{
  "synced": 2,
  "slots": [
    {"slot": 1, "preset": "Elegoo PLA @ECC2", "type": "PLA", "color": "#ffffff", "tray": "A1"},
    {"slot": 2, "preset": "Elegoo PETG @ECC2", "type": "PETG", "color": "#ff0000", "tray": "A3"}
  ],
  "unknown": [
    {"tray": "A2", "type": null, "reason": "empty"},
    {"tray": "A4", "type": "PLA", "reason": "fallback_preset",
     "detail": "The filament model is unknown. Generic filament presets will be used."}
  ]
}
```
(Values illustrative.)
- `slots`: every filament slot of the project after the sync; `tray` is the tray it was
  synced from (`null` for a `color_only` slot past the last synced tray).
- `synced`: how many slots took a tray's filament.
- `unknown`: the trays not taken as they are. `reason`: `empty` (left out),
  `not_matched` (left out: no filament preset matches the tray's filament, or the tray
  reports an unknown filament or an error; see `matched_preset` in `GET .../canvas`),
  `fallback_preset` (synced with a generic, the previous or another preset; `detail` is the
  line the GUI's warning dialog shows).

Errors, besides the ones of `GET .../canvas`:
- `409 {"error": "unsaved_filament_changes", "preset", "hint"}`: the Filament tab's preset
  has unsaved edits (the GUI would ask about them). Nothing was changed. Save them
  (`POST /preset/save`) or repeat with `"discard_changes": true` to drop them.
- `409 {"error": "no_compatible_filaments", "unknown": [...]}`: no tray can be synced (the
  GUI says "There are no compatible filaments"). Nothing was changed.
- `409 canvas_disconnected`: the printer reports no connected CANVAS unit.
- `400 bad_mode` (not a string) / `422 bad_mode` (unknown value), `400 bad_discard_changes`,
  `400 invalid_json`, `400 body_must_be_object`.

### GET /printers/{id}/canvas/mapping

`?plate=i` (0-based, default: the current plate; `400 bad_plate_index`, `404
unknown_plate`). Which tray each filament of the plate would print from, decided as the
Print dialog decides it (same code): the tray picked for that filament in an earlier print
when it is still loaded, else a ready tray of the same colour whose filament matches the
slot's preset. The CANVAS state is the read of `GET .../canvas` (a reply is reused for 5 s).
Read-only: nothing is saved, nothing is sent.

```json
{
  "plate": 0, "sliced": true, "ok": false,
  "filaments": [
    {"slot": 1, "preset": "Elegoo PLA @ECC2", "type": "PLA", "color": "#ffffff",
     "weight_g": 12.4, "tray": {"unit": "0", "tray": "A1", "type": "PLA", "color": "#ffffff"},
     "issues": []},
    {"slot": 3, "preset": "Elegoo PETG @ECC2", "type": "PETG", "color": "#00ff00",
     "weight_g": 3.1, "tray": null, "issues": ["no_tray"]}
  ]
}
```
(Values illustrative.)
- `filaments`: the slots the plate prints with: from its slice result when `sliced`, else
  from the filaments its objects, parts, paint and settings use.
- `weight_g`: the Print dialog's estimate (model, support, wipe tower and flush), `null`
  until the plate is sliced.
- `tray`: `unit` and `tray` as in `GET .../canvas` (`null` when no tray matched).
- `issues`: `no_tray` (the Print dialog would refuse to start: "some filaments not
  mapped"), `type_mismatch` (the tray's filament type differs from the preset's),
  `color_mismatch` (the tray's colour is another colour of ElegooSlicer's standard palette
  than the slot's, the comparison the matching itself uses), `tray_not_ready` (the tray is
  not loaded or preloaded).
- `ok`: `true` when no filament has an issue.

Errors, besides the ones of `GET .../canvas`: `409 canvas_disconnected` (the printer
reports no connected CANVAS unit; the Print dialog then sends without a mapping).

## Print control

Send the plate ElegooSlicer sliced to an Elegoo printer and start it, follow or cancel
the transfer, and pause, resume or stop the print (capability `print_control`). These
routes move a real machine, so they work under these rules:

- **Off by default.** Preferences → Remote API → **Allow sending and controlling prints**
  (`remote_api_allow_print_control`). While it is off, every route below answers
  `403 {"error": "print_control_disabled", "hint": "Preferences > Remote API > Allow
  sending and controlling prints"}`. The setting is read on every request, so switching it
  off stops the next one. `GET /status` reports it as `print_control_enabled`.
- **Only ElegooSlicer's own G-code.** What is sent is the G-code ElegooSlicer sliced for the
  current plate: the same bytes and the same file name the window's Print button sends. The
  API never takes a file or a path from the client.
- **Two steps.** `print/prepare` checks everything and answers a summary and a `plan_id`;
  nothing is sent. `print` sends that plan only: once, within 5 minutes, and only while the
  plate still holds the slice result that was checked. Slicing again, changing a setting or
  selecting another plate expires it (`410 plan_expired`). An MCP client shows the summary
  to the user and starts the print only after they confirm.
- **Always visible.** Every action shows a notification in ElegooSlicer naming the printer
  and the file, whatever "Notify on API changes" says. An action (not a `GET`) refused
  because the setting is off shows a warning too.
- **Stopping is explicit:** `POST .../stop` needs the body `{"confirm": "stop"}`.
- **First window only.** With several ElegooSlicer windows open, only the first one is
  connected to the printers; the others answer `409 {"error": "secondary_instance", "hint":
  "close the other ElegooSlicer windows"}`.
- **LAN printers only.** The Elegoo SDK sends pause, resume and stop over the LAN only, so
  the API neither starts nor controls a print on a cloud printer (`network: "wan"`): it
  could not stop it. `print/prepare`, `pause`, `resume` and `stop` answer
  `501 {"error": "unsupported_on_wan", "hint"}` for one, without sending anything.
- **Telemetry.** ElegooSlicer records the file transfer and the print start as it does for
  the window's Print button (uploaded only while logged in to an Elegoo account).

| Method | Path | Purpose |
|---|---|---|
| POST | `/printers/{id}/print/prepare` | Check a print of the current plate: summary and plan id |
| POST | `/printers/{id}/print` | Send the plan's G-code and start the print |
| GET | `/uploads/{id}` | How the sending is going |
| DELETE | `/uploads/{id}` | Stop sending, before the whole file reached the printer |
| POST | `/printers/{id}/pause` | Pause the print |
| POST | `/printers/{id}/resume` | Resume a paused print |
| POST | `/printers/{id}/stop` | Stop the print (`{"confirm": "stop"}`) |

Errors of every route, besides the printer ones above (`404 printer_not_found`,
`409 printer_offline`, `502 printer_error`, `503 printers_unavailable`,
`504 printer_timeout`): `403 print_control_disabled`, `409 secondary_instance`,
`400 invalid_json | body_must_be_object | unknown_field` (`unknown_field` names a field the
route does not know: a misspelt option is refused, not ignored).

### POST /printers/{id}/print/prepare

Body, every field optional:

```json
{"plate": 0, "bed_side": "A", "leveling": true, "timelapse": false, "auto_refill": false,
 "mapping": [{"slot": 2, "unit": "0", "tray": "A3"}]}
```

- `plate`: 0-based, default the current plate. Only the current plate can be printed, as in
  the window (and only its slice result follows every setting change at once): select
  another one first with `POST /plates/{i}/select` and slice it.
- `bed_side`, `leveling`, `timelapse`, `auto_refill`: default the Print dialog's last
  choice. An option the printer does not have is off, and asking for it answers
  `422 unsupported_option` + `option`. `timelapse` exists on LAN printers only,
  `auto_refill` with a connected CANVAS only, `bed_side` with bed switching only: `A` is the
  textured PEI side and `B` the other one, as in the Print dialog.
- `mapping`: the tray for some filament slots (`unit` and `tray` as in `GET .../canvas`;
  only loaded or preloaded trays). The other slots get the Print dialog's own choice, the
  one `GET .../canvas/mapping` shows. Nothing is saved.

```json
{
  "plan_id": "1f0e7c2a-...", "expires_in_s": 300,
  "summary": {
    "printer": "Centauri Carbon 2", "printer_id": "p3f9c2a71d04e8b65", "plate": 0,
    "file_name": "helmet_PLA_1h2m.gcode", "estimated_time_s": 3720, "filament_g": 41.27,
    "layers": 212, "bed_side": "A", "leveling": true, "timelapse": false, "auto_refill": false,
    "mapping": [
      {"slot": 1, "preset": "Elegoo PLA @ECC2", "type": "PLA", "color": "#ffffff", "weight_g": 30.1,
       "tray": {"unit": "0", "tray": "A1", "type": "PLA", "color": "#ffffff"}, "issues": []}
    ],
    "warnings": [{"code": "print_completed", "message": "the printer shows its last print as completed: make sure the bed is clear"}]
  }
}
```
(Values illustrative.)
- `file_name`: the name the file gets on the printer, the window's: the print profile's
  `filename_format`, folded to ASCII and cleaned as the Print dialog cleans it.
- `estimated_time_s`, `filament_g`, `layers`: the figures the Print dialog shows.
- `mapping`: one item per filament the plate prints with, as in `GET .../canvas/mapping`;
  empty when the printer has no connected CANVAS.
- `warnings`: `color_mismatch` (a slot prints from a tray of another colour), `no_canvas`
  (one filament and no connected CANVAS: the printer prints with the filament in its
  extruder), `print_completed` (make sure the bed is clear), `post_processing` (the print
  profile's post-processing scripts run on the file before it is sent and may rename it).

Errors (nothing was sent):
- the plate: `404 unknown_plate`, `400 bad_plate_index`, `409 plate_not_current`,
  `409 not_sliced`, `409 not_printable` + `reason` (`toolpath_outside |
  gcode_check_failed | filament_not_printable`: the window's Print button is disabled
  then too), `409 preview_only` (a G-code file is open: it was not sliced from this
  project);
- the printer: `501 unsupported_on_wan` (a cloud printer), `409 printer_busy` + `status`
  (only an `idle` or `print_completed` printer takes a print), `409 upload_in_progress` + `upload_id`, `409 printer_model_mismatch` +
  `printer_model`, `project_model` (the G-code is made for the profile's printer model; the
  Print dialog only warns), `409 not_elegoolink_profile` (the project's printer profile
  does not send through ElegooLink), `409 upload_format_unsupported` (the profile sends
  `.gcode.3mf` files), `422 filename_format_error`;
- the filaments: `409 canvas_disconnected | canvas_unsupported` + `filaments` (the plate
  prints with several filaments and the printer has no connected CANVAS; the Print dialog
  would send them without a mapping), `422 mapping_incomplete` + `issues: [{slot, issues}]`
  + `filaments` (a slot has no tray: `no_tray`, its tray is not ready or reports no colour:
  `tray_not_ready`, or
  its tray holds another filament type: `type_mismatch`; the Print dialog refuses a tray of
  another type only when the filament name differs too). Load the filament, sync the CANVAS
  (`POST .../canvas/sync`) or pick trays with `mapping`. `422 bad_mapping` (unknown slot or
  tray, tray not loaded, no CANVAS to map to), `400 bad_mapping`;
- the body: `400 bad_bed_side | bad_leveling | bad_timelapse | bad_auto_refill`;
- the CANVAS read, as `GET .../canvas`.

### POST /printers/{id}/print

Body `{"plan_id": "..."}` → `202 {"upload_id": "..."}` at once. The printer's state and the
plan's slice result are checked again first. Then, on a thread of its own, the API copies
the plate's sliced G-code (thumbnails included), runs the print profile's post-processing
scripts on the copy as the window does, and has ElegooSlicer's upload manager send it with
"upload and print" and the plan's options and trays. The Print dialog's remembered printer
and options become the plan's, as after a print from the dialog; tray choices are not saved.

Errors: `400 bad_plan_id`, `404 unknown_plan` (no such plan for this printer; a plan is used
once), `410 plan_expired` + `detail` (older than 5 minutes, or the plate was sliced again or
changed, another plate was selected, or a setting changed: prepare again),
`409 printer_busy | upload_in_progress | printer_offline`.

### GET /uploads/{id}

```json
{"status": "uploading", "progress": 42, "bytes": [5242880, 12582912],
 "message": "sending the file to the printer", "printer_id": "p3f9c2a71d04e8b65",
 "file_name": "helmet_PLA_1h2m.gcode"}
```
- `status`: `exporting` (copying the G-code, running the post-processing scripts),
  `uploading`, `success` (the file was sent and the print started), `failed` (with `code`,
  ElegooSlicer's `PrinterNetworkErrorCode`, and a `message` that says whether the file was
  sent), `cancelled`.
- `bytes`: `[sent, total]`. An upload stays readable for an hour after it ended;
  `404 unknown_upload`.

### DELETE /uploads/{id}

`{"ok": true}`: the transfer stops and the print does not start. Once the whole file reached
the printer it is too late: `409 {"error": "too_late", "status", "hint"}`; stop the print
with `POST .../stop` instead. If the last bytes went out just as the cancel arrived, the
printer still starts the print: `GET /uploads/{id}` keeps watching for a minute after a
cancel and then shows `success`.

### POST /printers/{id}/pause, /resume, /stop

`{"ok": true}` once the printer accepted the command. `stop` needs the body
`{"confirm": "stop"}` (else `400 confirm_required`). The printer's state is checked first,
from ElegooSlicer's cache: `409 not_printing` + `status` (pause: the printer is not
printing; stop: it is idle, its print completed or was canceled), `409 not_paused` +
`status` (resume). A command goes straight to the printer, one at a time per printer.
- `504 printer_timeout` + `hint`: no answer within 20 s. The printer may still carry the
  command out: check `GET /printers/{id}/status`.
- `501 {"error": "resume_unsupported", "hint": "resume on the printer's screen"}`: the
  Elegoo SDK of this build cannot resume a Centauri Carbon 2 over the LAN.
  `501 command_unsupported` for another command the SDK does not have for the printer.
  `501 unsupported_on_wan` for a cloud printer (nothing is sent).
- `409 printer_busy`, `502 printer_error` + `code`.
