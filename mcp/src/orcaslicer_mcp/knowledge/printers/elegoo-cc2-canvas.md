---
topics: [elegoo, centauri carbon 2, cc2, canvas, multi-material, multi-colour, purge, flush, prime tower, filament change, m6211, bed side, textured pei, lan only]
orca_keys: [flush_volumes_matrix, flush_multiplier, enable_prime_tower, prime_tower_width, purge_in_prime_tower, change_filament_gcode, machine_start_gcode, machine_pause_gcode, nozzle_volume, grab_length, curr_bed_type, default_bed_type, bed_texture_area, bed_exclude_area, support_mms]
---

# Elegoo Centauri Carbon 2 with the CANVAS: what the slicer does

Facts read from ElegooSlicer's code and stock profiles, with where each one comes from. Paths are in
the ElegooSlicer source at v1.5.3.5 (`3f1042e`); `elegoo-link:` is the printer SDK at `46c7b81`;
`pycentauri:` is the community CC2 library, used only where the slicer has nothing (marked as such).
Profile values are the stock "Elegoo Centauri Carbon 2 0.4 nozzle" printer and its process and
filament presets; a user preset can override any of them.

## The printer profile

- `resources/profiles/Elegoo/machine/ECC2/Elegoo Centauri Carbon 2 0.4 nozzle.json`: `printer_model`
  "Elegoo Centauri Carbon 2", `host_type` `elegoolink` (sent through Elegoo's own link, not a
  print host URL), `gcode_flavor` klipper, `printable_area` 256 x 256 mm, `printable_height` 256,
  `bed_exclude_area` the 10 x 20 mm corner from X 246 to 256, Y 0 to 20, `support_mms` 1 (the
  CANVAS handling), `manual_filament_change` 0, `purge_in_prime_tower` 0,
  `machine_pause_gcode` `M600`, `default_bed_type` 4, thumbnails 144x144.
- `resources/profiles/Elegoo/machine/fdm_elegoo_3dp_001_common.json`:
  `single_extruder_multi_material` 1 (one nozzle, several filaments) and `nozzle_volume` 107 mm3.
- Every CC2 process preset turns the prime tower on: `enable_prime_tower` 1 is set in the four
  "Standard" presets (`resources/profiles/Elegoo/process/ECC2/0.10mm Standard @Elegoo CC2 0.2 nozzle.json`
  and the 0.4, 0.6 and 0.8 nozzle ones) and every other CC2 preset inherits one of them.
  `prime_tower_width` 35 and `wipe_tower_wall_type` rib come from
  `resources/profiles/Elegoo/process/fdm_process_elegoo_common.json`.
- The profiles set no `flush_volumes_matrix` or `flush_multiplier`, so the project's own values
  apply (defaults in `src/libslic3r/PrintConfig.cpp:6848-6862`: multiplier 1.0), recalculated from
  the slot colours when the preference to auto-calculate flushing volumes is on
  (`src/slic3r/GUI/Plater.cpp:3725`).

## A filament change: M6211

- The CC2's `change_filament_gcode` raises the nozzle to 3 mm above the highest layer printed so far
  (at most `printable_height` + 0.5), then sends
  `M6211 T[next_extruder] L[flush_length] M{old_filament_e_feedrate} N{new_filament_e_feedrate}
  Q[old_filament_temp] R[nozzle_temperature_range_high] S[new_filament_temp]` and `T[next_extruder]`.
  The printer firmware does the change and the flushing; the slicer only says how much to flush
  (`L`, in mm of filament) and at which speeds and temperatures.
- `flush_length` is a flush volume divided by the new filament's cross-section. With the prime
  tower on it is the tower's purge volume for that change (`src/libslic3r/GCode.cpp:883-885`, `:959`).
  Without a tower it is `flush_volumes_matrix[old][new] x flush_multiplier`, minus
  `grab_length x 2.4`, never below 0 (`src/libslic3r/GCode.cpp:7894-7923`, `:8005`).
- The start G-code loads the first filament with `M6211 A1 L200 T[initial_no_support_extruder] ...`
  (`machine_start_gcode` of the same profile), so even a single-filament CC2 print begins with one
  filament load of 200 mm.

### How the slicer counts its time and filament

- `M6211` is only interpreted when `printer_model` starts with "elegoo"
  (`src/libslic3r/GCode/GCodeProcessor.cpp:1869-1873`); the CC2 formula is chosen by the model name
  (`src/libslic3r/GCode/ElegooGCodeProcessorHelper.cpp:123-132`).
- CC2 time per change (`ElegooGCodeProcessorHelper.cpp:86-121`): 31 s fixed (change, heating,
  moves), the first 15 mm of old filament at the `M` feedrate, then the rest of `L` (clamped to 10 to
  1000 mm) in segments of `K` mm (75 when, as on the CC2, the G-code gives no `K`), each with a
  retract, a cool time `P` (3000 ms by default) and 5 s of wiping, the last segment at 80 % speed;
  plus the cooling from `R` (the flush temperature, `nozzle_temperature_range_high` on the CC2) down
  to `S` (the new print temperature) at 1.36 C per second when `S` is lower.
- The flushed filament (`L` x cross-section) is booked as flush: the part that fills the nozzle (up
  to `nozzle_volume`) against the filament leaving it, the rest against the new one
  (`ElegooGCodeProcessorHelper.cpp:167-187`).
- Both the change time and the flushed filament are in the slice's total time and filament but in no
  feature role, so the roles of a multi-colour print don't add up to the total. On ElegooSlicer MCP,
  `get_slice_breakdown` reports them as `purge` (`filament_changes`, `filament_change_time_s`,
  `flush_g`, `flush_mm`) and `describe_plate` names them.
- To cut them: fewer changes (group colours by plate or by height), a lower `flush_multiplier` (at the
  risk of colour bleed), and checking `flush_volumes_matrix` for light-after-dark pairs.

## Changes at a layer height

- A filament switch at a height (`tool_change`) goes through the normal tool-change path, so it is an
  `M6211` like any other change.
- A pause emits `machine_pause_gcode`, which is `M600` on the CC2. Its message is not written to the
  G-code: the `M117` line is commented out (`src/libslic3r/GCode.cpp:4296-4299`).
- A colour change (the OrcaSlicer M600 item) emits no command in ElegooSlicer: the colour-change
  insertion is compiled out ("BBS: inserting color gcode is removed", `src/libslic3r/GCode.cpp:4261-4291`),
  so only a custom-code tag reaches the G-code. For a manual swap use a pause; with the CANVAS use a
  filament switch.

## Bed sides A and B

- The CC2 plate has two sides. For Elegoo CC printers the sidebar names the bed types "Textured PEI
  Plate (Side A)" (`btPTE`) and "Smooth PEI Plate (Side B)" (`btPEI`)
  (`src/slic3r/GUI/Plater.cpp:2863-2865`, `src/slic3r/GUI/Tab.cpp:4362-4363`). The profile's
  `default_bed_type` 4 is `btPTE`, side A (`src/libslic3r/PrintConfig.hpp:314-323`). The sidebar's
  bed type sets the plate temperatures the slice uses.
- The send dialog asks separately which side is on the printer: `btPTE` = A, `btPC` = B, A by default
  (`src/slic3r/GUI/Elegoo/PrintSendDialogEx.cpp:48`, `:283-288`, `:343-346`). The upload sends
  `bedType` 1 only for `btPC` (`src/slic3r/Utils/PrintHost.cpp:444-449`, passed on as
  `heatedBedType` in `src/slic3r/Utils/Elegoo/ElegooLink.cpp:611`), and the CC2 adapter turns 0 into
  `print_layout` "A" and anything else into "B" (`elegoo-link:src/lan/adapters/elegoo_fdm_cc2/elegoo_fdm_cc2_message_adapter.cpp:83`).
- `bed_texture_area` (0,-10 to 256,256) only places the bed texture drawn in the 3D view
  (`src/slic3r/GUI/PartPlate.cpp:3077-3084`); `printable_area` stays 256 x 256.

## Connecting

- The slicer reaches the CC2 over the local network through the elegoo-link SDK. When the printer
  answers an HTTP request with 429 Too Many Requests, the SDK reports the printer as busy
  (`elegoo-link:src/lan/adapters/elegoo_fdm_cc2/elegoo_fdm_cc2_http_transfer.cpp:225-229`).
- Community-reported, not in the slicer code (`pycentauri:README.md`): the CC2's local API needs
  **LAN Only** mode on (network settings on the touchscreen; required on firmware 2.0); 3 or more
  back-to-back MQTT requests trip a cooldown of a few seconds during which replies are silently
  dropped; the camera is an unauthenticated MJPEG stream on port 8080. Poll gently: ElegooSlicer MCP
  caches CANVAS reads for that reason.
