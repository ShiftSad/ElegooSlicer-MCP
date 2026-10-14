# ElegooSlicer MCP v1.5.3.5-mcp.1 (release notes, DRAFT)

> **Draft, not published.** Nothing here is released yet. Before any public release the
> licensing items at the end must be resolved, and Victor confirms the release.

ElegooSlicer MCP is **ElegooSlicer 1.5.3.5** (Elegoo tag `v1.5.3.5`, `3f1042e`) with the
Remote API of the OrcaSlicer MCP fork built in (`MaxEllis/OrcaSlicer@fa3c9ee`, release
`v2.4.2-mcp.13`). The API lets an MCP server, and through it an AI assistant, drive the
running slicer: load models, change settings, slice and read the results back.

**This is an unofficial build. It is not made or supported by Elegoo.** It shows its
version as `1.5.3.5-mcp.1` and its window title ends in "- ElegooSlicer MCP".

## What it adds

### Remote API (same as OrcaSlicer MCP v2.4.2-mcp.13)

- HTTP + WebSocket API under `http://127.0.0.1:13130/api/v1`, **off by default**
  (Preferences > Remote API). Every request needs the token shown there; it listens on
  localhost only unless you turn on *Bind LAN*.
- Status, config read/write (all-or-nothing), slicing with status and cancel, model
  loading, presets, G-code export, objects (delete, transform, duplicate, move to a plate,
  per-object settings, layer height, height ranges), plates (list, add, select, delete),
  plate renders (editor and preview), arrange, orient, job status and a live event stream.
- Works with the unmodified `orcaslicer-mcp` 0.1.17 server.

### New in ElegooSlicer MCP

- Orient a single object: `POST /orient {"object_id": ...}`.
- Switch between the Prepare and Preview tabs: `POST /view`; `/status` reports `view`.
- Part volumes: list, add (box, cylinder or sphere as modifier, negative volume, support
  enforcer or blocker), configure and remove.
- Save the project as `.3mf`: `POST /project/save`.
- Paint support, seam or colour on the triangles inside a box, optionally only on
  overhangs: `POST /objects/{id}/paint`.
- `/status` identifies the build: `app` = ElegooSlicer, `app_version`,
  `orca_compatible_version`, `mcp_build`.
- Multi-material project: list the filament slots, set their number, preset and colour
  (`/filaments`); split an object into objects or parts; undo and redo; filament changes
  and pauses at a height (`/plates/{i}/layer_changes`); the slice breakdown reports the
  filament changes, their time and the flush (`purge`).
- Calibration tests from the Calibration menu without their dialogs (`POST /calibration`).
- Elegoo printers (capability `printers`): list them, read their status and, for a
  Centauri Carbon 2, the CANVAS trays with the filament preset matched to each; sync the
  CANVAS into the project like the Sync button; preview which tray each filament prints
  from. Printer ids are opaque and no access code, serial number or other secret ever
  appears in a reply.
- Sending and controlling prints (capability `print_control`), **off by default** behind
  Preferences > Remote API > *Allow sending and controlling prints*: a two-step
  prepare/print that only sends the G-code ElegooSlicer sliced for the current plate,
  upload status and cancel, pause and stop (resume is not possible over the LAN with
  Elegoo's SDK: use the printer's screen). LAN printers only. Every action shows a
  notification in the window.
- No modal dialog during API calls: error and info boxes, settings questions and
  model-load questions become notifications (and `skipped_dialogs` in the reply).
- Config writes refused as a whole now carry `"error": "config_rejected"`.
- The MCP server with the ElegooSlicer tools ships in this repository's `mcp/` folder
  (printers, CANVAS, two-step printing with a confirmation the user must give, camera
  snapshot, filament slots, split, undo/redo, layer changes, calibration).

### Fixes and safety changes compared with the OrcaSlicer MCP fork

- Rotations are applied about the world axes (no more gimbal-lock surprises), and objects
  are dropped back onto the bed after a move, rotation or scale.
- Loading a missing file answers `404 file_not_found` with the path.
- Config and object edits mark every affected plate as unsliced.
- Read-only settings are refused (`422 readonly_key`); Elegoo's `support_mms` and
  `support_wan_network` count as sensitive settings.
- No modal dialog from Elegoo's Beginner Guide during API calls; plate renders make sure
  the OpenGL context is ready; Elegoo's sticky "arrange outside the bed" state is reset
  before API arrange/orient.
- Windows: only one ElegooSlicer window can own the API port; a second window shows
  "Remote API could not listen on port ..." instead of being silently shadowed.
- The in-app update check only looks at this project's GitHub releases. It never offers
  the official ElegooSlicer, which would remove the API.
- Reproducible builds: the elegoo-link printer SDK is pinned to `46c7b81`; includes
  Elegoo's later ADMesh STL-reader overflow fix.

### Not tested on a real printer yet

The printer, CANVAS and print routes were tested against an emulated Centauri Carbon 2
built from the elegoo-link SDK's protocol (`tools/fakecc2` in the workspace), not yet on
the real printer. Field values the SDK leaves to the firmware (the printer model string,
the camera address, tray numbering) are confirmed only once a real CC2 is connected.

## Install (Windows x64)

1. Download `ElegooSlicer-MCP_v1.5.3.5-mcp.1_Windows_x64_portable.zip` and `SHA256SUMS`.
2. Check the download. In PowerShell:
   `(Get-FileHash .\ElegooSlicer-MCP_v1.5.3.5-mcp.1_Windows_x64_portable.zip -Algorithm SHA256).Hash`
   must match the line in `SHA256SUMS` (case does not matter). In Git Bash:
   `sha256sum -c SHA256SUMS`.
3. Extract the zip anywhere and run `ElegooSlicer-MCP\elegoo-slicer.exe`. The binaries are
   not code-signed, so Windows SmartScreen may warn: *More info* > *Run anyway*. Like the
   official app, it needs the Microsoft Edge WebView2 Runtime and the Visual C++ x64
   runtime (both are usually already on Windows 11).
4. The `data_dir` folder inside the zip keeps this build's settings, presets, logs and
   saved printers apart from an official ElegooSlicer (`%APPDATA%\ElegooSlicer`). Do not
   share that folder: saved printers include their access codes.
5. Enable the API: Preferences > Remote API > Enable, and copy the token.
6. Connect an MCP client to the server in `mcp/`:
   `uvx --from "git+https://github.com/ShiftSad/ElegooSlicer-MCP@v1.5.3.5-mcp.1#subdirectory=mcp" orcaslicer-mcp`,
   with the environment variable `ORCA_API_TOKEN` set to the token (`ORCA_API_URL`
   defaults to `http://127.0.0.1:13130`). See `mcp/README.md` for Claude Desktop.

Read [Privacy and network](README.md#privacy-and-network) before logging in to an Elegoo
account in this build (telemetry, cloud, data directory).

## Checksums

```
<sha256 from the CI run's SHA256SUMS>  ElegooSlicer-MCP_v1.5.3.5-mcp.1_Windows_x64_portable.zip
```

Built by the `Build ElegooSlicer MCP (Windows)` workflow, run `<run URL>`, commit `<sha>`.

## Source code

ElegooSlicer, the OrcaSlicer MCP fork and this build are licensed under the AGPL-3.0.
The exact source of this release is the tag
[`v1.5.3.5-mcp.1`](https://github.com/ShiftSad/ElegooSlicer-MCP/tree/v1.5.3.5-mcp.1)
of `ShiftSad/ElegooSlicer-MCP` (GitHub also offers it as a `.zip`/`.tar.gz` on the release
page). Credits: Elegoo (ElegooSlicer), the OrcaSlicer, Bambu Studio and PrusaSlicer
projects, and MaxEllis (Remote API and `orcaslicer-mcp`).

## Open items before any public release (licensing, D11)

A public binary would redistribute files that this project did not build from source or
whose terms are unclear. Each needs a decision before the first public release:

- **Agora RTM SDK DLLs** (`agora_rtm_sdk.dll`, `libaosl.dll`): closed-source, delivered
  with the elegoo-link SDK and copied into the install folder by the build. Check Agora's
  redistribution terms, or find out whether the build works without them.
- **Device-tab web UI** (`resources/plugins/elegoolink/web/lan_service_web` and
  `cloud_service_web`): downloaded at build time (`dlweb`) from a GitHub release of
  Elegoo's `elegoo-fdm-web` repository. It is a minified bundle with no license and no
  public source. Options: ask Elegoo, or release without it (the Device tab would then be
  empty).
- **Prebuilt GMP and MPFR** (`libgmp-10.dll`, `libmpfr-4.dll`): copied from prebuilt
  binaries in `deps/GMP` and `deps/MPFR`, not built from source. They are LGPL, so a
  release must say which versions they are and where their source is.
- Also confirm that the release page links the exact source tag (AGPL-3.0) and keeps the
  upstream license and credit files that the install tree carries.
