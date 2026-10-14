# PORT_NOTES — ElegooSlicer MCP port

Running notes for the port described in `PORT_PLAN.md`. Read this first in every new session.
Line numbers refer to ElegooSlicer **v1.5.3.5 (`3f1042e`)** unless prefixed (`fork:` = `MaxEllis/OrcaSlicer@fa3c9ee`, `orca242:` = OrcaSlicer `v2.4.2` = `8500fcd`, `mcp:` = `orcaslicer-mcp@v0.1.17`, `sdk:` = `elegoo-link@46c7b81`).

## 1. Phase status

| Phase | State | Date | Notes |
|---|---|---|---|
| 0 — Recon | **Done, waiting for Victor's review** | 2026-10-13 | No code changed. This file + `PORT_PLAN.md` are untracked in the checkout. |
| 1 — prerequisites | **Done** | 2026-10-13 | GitHub fork, Perl, toolchain, deps build (~20 min) and **baseline build of unmodified v1.5.3.5** (~18 min, 0 errors, app starts) — see section 7. |
| 1 — 1:1 port | **Done, waiting for Victor's test** | 2026-10-13 | Branch `remote-api` (see section 11): port + adaptations + review fixes, built, runtime-check 16/16, MCP 0.1.17 smoke 12/12 on the CC2 profile. |
| 3 (part) + 3B | **Pulled forward, built and tested, waiting for Victor's test** | 2026-10-14 | Victor's request after the Claude Desktop sessions: orient one object, view switch, part volumes, project save, region painting, stale-slice fix (section 11.2). |
| 2 — printers (read) | **Done, tested against the fake CC2, waiting for Victor + the real printer** | 2026-10-14 | Section 12. Real-printer acceptance after 17/10. |
| 3 (rest), 5 | **Done, built and tested** | 2026-10-14 | Filament slots, split, undo/redo, layer changes, calibration, CANVAS sync + mapping (GUI parity checked), purge breakdown, D8 modal guards (section 12). |
| 4 — print control | **Done, tested against the fake CC2 only** | 2026-10-14 | Off by default; byte-identical upload vs the window's Print checked. Phase 4.4 (cube on the real CC2 with Victor) still to do. |
| 6 — MCP server | **Done, vendored in `mcp/`** | 2026-10-14 | `git subtree` of orcaslicer-mcp `elegoo` (history kept). pytest 1438 passed / 4 skipped. `.mcpb` not packed (needs `npx`). |
| 7 — CI/release | **CI green, merged** | 2026-10-14 | `build_elegoo_mcp.yml` merged into `remote-api` (Victor: "se é de graça ta liberado dar merge"; public repo, standard runners are free). Green run 38592205059. No release (D11 open, Victor's go needed). |

## 2. Workspace

Root: `C:\Users\rosal\Documents\elegooslicer-mcp\`

| Path | What |
|---|---|
| `ElegooSlicer/` | Partial clone (`--filter=blob:none`) of `elegooofficial/ElegooSlicer`, **detached at `v1.5.3.5`**. Remotes: `origin` = **`ShiftSad/ElegooSlicer-MCP`** (GitHub fork, public, created 2026-10-13), `elegoo` (upstream), `orcamcp` (`MaxEllis/OrcaSlicer`; fetched `remote-api-242`, tags `v2.4.2-mcp.*`). Tag `v2.4.2` (Orca upstream) fetched too. Lazy blob fetches may print `fatal: remote error: upload-pack: not our ref` (harmless: git tries the `elegoo` promisor first). |
| `tools/build.cmd` | **Build wrapper** — sets up VS 2022 Build Tools v143 + bundled CMake 3.31.6 + portable Perl, clears `NoDefaultCurrentDirectoryInExePath` (set by the Claude Code shell; breaks the Elegoo/deps scripts), then calls `build_release_windows.bat %*` at **below-normal priority, pinned to 24 of 32 logical CPUs** (`BUILD_AFFINITY`, default `0xFFFFFF`; Victor uses the machine during builds — `0xFFFF` = 16 threads when busy, `0xFFFFFFFF` = all threads). Run e.g. `cmd /c tools\build.cmd onlydeps > tools\logs\deps.log 2>&1`. |
| `tools/strawberry-perl-5.42.3.1/` | Strawberry Perl 5.42.3.1 portable (GitHub release `SP_54231_64bit`, zip SHA-256 `6a081a81…a10690` verified against the GitHub asset digest). Only `perl\bin` goes on PATH, and only inside `build.cmd`. No system change. |
| `tools/logs/` | Build logs and disk measurements. |
| `tools/test-datadir/` | The TEST data dir every runtime check uses. |
| `ElegooSlicer/tools/remote-api/` | Since 2026-10-14 the workspace scripts this file names as `tools\build.cmd`, `tools\phase*.py`, `tools\fakecc2`, `tools\run_all_checks.sh` and `tools/overnight/CONTRACT.md` live in the repository here (README there). Perl, logs and the test data dir stay in the workspace `tools/`. |
| `orcaslicer-mcp/` | `MaxEllis/orcaslicer-mcp` at `v0.1.17` (`ccfacb4`). Has a `.venv` from the test run. |
| `elegoo-link/` | SDK, `main` = `46c7b81` (reference only). |
| `pycentauri/` | Community CC2 library (reference only, shallow). |
| `phase0/remote-api.patch` | `git diff 8500fcd fa3c9ee -- src version.inc build_release_vs.bat docs/remote-api` — **28 files, 57 hunks**. |
| `phase0/orca242/`, `phase0/fork/` | Extracted source trees of Orca 2.4.2 and the fork (for diffing without checkouts). |
| `phase0/applytest/` | Sparse git worktree of `v1.5.3.5` with the patch applied via `git apply --reject` (`*.rej` for the 10 rejected hunks). Registered in `ElegooSlicer`'s `git worktree list`; remove with `git worktree remove ../phase0/applytest` when no longer needed. |
| `phase0/recon/` | Full Phase 0 recon output (per-area reports, verifier verdicts, raw workflow JSON). `B-hunks.md`, `C-calls.md`, `D-findings.md` have every detail behind this file. |

Verified SHAs (2026-10-13, `git ls-remote`): `v2.4.2-mcp.13` = `fa3c9ee` = current tip of `orcamcp/remote-api-242` (no newer fork commits); `v2.4.2` = `8500fcd`; `v1.5.3.5` = `3f1042e`; `elegoo-link` main = `46c7b81` (SDK version string 1.0.6, untagged); MCP `v0.1.17` = `ccfacb4`. `merge-base(8500fcd, v1.5.3.5)` = `19db9aa` (2026-06-28), so Orca↔Elegoo diffs are meaningful.

Reproduce the apply test:
```bash
cd ElegooSlicer
git diff 8500fcd fa3c9ee -- src version.inc build_release_vs.bat docs/remote-api > ../phase0/remote-api.patch
git apply --check -v ../phase0/remote-api.patch       # fails at the 10 hunks below
```

## 3. Corrections to PORT_PLAN.md §2 (deviations found in Phase 0)

Everything not listed here was confirmed. Each item below was re-checked by an independent verifier.

| Plan claim | Reality |
|---|---|
| 2.3 "10 rejected hunks are all branding/version/updater" | Count confirmed (GUI_App.cpp 4, Plater.cpp 2, GUI_App.hpp 1, AboutDialog.cpp 1, libslic3r_version.h.in 1, version.inc 1). But **3 are structural**: GUI_App.hpp #1 (forward declaration), GUI_App.cpp #4 (constructs the server) and #9 (defines `start/stop_remote_api`, `remote_api_server`). Without them: compile error, link error, or null dereference on every exit. |
| 2.3 "the rest applies clean" | Applies clean, but **`RemoteAPIController.cpp:279` uses `SoftFever_VERSION` → compile error** on Elegoo. Must become `ELEGOOSLICER_VERSION` (plan Phase 1.1 already asks for the `/status` change). |
| 2.3 Build Windows: CI uses `build_release_vs2022.bat` | `build_elegoo.yml` (the only caller of `build_release_vs2022.bat`) is **not called by any workflow**; the wired chain is Orca's unchanged one (`build_release_vs.bat`, `OrcaSlicer_dep` cache path, `SoftFever_VERSION` grep) and has **never run** on the public repo (all 25 runs are dependabot). The supported script is **`build_release_windows.bat`** (README, `doc/DEVELOPER.md`). It is the only one that passes `-DELEGOO_INTERNAL_TESTING=0` (`:233`, `:278`); without it `libslic3r_version.h` gets an empty `#if ELEGOO_INTERNAL_TESTING` → MSVC **C1017** (reproduced with cl 19.44). |
| 2.3 "Cloud features depend on a private `.env`; own builds work LAN-only" | **Wrong.** Production IoT/login/update/telemetry URLs are hard-coded in `libslic3r_version.h.in:37-52` (when `ELEGOO_INTERNAL_TESTING=0`) and `AGORA_APP_ID` is hard-coded in `sdk:src/cloud/services/rtm_service.cpp:12`. Without `.env` only Sentry crash upload and code signing are lost. A self-built binary is **not** LAN-only. `ENABLE_CLOUD_FEATURES=ON` is effectively required (`ElegooLink.cpp:258-266` writes `cfg.cloud.*` unconditionally; `CMakeLists.txt:881-882` copies the Agora DLLs). |
| 2.3 Status comes from `PrinterCache` | Only connection, status, task and attributes. **CANVAS (`getPrinterMmsInfo`) is a live MQTT request every call**; task list, file list, exception history and `getPrinterStatusRaw` are **cloud-only** in the SDK (fail or hit Elegoo's cloud for LAN printers). |
| 2.3 SDK `setAutoRefill`/`getCanvasStatus` not exposed by the wrapper | `getCanvasStatus` **is** wrapped (as `ElegooLink::getPrinterMmsInfo`, `ElegooLink.cpp:695-769`); `setAutoRefill` is used inside `sendPrintTask`. Not exposed: `pausePrint`, `resumePrint`, `stopPrint`. **`resumePrint` returns `OPERATION_NOT_IMPLEMENTED` for CC2 over LAN** in SDK 46c7b81 (method 1023 is commented out, `sdk:elegoo_fdm_cc2_message_adapter.cpp:1399`); pycentauri confirms the firmware accepts 1023. |
| 2.3 `convertPrinterNetworkInfoToJson` includes password/token/accessCode/pinCode | Also `username`, `serialNumber`, `mainboardId`, `webUrl`, `extraInfo` (`PrinterNetworkInfo.cpp:95-118`; `json::parse(extraInfo)` can throw). The same secrets are stored in plain text in `data_dir/user/printer_list.json`. |
| 2.3 `MultiInstanceCoordinator` routes slave calls to the master | True for `PrinterManager`, **not** for `PrinterUploadManager::startAsyncUpload/getUploadTask/cancelUploadTask` (no `isMaster` check). `getUploadTask` deletes a finished task on first read. |
| 2.3 Send flow: `p->export_gcode(..., upload_job)` exports the file | It only schedules; the copy happens in `BackgroundSlicingProcess::prepare_upload`. This path **opens modals** (slicing-warnings `MessageDialog`, "Another export job is running", `show_error` on upload failure) → the API must not reuse it (see 10.3). |
| 2.3 CANVAS sync via `PlaterExt::sync_mms_filament` | Also reachable modal: `Tab::select_preset` → `UnsavedChangesDialog` when the filament preset is dirty (`PlaterExt.cpp:191`, `Tab.cpp:6060`; `force_select` does not prevent it). |
| 6.2 sync `mode` defaults to the `sync_ams_filament_mode` preference | The Elegoo GUI **ignores** that preference (hard-codes `color_only=false`, `PlaterExt.cpp:148`; preference UI is `#if 0`, `Preferences.cpp:1906-1931`). Default to `filament_and_color` to match the GUI. |
| 2.4 ">3 MQTT requests trigger a cooldown" | **3 or more** back-to-back requests. The SDK does not mitigate it (dropped replies surface as 3 s/15 s SDK timeouts). |
| 2.4 "No UDP discovery" | pycentauri's claim is about the CC1-style probe. The SDK has CC2 UDP discovery on **port 52700** (method 7000) used by the Device tab; whether the CC2 answers it is unverified until the printer arrives. Manual add by IP + access code works at code level without cloud/login. |
| 2.1 ">500 tests, `uv run pytest`" | **1202 tests.** Plain `uv run pytest` fails on a fresh clone (dev deps are an optional extra): use `uv run --extra dev pytest`. **Windows baseline: 1183 passed / 15 failed / 4 skipped** (all 15 are Windows-only test assumptions). |
| 2.1 `target.py` resolves the printer by `host_type` | Resolution uses the profile's `print_host`; `host_type` is only a deny-list and the protocol comes from probing. The stock CC2 profile has `host_type: elegoolink` and no `print_host` → `not_configured`. |
| 2.1 guard.py list | Also blocks `printer_technology`, `filename_format`, `bbl_use_printhost`, `printer_agent`, `flashforge_serial_number`, `printhost_*`. `support_mms`/`support_wan_network` not yet included. |
| 2.5 "VS 2022" | Installed: **VS 2022 Build Tools 17.14** (MSVC 14.44 = v143, Windows SDK 10.0.26100, bundled CMake **3.31.6**) **and** VS Community 2026 18.4 (MSVC 14.50). `cmake` on PATH is **4.3.3** (README asks for < 4.0 on Windows). **No Perl** (required by the OpenSSL dep), no NASM (not needed). |
| 2.5 / 9 Telemetry "apparently tied to login" | Events are **always collected and cached on disk** (no opt-out); upload happens only while logged in (also later, after a login). API-driven loads, slices and uploads emit events through existing Elegoo hooks (`report_model_import` Plater.cpp:13980, `report_slice_started` :9997, `report_slice_completed` :10150, `report_printer_file_transfer`/`print_job_start` in `PrinterUploadManager`). |
| (not in plan) | The CC2 **Device tab web UI (`lan_service_web`) is not in git**; it is downloaded by `scripts/download_web_dep.bat` (`dlweb` arg of `build_release_windows.bat`, OFF by default) from a GitHub release of `elegooofficial/elegoo-fdm-web` (minified bundle, no license, no public source). |
| (not in plan) | `docs/` is gitignored in Elegoo (`.gitignore:62`) → `docs/remote-api/` needs `git add -f`. |
| (not in plan) | Elegoo-only **modal on POST /model**: `select_tab(tp3DEditor)` (Plater.cpp:7131) → `TryShowBeginnerGuideOnPreparePage` → `BeginnerGuideView::ShowModal` (GUI_App.cpp:4363) when `beginner_guide_shown` is false and the current tab is not Prepare. |
| (not in plan) | Elegoo-only **sticky `Job::PREPARE_STATE_OUTSIDE_BED`** (Alt+A): afterwards the fork's `POST /orient` is a silent no-op and `POST /arrange` moves only off-bed objects (both still return 202). |
| (not in plan) | On Windows the fork's `reuse_address(true)` (`fork:RemoteAPIServer.cpp:182`) lets a **second instance bind 13130 too** (verified with a loopback test on this machine); the second instance is silently shadowed. Plan Phase 1.2 ("port in use" notice) cannot work without changing this (D7). Elegoo allows multiple instances by default (`single_instance=false`). |

## 4. Hook map (all 57 hunks)

`Status`: **R** = rejected by `git apply` (resolve by hand), C = applied clean (verified to land in the semantically equivalent place). "none" = apply verbatim (add an `// elegoo-mcp:` marker on hooks in existing Elegoo files, per rule 2).

| # | File | Hunk | St | Fork hook (purpose) | ElegooSlicer location | How to adapt |
|---|---|---|---|---|---|---|
| 1 | `build_release_vs.bat` | 1 | C | 4× `if errorlevel 1 exit /b 1` after deps configure/build | `:128-134` | Not used by the Elegoo build (`build_release_windows.bat`). Recommend **drop** (D18). |
| 2 | `build_release_vs.bat` | 2 | C | 5× fail-fast after slicer configure/build/install | `:147-158` | Same as #1. |
| 3 | `docs/remote-api/README.md` | 1 | C | New API docs | new file | `docs/` is gitignored → `git add -f` (or move to `doc/remote-api/`, D19). Rebrand title and the `/status` example (README.md:56). |
| 4 | `docs/remote-api/openapi.yaml` | 1 | C | New OpenAPI spec | new file | Same; update `Status` schema (`app`, `app_version`, add `orca_compatible_version`, `mcp_build`). |
| 5 | `src/libslic3r/Format/bbs_3mf.cpp` | 1 | C | `_BBS_Backup_Manager::defer_backup()` | after `set_interval()` `:8701-8709` | none |
| 6 | `src/libslic3r/Format/bbs_3mf.cpp` | 2 | C | free `backup_defer(long)` | after `backup_soon()` `:9054` | none |
| 7 | `src/libslic3r/Format/bbs_3mf.hpp` | 1 | C | `extern void backup_defer(long)` | after `:290` | none |
| 8 | `src/libslic3r/libslic3r_version.h.in` | 1 | **R** | `ORCA_MCP_RELEASE`, `_STR`, `ORCA_MCP_VERSION` | after `:7` (`#define ELEGOOSLICER_VERSION`) | Add `ELEGOO_MCP_RELEASE @ELEGOO_MCP_RELEASE@`, `ELEGOO_MCP_RELEASE_STR "@ELEGOO_MCP_RELEASE@"`, `ELEGOO_MCP_VERSION ELEGOOSLICER_VERSION "-mcp." ELEGOO_MCP_RELEASE_STR` (simulated: expands to `"1.5.3.5" "-mcp." "1"`). |
| 9 | `src/slic3r/CMakeLists.txt` | 1 | C | 6 RemoteAPI sources in `SLIC3R_GUI_SOURCES` | after `GUI/HttpServer.hpp` `:299` | none (Phase 2 adds `RemoteAPIElegoo.*` here) |
| 10 | `src/slic3r/GUI/AboutDialog.cpp` | 1 | **R** | version label = `format_display_version()` | `:247` `_L("ElegooSlicer ") + " " + std::string(SLIC3R_BUILD_ID)` | Replace only `std::string(SLIC3R_BUILD_ID)` with `GUI_App::format_display_version()`; keep the `(Test)` suffix and the "Based on OrcaSlicer" line. |
| 11 | `src/slic3r/GUI/GCodeViewer.cpp` | 1 | C | `render_toolpaths_with_camera()` | before `render_toolpaths()` `:2301` | none |
| 12 | `src/slic3r/GUI/GCodeViewer.hpp` | 1 | C | fwd decl `class Camera;` | `:32` | none (MSVC C4099 warning, same as fork) |
| 13 | `src/slic3r/GUI/GCodeViewer.hpp` | 2 | C | decl `render_toolpaths_with_camera` | after `enable_legend()` `:340` | none |
| 14 | `src/slic3r/GUI/GLCanvas3D.cpp` | 1 | C | offscreen `render_plate_thumbnail` / `render_gcode_thumbnail` | before `render_thumbnail_internal` `:6196` | none required; recommended GL guard `if (!_set_current() \|\| !wxGetApp().init_opengl()) return false;` (Elegoo's own pattern at `:2282`) — D8 |
| 15 | `src/slic3r/GUI/GLCanvas3D.hpp` | 1 | C | decls of the two renderers | before `render_thumbnail_internal` `:955` | none |
| 16 | `src/slic3r/GUI/GUI_App.cpp` | 1 | C | include RemoteAPI headers before `GUI_App.hpp` | `:3-4` | none |
| 17 | `src/slic3r/GUI/GUI_App.cpp` | 2 | C | splash version y 0.72→0.79 (fork's own SVG) | `SplashScreen::OnPaint` `:340` | **Drop**: Elegoo's splash SVG is different; keep 0.72. |
| 18 | `src/slic3r/GUI/GUI_App.cpp` | 3 | C | `post_init` → `start_remote_api()` | before "finished post_init" `:1077` | none |
| 19 | `src/slic3r/GUI/GUI_App.cpp` | 4 | **R** | constructor: `m_remote_api_server = std::make_unique<RemoteAPI::Server>();` | `GUI_App::GUI_App()` `:1105` (before `init_app_config()` `:1107`) | Insert verbatim after the `{`. Rejected only because Elegoo's comment says `ElegooSlicer.cpp` and it added `CrashReporter::init`. **Mandatory.** |
| 20 | `src/slic3r/GUI/GUI_App.cpp` | 5 | C | `shutdown()` → `stop_remote_api()` | after `removable_drive_manager()->shutdown()` `:1136` | none (Phase 2: Elegoo closes `PrinterManager`/IPC earlier in `MainFrame::shutdown`, see 10.1) |
| 21 | `src/slic3r/GUI/GUI_App.cpp` | 6 | C | `recreate_GUI` → `bind_plater_events()` | after `update_publish_status()` `:4300` | none |
| 22 | `src/slic3r/GUI/GUI_App.cpp` | 7 | **R** | updater: query fork releases, compare `-mcp.N`, stock code unreachable | `check_new_version_sf` `:5871`; insert at `:5876` (after the existing `#ifdef __LINUX__ return` and before `#if 1 // Elegoo`) | Copy the fork block verbatim (braces, `parse_mcp_ordinal`, numeric `skip_version`), with URL `https://api.github.com/repos/<owner>/ElegooSlicer-MCP/releases/latest` (D1) and `ELEGOO_MCP_RELEASE`. Elegoo's code stays below, unreachable. The `EVT_SLIC3R_VERSION_ONLINE` handler is identical to the fork's. |
| 23 | `src/slic3r/GUI/GUI_App.cpp` | 8 | **R** | `format_display_version()` → MCP version | `:6218` `version_display = ELEGOOSLICER_VERSION;` | `version_display = ELEGOO_MCP_VERSION;` (feeds splash `:396` and About clipboard). |
| 24 | `src/slic3r/GUI/GUI_App.cpp` | 9 | **R** | define `remote_api_server()`, `start_remote_api()`, `stop_remote_api()` | after `stop_sync_user_preset()` (ends `:7400`); insert at `:7401` | Insert verbatim (rejected only because Elegoo put `#if 0` in `restart_sync_user_preset`). **Mandatory.** Phase 1.2: after `start(cfg)`, if `!running()` log + in-app notification "Remote API port in use" (needs D7). |
| 25 | `src/slic3r/GUI/GUI_App.cpp` | 10 | C | `open_preferences()` → restart API after the dialog | after `dlg.ShowModal()` `:8298` | none |
| 26 | `src/slic3r/GUI/GUI_App.hpp` | 1 | **R** | `namespace Slic3r { namespace GUI { namespace RemoteAPI { class Server; class Controller; } } }` | after `:21` (`#include "slic3r/GUI/HttpServer.hpp"`) | Insert verbatim (rejected because of Elegoo's `#include "Elegoo/TelemetryEvents.hpp"` at `:23`). **Mandatory** (every TU including `GUI_App.hpp` fails otherwise). |
| 27 | `src/slic3r/GUI/GUI_App.hpp` | 2 | C | members `m_remote_api_server`, `m_remote_api_controller` | after `m_http_server` `:341` | none |
| 28 | `src/slic3r/GUI/GUI_App.hpp` | 3 | C | decls `start_remote_api`/`stop_remote_api`/`remote_api_server` | after `stop_http_server()` `:564` | none |
| 29 | `src/slic3r/GUI/MainFrame.cpp` | 1 | C | include `RemoteAPIController.hpp` | `:1` | none |
| 30 | `src/slic3r/GUI/MainFrame.cpp` | 2 | C | comment em-dash → `-` | `:160` | Recommend **drop** (no effect; MSVC `/utf-8` already on) — D18 |
| 31 | `src/slic3r/GUI/MainFrame.cpp` | 3 | C | auto-backup ↔ API mutual exclusion in `set_backup_callback` | `:798-808` | none (Elegoo has the same backup mechanism and defaults) |
| 32 | `src/slic3r/GUI/MainFrame.cpp` | 4 | C | comment em-dash | `:1239` | Recommend **drop** — D18 |
| 33 | `src/slic3r/GUI/NotificationManager.hpp` | 1 | C | `NotificationType::RemoteAPIChange` | before `NotificationTypeCount` `:168` | none |
| 34 | `src/slic3r/GUI/Plater.cpp` | 1 | C | include `RemoteAPIController.hpp` | `:1` | none |
| 35 | `src/slic3r/GUI/Plater.cpp` | 2 | C | comment arrow → `->` | `:244` | Recommend **drop** — D18 |
| 36 | `src/slic3r/GUI/Plater.cpp` | 3 | C | comment arrows | `:3402` | Recommend **drop** — D18 |
| 37 | `src/slic3r/GUI/Plater.cpp` | 4 | C | skip "Multi-part object detected" dialog while `api_ui_task_active()` | `priv::load_files` `:6891` | none |
| 38 | `src/slic3r/GUI/Plater.cpp` | 5 | **R** | title `" - OrcaSlicer MCP"` in `set_project_name` | `:11006` (non-Apple branch) | `SetTitle(m_project_name + " - " SLIC3R_APP_NAME " MCP");`. macOS branch `:11002` unchanged for fork parity (D12). |
| 39 | `src/slic3r/GUI/Plater.cpp` | 6 | **R** | same in `update_title_dirty_status` | `:11026` | `SetTitle(title + " - " SLIC3R_APP_NAME " MCP");` (must change together with #38). |
| 40 | `src/slic3r/GUI/Plater.cpp` | 7 | C | `notify_project_opened()` at end of `load_project` | after `m_loading_project = false;` `:12415` | none |
| 41 | `src/slic3r/GUI/Plater.cpp` | 8 | C | new `Plater::stop_background_slicing()` | after `is_background_process_slicing()` `:18393` | none |
| 42 | `src/slic3r/GUI/Plater.hpp` | 1 | C | fwd decls `BackgroundSlicingProcess`, `SlicingProcessCompletedEvent` | after `class SlicingStatusEvent;` `:52` | none |
| 43 | `src/slic3r/GUI/Plater.hpp` | 2 | C | `wxDECLARE_EVENT(EVT_PROCESS_COMPLETED, ...)` | after `EVT_SLICING_UPDATE` `:92` | none (defined at Plater.cpp:199) |
| 44 | `src/slic3r/GUI/Plater.hpp` | 3 | C | decl `stop_background_slicing()` | after `reslice()` `:508` | none |
| 45 | `src/slic3r/GUI/Preferences.cpp` | 1 | C | include `RemoteAPIConfig.hpp` | `:21` | none |
| 46 | `src/slic3r/GUI/Preferences.cpp` | 2 | C | `create_remote_api_page()` call (last tab) | `create_items()`, before `g_sizer = nullptr;` `:2066` (outside every Elegoo `#if 0`) | none |
| 47 | `src/slic3r/GUI/Preferences.cpp` | 3 | C | "Remote API" tab | after `create_debug_page()` `:2225` | Rebrand 3 tooltips "OrcaSlicer" → "ElegooSlicer" (D20). Phase 4 adds `remote_api_allow_print_control` here. |
| 48 | `src/slic3r/GUI/Preferences.hpp` | 1 | C | decl `create_remote_api_page` | after `create_item_network_plugin_version` `:102` | none |
| 49 | `src/slic3r/GUI/RemoteAPI/RemoteAPIConfig.cpp` | 1 | C | new: settings + `RAND_bytes` token | new | none (Elegoo uses OpenSSL 3.1.8 vs 1.1.1w; `RAND_bytes` already used in Elegoo) |
| 50 | `src/slic3r/GUI/RemoteAPI/RemoteAPIConfig.hpp` | 1 | C | new | new | none |
| 51 | `src/slic3r/GUI/RemoteAPI/RemoteAPIController.cpp` | 1 | C | new: routes/handlers | new | **`:279` `SoftFever_VERSION` → `ELEGOOSLICER_VERSION`** + add `orca_compatible_version`, `mcp_build`; add `support_mms`, `support_wan_network` to `is_sensitive_config_key` (`:371-382`, rule 3). Optional: reset prepare state in arrange/orient (D8). |
| 52 | `src/slic3r/GUI/RemoteAPI/RemoteAPIController.hpp` | 1 | C | new | new | none (Phase 2: one declaration for the Elegoo dispatch entry) |
| 53 | `src/slic3r/GUI/RemoteAPI/RemoteAPIServer.cpp` | 1 | C | new: Beast HTTP+WS server | new | Windows: replace `reuse_address(true)` (`:182`) by `SO_EXCLUSIVEADDRUSE` under `#ifdef _WIN32` (D7). Optional rebrand of `Server: orca-remote-api` (`:135`, `:150`). |
| 54 | `src/slic3r/GUI/RemoteAPI/RemoteAPIServer.hpp` | 1 | C | new | new | none |
| 55 | `src/slic3r/GUI/Tab.cpp` | 1 | C | include `RemoteAPIController.hpp` | `:3` | none |
| 56 | `src/slic3r/GUI/Tab.cpp` | 2 | C | `notify_config_changed((int) m_type)` in `Tab::update_dirty` | `:1221` | none |
| 57 | `version.inc` | 1 | **R** | `set(ORCA_MCP_RELEASE "13")` | next to `ORCA_COMPATIBLE_VERSION` (`:27-28`) | `set(ELEGOO_MCP_RELEASE "<N>")` with comment (D4). Must be a bare integer. |

Not ported (plan §2.2): `.github/`, root `README.md`, `docs/research/`, `resources/images/splash_logo*.svg`, `tests/libslic3r/test_placeholder_parser.cpp`, `doc/images/remote-api-preferences.png` (only referenced by the fork's root README).

## 5. Controller audit (`RemoteAPIController.cpp`, 2423 lines)

Read in full (three ranges + header + Server/Config). Every call into slicer internals was compared against orca242 and Elegoo, and every mutating call's **body** was diffed (not only its signature).

- Totals across all audited symbols: **242 identical, 36 compatible, 8 differ, 18 missing**. All "missing" ones are supplied by the patch itself (`backup_defer`, `RemoteAPIChange`, `stop_background_slicing`, `render_*_thumbnail`, `render_toolpaths_with_camera`, `remote_api_server`…) or are the version macros (`SoftFever_VERSION`, `ORCA_MCP_*`) handled in section 4.
- "Differs" items and what they need:
  - `SoftFever_VERSION` → compile error (row 51).
  - `GUI_App::remote_api_server()` → needs rejected hunks 19/24/26.
  - `ELEGOOSLICER_VERSION`, `ORCA_COMPATIBLE_VERSION`, `SLIC3R_VERSION` → use as base; **never append `-mcp` to `ELEGOOSLICER_VERSION`/`SLIC3R_VERSION`** (they feed 3MF metadata, `From_Elegoo` detection, profile OTA checks, user agents, G-code placeholder `version`).
  - `load_bbs_3mf` (`generator` param) → not called by the controller.
  - `Plater::arrange()` / `Plater::orient()` → compile unchanged, but Elegoo's sticky `PREPARE_STATE_OUTSIDE_BED` changes their behavior (section 3).
  - MCP `get_status` → only the C++ line 279 change; MCP 0.1.17 reads no `app`/`app_version`/`capabilities` field, so the planned `/status` change is safe.
- Plan §2.3 header differences: all five confirmed (`load_bbs_3mf generator`, `auto_calc_flushing_volumes_internal(..., const DynamicPrintConfig&)`, `CalcFlushingVolume(..., printer_settings_id = "")`, `LoadMachineOnly`, `Preset::is_elegoo_cc_printer()` — non-const). The list is incomplete but none of the extra diffs affects the controller: `NotificationManager.hpp` new `push_notification(type, level, title, text, ...)` overload (keep API toasts on the 3-argument form to avoid ambiguity), `GCodeProcessor.hpp` (`EProducer::ElegooSlicer`, `process_elegoo_M6211`), `Model.hpp` (`From_Elegoo`), `Config.hpp` (`to_json`), `PartPlate.hpp`, `AppConfig.hpp`, `GUI_App.hpp` (beginner guide, `check_message`), `GLCanvas3D.hpp` (GPU picker), `WipeTowerDialog.hpp`, `FlushVolCalc.hpp`.
- Routes: every route in plan §2.2 exists. Not named by the plan: `POST /plates` (create), `objects/{id}/plate` is POST, `preset/config` is POST; the WebSocket `/api/v1/events` lives in `RemoteAPIServer.cpp:95-104` (auth via `?token=`).
- Elegoo-only keys `support_mms`, `support_wan_network` (coBool, printer) and `bed_texture_area` (coPoints) are **not** covered by the fork's sensitive-key policy → add the first two (plan rule 3). Elegoo-readonly/hidden keys (`nozzle_diameter`, `pellet_modded_printer`, `pellet_flow_coefficient`, `accel_to_decel_factor`) stay writable through the API (D16).

### 5.1 Modal dialogs reachable from the API (plan rule 3)

The fork itself does **not** fully meet "no modal dialog via the API". Known paths on Elegoo:

| Path | Trigger | Origin |
|---|---|---|
| `POST /model` → `BeginnerGuideView::ShowModal` (GUI_App.cpp:4363) | `beginner_guide_shown=false` and current tab ≠ Prepare | **Elegoo-only** |
| `PUT /config` → `ConfigManipulation` MessageDialogs (layer height ≤ 0, spiral mode, max volumetric speed, compensation…) | invalid/edge values | Orca + fork |
| `POST /model` → Prusa-3MF info, `ObjColorDialog`, zero-volume, too small/large, no geometry, `show_error` | file-dependent | Orca + fork |
| `POST /preset/save` → "Temperature Safety Check" (Tab.cpp:7341) | dirty filament preset with big first-layer temperature deltas | Orca + fork |
| `GET /plate/render?view=preview` → `GCodeViewer::init` MessageDialog (GCodeViewer.cpp:1039) | GL init failure / context not current | Orca + fork (GL guard mitigates) |
| arrange/orient job exception → async `ErrorDialog` | geometry/job failure | Orca + fork |
| critical slicing error → async `show_error` (Plater.cpp:10114) | slicing failure | Orca + fork |

Elegoo also **removes** one (`show_substitutions_info` is now gated by `load_config`). Elegoo's new "Load 3mf" 3-button dialog (Plater.cpp:6117-6133) is not reachable from `/model` (LoadModel only) but will be from future project-open/calibration endpoints.

## 6. Phase 1 checklist (derived)

1. Branch `remote-api` from `v1.5.3.5`; apply the patch; resolve the 10 rejected hunks per section 4 (rows 8, 10, 19, 22, 23, 24, 26, 38, 39, 57).
2. Drop row 17 (splash offset); per D18 drop rows 1, 2, 30, 32, 35, 36.
3. `RemoteAPIController.cpp:279` + `/status` new fields; update `openapi.yaml`/`README.md` accordingly.
4. `support_mms`, `support_wan_network` → sensitive (controller + later `guard.py`).
5. Phase 1.2 port-in-use: Windows exclusive bind + notification (D7).
6. `git add -f docs/remote-api`.
7. Commit body cites `MaxEllis/OrcaSlicer@fa3c9ee`; markers `// elegoo-mcp:` on hooks in Elegoo files.
8. Optional per D8: GL guard (row 14), BeginnerGuide guard, arrange/orient prepare-state reset.
9. Build per section 7; smoke + `remote-api-runtime-check.py` (run with `ORCA_BASE=http://127.0.0.1:13130`, its default is 13131) + MCP 0.1.17 smoke on "Elegoo Centauri Carbon 2 0.4 nozzle". Expect `describe_plate` in whole-plate mode (Elegoo sets `gcode_label_objects=0`) unless label objects are turned on for the smoke. A wrong model path makes `load_model` say "needs M4a" (fork returns 404 `not_found` for missing files).
10. Phase 1.5 (M6211): the CANVAS change time **is** in the total time (`print_statistics.modes[0].time`) but attributed to `Tool_change` moves, so **no role gets it**; the flush filament is in totals but in no role. The MCP breakdown does not sum roles, so nothing breaks, but acceptance 8.2 ("breakdown shows the purge") needs new fields (D15).

## 7. Build plan (Windows) — executed 2026-10-13 (see measurements below)

Free space on C: **123 GiB** at the end of Phase 0 (was 166 GiB at the start of the session). This session wrote ~2.3 GiB (workspace 0.7, temp 1.5, uv cache +0.08); the rest of the drop came from something else on the machine. The uv cache (`%LOCALAPPDATA%\uv`) already holds 35.8 GiB of older data (`uv cache clean` would reclaim it if needed). Estimated footprint of one Release build: **~25–55 GB** (deps tree 15–30 GB, slicer build 8–20 GB). Measure after each stage.

Prerequisites: Strawberry Perl x64 (**missing**, D2). Everything else is installed.

```bat
:: 1. x64 Native Tools environment with the v143 toolset (VS 2022 Build Tools)
call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
:: 2. CMake < 4.0 (bundled 3.31.6) and Perl first on PATH; do NOT put Strawberry's c\bin before CMake (CMakeLists.txt:58-80 guard)
set "PATH=C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin;<strawberry>\perl\bin;%PATH%"
cmake --version & perl -v & where cl
:: 3. (D5) pin deps/elegoolink/elegoolink.cmake:40  GIT_TAG 46c7b814e055cf9675d58482d79f43d0bd2280da
:: 4. deps (~30-50 min estimated, worst case ~75)
build_release_windows.bat onlydeps
:: 5. baseline of unmodified v1.5.3.5 (~15-30 min); dlweb downloads the CC2 Device tab web UI (D6)
build_release_windows.bat dlweb slicer
:: 6. after applying the Phase 1 port
build_release_windows.bat slicer
```

- **Measured (2026-10-13):** `tools\build.cmd onlydeps` = **~20 min** (20:00:34 → 20:20:16; first ~10 min on all 32 threads, then 16–24 threads below-normal). All 28 Windows deps built (Qhull is skipped on MSVC by design; GMP/MPFR are prebuilt copies). `elegoolink` resolved to `46c7b81` (unpinned). Deps tree ≈ 11.7 GiB (free 120.8 → 109.1 GiB). Note: msbuild leaves ~90 idle reusable worker nodes behind; they exit on their own.
- **Measured (2026-10-13):** baseline `tools\build.cmd dlweb slicer` on unmodified `v1.5.3.5` = **~18 min** (20:21:53 → 20:39:48, 24 threads below-normal), **0 compile errors**, exit code 0. `build\` = 8.3 GB, install tree `build\ElegooSlicer` = 418 MB (includes `resources\plugins\elegoolink\web\{lan,cloud}_service_web` from dlweb). Free space after: 100.2 GiB.
- **Smoke test:** `build\ElegooSlicer\elegoo-slicer.exe --datadir C:\Users\rosal\Documents\elegooslicer-mcp\tools\test-datadir` → main window "Sem título - ElegooSlicer" after ~27 s, ~607 MB, no error lines in the log. **Always test with `--datadir tools\test-datadir`**: Victor has the official ElegooSlicer profile in `%APPDATA%\ElegooSlicer` and our builds must not touch it.
- Never use `build_release_vs.bat` / `build_release_vs2022.bat` (no `ELEGOO_INTERNAL_TESTING` → C1017) and do not use the script's `vs2026` argument (still uses the VS 2022 generator).
- Outputs: deps in `deps\build\ElegooSlicer_dep\usr\local` (cache `deps\DL_CACHE`), slicer in `build\`, install tree `build\ElegooSlicer`. The script's "clean previous build" step targets a wrong path; delete `build\ElegooSlicer` by hand before release builds.
- Time estimates are extrapolated from the fork's 4-vCPU CI (deps 37–73 min, slicer 43–70 min); this machine has 16C/32T. Record real timings here after the first build.
- Network hosts contacted by a default build: github.com (+ codeload/release assets, raw.githubusercontent.com for OpenCV IPPICV), gitlab.com (Eigen 5.0.1), nuget.org (WebView2 SDK during the wxWidgets configure). The slicer stage downloads nothing. Git-based deps (Sentry 0.14.2, Paho 1.5.3, wx 3.3.2, elegoolink) are pinned by tag/branch only.
- Deps differences vs Orca 2.4.2: adds Sentry, Paho MQTT C/C++, ixwebsocket, elegoolink; OpenSSL 3.1.8 (was 1.1.1w), CURL 8.14.1. Boost 1.84, wxWidgets 3.3.2, nlohmann 3.10.4 unchanged.

## 8. Decisions log

| Date | Decision | By |
|---|---|---|
| 2026-10-13 | Phase 0 run with local clones only; no GitHub fork created (outward-facing action left to Victor, D1). | Claude |
| 2026-10-13 | Hook-map default for titles: change only the non-Apple branches (fork parity). | Claude (pending D12) |
| 2026-10-13 | Victor approved installing everything needed to build and creating the GitHub repo → fork created, Perl portable installed, toolchain = v143 + CMake 3.31.6. | Victor |
| 2026-10-14 | Victor went to sleep and approved every pending decision with Claude's recommendation (pinned in `tools/overnight/CONTRACT.md`: D9, D10, D13, D14, D15, D8 part 2, the 422 `config_rejected` token) and all remaining phases without phase-gate stops. Still never: force-push, release, real-printer tests. | Victor |
| 2026-10-14 | Printer ids are an HMAC-SHA256 of PrinterManager's id with a random per-install key (`remote_api_printer_id_key`): the SDK's ids contain the serial number. Cloud (WAN) printers: no print control at all (`501 unsupported_on_wan`), the SDK can only pause/stop over LAN. | Claude (overnight, under Victor's blanket approval) |
| 2026-10-14 | MCP vendored with `git subtree add --prefix=mcp` (full history: MaxEllis' authorship stays visible; `git subtree pull` takes upstream fixes). Python edits are made in the `orcaslicer-mcp` clone (branch `elegoo`, Claude Desktop's editable install) and pulled into `mcp/`. | Claude |
| 2026-10-13 | **Single GitHub repo: `ShiftSad/ElegooSlicer-MCP`** (fork of `elegooofficial/ElegooSlicer`). No fork of `MaxEllis/orcaslicer-mcp`. Deviation from plan §4: in Phase 6 the MCP server is vendored into this repo under `mcp/` (keep its AGPL `LICENSE` and credits; module name stays `orcaslicer_mcp`). Install with `uvx --from <local clone>\mcp orcaslicer-mcp` or `uvx --from "git+https://github.com/ShiftSad/ElegooSlicer-MCP@<ref>#subdirectory=mcp" orcaslicer-mcp`. Upstream MCP fixes are copied by hand if ever wanted. Phase 1 parity tests use the unmodified MCP 0.1.17 clone. | Victor |

## 9. Pending decisions (for Victor)

Blocking Phase 1:
- **D1** ✅ Done: `ShiftSad/ElegooSlicer-MCP` created (single repo, see decisions log).
- **D2** ✅ Done: Strawberry Perl 5.42.3.1 portable in `tools/`, PATH only inside `tools/build.cmd`.
- **D3** ✅ Done: VS 2022 Build Tools v143 + bundled CMake 3.31.6 (via `tools/build.cmd`).
- **D4** `ELEGOO_MCP_RELEASE` start value. *Recommended: 1 → tags `v1.5.3.5-mcp.1`; N stays global and monotonic across Elegoo base versions (the updater compares only N).*
- **D5** Pin `elegoolink` to `46c7b81` before the first deps build (Phase 7.1 moved earlier; one-line change). *Recommended: yes.*

Phase 1 behavior (recommendation in parentheses):
- **D6** ✅ Done as part of "everything needed to build": `dlweb` ran for the baseline build (Device-tab web UI, release `20260625`). Redistribution question stays under D11.
- **D7** Windows multi-instance: exclusive bind (`SO_EXCLUSIVEADDRUSE`) so the second instance fails and shows "Remote API port in use" — deviation from the fork. Alternative: start the API only in the `MultiInstanceCoordinator` master. (exclusive bind)
- **D8** Modal dialogs (5.1): fork parity has several. (Phase 1: fix the Elegoo-only BeginnerGuide + GL guard + arrange/orient prepare-state reset; the Orca-inherited ones in a separate generic commit that could go upstream.)
- **D9** Telemetry from API actions: document / suppress with hooks / stay logged out. (document + recommend staying logged out; hooks later if wanted)
- **D10** LAN-only: the build talks to Elegoo's cloud when logged in. Accept and document, or patch it out. (accept + document)
- **D12** macOS title suffix and " MCP" in the in-window top bar. (keep fork parity: no)
- **D13** Data dir: share `%APPDATA%\ElegooSlicer` with the official app, or ship the portable zip with its own `data_dir`. (own data_dir for the portable zip)
- **D16** Refuse Elegoo-readonly keys through `PUT /config`. (yes, as a 422 `readonly_key`, since it is a response change it needs approval)
- **D18** Drop the 4 comment-only hunks and the 2 `build_release_vs.bat` hunks. (drop)
- **D19** Docs location: `docs/remote-api` with `git add -f`, or `doc/remote-api`. (`docs/remote-api` + `-f`, keeps fork parity)
- **D20** Rebrand Preferences tooltips and the `Server:` header. (tooltips yes, header no)

Later phases (spec changes found in Phase 0):
Overnight 2026-10-14: D9, D10, D13 (documented: README "Privacy and network"), D14 (as listed below, implemented), D15 (purge fields), D17 (MCP tests fixed on Windows, never write into the real home) are **resolved**. D11 stays open.

- **D11** Public binary releases would redistribute closed Agora RTM DLLs, the unlicensed web bundles and prebuilt GMP/MPFR (rule 7). Decide before Phase 7.
- **D14** Plan §6 vs SDK reality: `/tasks`, `/files`, `raw=1`, exception history are cloud-only (→ 501 `unsupported_on_lan`?); Canvas `temperature_c`, `humidity`, `bed_temp`, `external_spool` have no data source (→ null or remove); `resumePrint` not implemented for CC2 LAN (patch SDK / 501); `DELETE /uploads/{id}` cannot stop a print once the transfer finished (→ `too_late`); default `bed_side` (send dialog's B = btPC, sidebar's B = btPEI); save tray mapping to `filament_mms_mapping.json` like the GUI?; Phase 4 route: full Elegoo chain (~10 files touched) vs calling the SDK singleton from `RemoteAPIElegoo.cpp` (master instance only).
- **D15** Add `filament_change_time_s`, `filament_changes`, `flush_g` to the slice breakdown (response change; needed for acceptance 8.2).
- **D17** MCP Windows test baseline (15 failures): fix in the MCP fork as a generic commit, or run the suite in Linux CI/WSL. The Phase 0 test run left 6 tiny files in `C:\Users\rosal\.orcaslicer-mcp\gcode\` (directory created by the run) — delete?

## 10. Reference for later phases

### 10.1 Device stack (`src/slic3r/Utils/Elegoo/`)
- `PrinterManager::getInstance()` (Singleton). Cache reads (IPC on slave): `getPrinterNetworkInfo(id)` (`:45`, empty `printerId` if not found), `getPrinterList()` (`:46`), `getSelectedPrinter(model, id)` (`:47`; match model+id → model → id → first). Live network: `getPrinterMmsInfo(id)` (`:57`, MQTT 2005, 3 s timeout), `refreshPrinterStatus(id)` (`:66`, fire-and-forget). Cloud-only: `getFileList`, `getPrintTaskList`, `getExceptionList`, `getPrinterStatusRaw`. Result type `PrinterNetworkResult<T>{code, message (localised), optional data}` — key on `code`.
- Template for new control calls (pause/stop): `PrinterManager::getPrinterStatusRaw` (`PrinterManager.cpp:892-922`: slave→IPC, `CHECK_INITIALIZED`, cache lookup, `connectStatus` check, `getPrinterNetwork`, call) → `IPrinterNetwork` (pure virtual) → `ElegooPrinterNetwork` → `ElegooLink` (private + friends) → `elink::ElegooLink::getInstance()`; IPC client/server branches in `IPCClient.cpp`/`IPCServer.cpp:497-675` (default IPC timeout 5 s; control calls need ~30–90 s).
- SDK (`sdk:include/elegoo_link.h`): `pausePrint` `:293`, `resumePrint` `:300`, `stopPrint` `:307` (LAN only, 15 s timeout; lifecycle commands can take longer → treat timeout as "unknown" and confirm via status), `getCanvasStatus` `:346`, `setAutoRefill` `:353`. CC2 LAN methods: 1020 start, 1021 pause, 1022 stop, 1023 resume (commented out), 2005 canvas, 2004 auto refill.
- `PrinterStatus` enum: OFFLINE=-1, IDLE=0, PRINTING=1, PAUSED=2, PAUSING=3, CANCELED=4, SELF_CHECKING=5, AUTO_LEVELING=6, PID_CALIBRATING=7, RESONANCE_TESTING=8, UPDATING=9, FILE_COPYING=10, FILE_TRANSFERRING=11, HOMING=12, PREHEATING=13, FILAMENT_OPERATING=14, EXTRUDER_OPERATING=15, PRINT_COMPLETED=16, RFID_RECOGNIZING=17, VIDEO_COMPOSING=18, EMERGENCY_STOP=19, POWER_LOSS_RECOVERY=20, INITIALIZING=21, BUSY=998, ERROR=999, ID_NOT_MATCH=1000, AUTH_ERROR=1001, UNKNOWN=10000. Derive `offline` from `connectStatus != CONNECTED` (a just-loaded printer has IDLE + DISCONNECTED).
- Whitelist source fields for `Printer` (6.1): `printerId, printerName, printerModel, vendor, host, port, networkType, firmwareVersion, connectStatus, printerStatus, printTask{fileName, progress, currentTime, totalTime, estimatedTime}, printCapabilities{supportsTimeLapse, supportsAutoBedLeveling, supportsHeatedBedSwitching, supportsAutoRefill, supportsFilamentMapping}, systemCapabilities.supportsMultiFilament`. Task name/begin time and exception level/time are not available for LAN.
- CANVAS structs have **no default initialisers**; the wrapper never sets `PrinterMms.temperature/humidity`, `PrinterMmsTray.minBedTemp/maxBedTemp/serialNumber`, `vtTray` → never serialise them. Tray names `A1..A4`, `B1..` by unit order. `TrayStatus`: UNKNOWN=-1, DISCONNECTED(empty)=0, PRELOADED=1, PRELOADED_UNKNOWN_FILAMENT=2, LOADED=3, LOADED_UNKNOWN_FILAMENT=4, ERROR=999. `PrinterMmsManager::getPrinterMmsInfo` also reloads vendor/filament-library JSON on every call and reads `wxGetApp().preset_bundle` from the calling thread → cache (≥5 s) and serialise per printer.
- Never call `PrinterMmsManager::removeFilamentMmsMapping` (self-deadlock).
- Shutdown/recreate: `MainFrame::shutdown` closes IPC and `PrinterManager` **before** `GUI_App::shutdown()` stops the API; `recreate_GUI` closes and re-inits them while the API stays up → Elegoo handlers must tolerate an uninitialised manager.

### 10.2 CC2 facts
- Presets: "Elegoo Centauri Carbon 2 0.4 nozzle" (+0.2/0.6/0.8 inheriting it), `printer_model` "Elegoo Centauri Carbon 2", `host_type` `elegoolink`, `is_elegoo_cc_printer()` true. `bed_texture_area` = 0x-10, 256x-10, 256x256, 0x256.
- Upload format: plain `.gcode` (`use_3mf` unset → false), 144×144 PNG thumbnails embedded.
- Bed sides: sidebar labels btPTE(4) "Textured PEI (Side A)", btPEI(3) "Smooth PEI / High Temp (Side B)"; the send dialog offers btPTE(A) and btPC(1)(B); `perform_job` sends `bedType=1` only for btPC → SDK `print_layout` "B", else "A".
- `extended_info` keys (all strings): `bedType` (numeric BedType), `timeLapse`, `heatedBedLeveling`, `autoRefill`, `hasMms` ("true"/"false"), `selectedPrinterId`, `filamentAmsMapping` (JSON array string). Recent keys: `printsend_upload_and_print`, `printsend_timelapse`, `printsend_heated_bed_leveling`, `printsend_bed_type`, `printsend_auto_refill`, `printsend_selected_printer_id`, `printsend_switch_to_device_tab`, `printhost_path`. Saved tray mappings: `data_dir/user/filament_mms_mapping.json`.
- Transport: MQTT `tcp://<host>:1883`, user `elegoo`, password = access code; HTTP `PUT /upload` on :80 (needs LAN Only); UDP discovery 52700; camera MJPEG `:8080` (no auth, any path; not in the SDK). Concurrent MQTT client slots are limited — the MCP must not open its own MQTT session.
- M6211 (`change_filament_gcode` in the ECC2 profile) is handled by `ElegooGCodeProcessorHelper.cpp` (time + flush volume; gated by `printer_model` starting with "elegoo").

### 10.3 Send path for Phase 4 (no modals)
Do **not** reuse `p->export_gcode(..., upload_job)` / `PrintHostJobQueue`. Instead: copy the plate's tmp G-code (`GCodeProcessorResult::filename`), run post-process scripts only if configured (as `prepare_upload` does), name it via `finalize_output_path`, build `PrinterNetworkParams` like `PrintHost.cpp:434-485`, and call `PrinterUploadManager` (keep task state in the API: `getUploadTask` is read-once; delete the temp file ourselves; slave instances need IPC routing).

### 10.4 CANVAS sync for Phase 3
Proposal (minimal diff, `// elegoo-mcp:` in `PlaterExt.{hpp,cpp}`): split the conversion part of `load_mms_filament_sync_data` (`PlaterExt.cpp:58-96`) into `make_mms_filament_sync_data(const PrinterMmsGroup&)` and the apply part into a UI-free function returning a report; the GUI path keeps its dialogs. Before `select_preset`, refuse (409) or discard dirty filament presets. Note: the GUI sync replaces the whole filament list with only the matched trays and silently drops unmatched/empty trays — the API report must list them.

### 10.5 MCP server
- 53 tools (51 async + `describe_setting`, `search_settings` sync), 3 resources, 3 prompts. Every new tool must be added to `_TOOL_ANNOTATIONS` or the server fails to import.
- Tests: `uv run --extra dev pytest` (1202; Windows baseline 1183/15/4). `lhm.plugin.json`: `uv run python scripts/gen_lhm_tools.py [--check]`.
- Schema: `uv run python scripts/extract_settings_schema.py --src <ElegooSlicer>/src/libslic3r/PrintConfig.cpp` → 858 keys (bundled schema has 807 from an older Orca).
- `.mcpb` launcher (`mcpb/server/index.js:72`) always runs PyPI `uvx orcaslicer-mcp` → must become `uvx --from "git+https://github.com/ShiftSad/ElegooSlicer-MCP@<tag>#subdirectory=mcp" orcaslicer-mcp`; drop PyPI, MCP-registry and LobeHub publishing (`server.json`, `lhm.plugin.json`, `glama.json`, `publish-mcp-registry.yml`) when vendoring into `mcp/`.

## 11. Phase 1 results (2026-10-13)

Decisions D4, D5, D7, D8 (Elegoo-only part), D12 (fork parity), D16, D18, D19, D20 were approved by Victor ("segue com tudo") and are implemented on branch `remote-api` (base `v1.5.3.5`):

| Commit | What |
|---|---|
| `435c400` | Upstream cherry-pick: ADMesh `stl_read` stack-overflow fix (Elegoo `main` 2d507e3, after the v1.5.3.5 tag). |
| `1f56ec7` | deps: elegoo-link pinned to `46c7b81` (D5). |
| `6271b8a` | **Port** of MaxEllis/OrcaSlicer@fa3c9ee + the 10 rejected hunks resolved + `/status` (`ELEGOOSLICER_VERSION`, `orca_compatible_version`, `mcp_build`). Note: its message says "47 of the 57 hunks"; with the quoted pathspec (no `build_release_vs.bat`) the patch has 55 hunks (45 clean + 10 rejected); splash offset and 4 comment-only hunks were then dropped. |
| `10b1d63` | Windows `SO_EXCLUSIVEADDRUSE` + "port unavailable" warning (D7, plan 1.2). Generic. |
| `5b8a25e` | `support_mms`, `support_wan_network` sensitive (rule 3). Elegoo-specific. |
| `171785a` | Read-only keys → 422 `readonly_key` (D16). Generic. |
| `9a282a6` | No Beginner Guide modal during API tasks (D8). Elegoo-specific. |
| `a2252c4` | GL context guard in the offscreen renderers (D8). Generic. |
| `246ff1d` | Reset Elegoo's sticky `PREPARE_STATE_OUTSIDE_BED` before API arrange/orient (D8). Elegoo-specific. |
| `00a9a1b` | Preferences tooltips say ElegooSlicer (D20). |
| `2a9121dbe`, `f696e16e8`, `76a6f6cbd`, `738b5a74a` (review fixes) | Found by the adversarial review: acceptor/io_context destruction order in `Server::start` (2nd instance crashed) and `Server::stop` (WS clients), updater `SetInt(by_user)` → crash via Elegoo's missing "Skip" button, 404 feedback on manual check, dedicated `RemoteAPIPortUnavailable` notification, README bind/token notes. |

Marker convention (decided): `// elegoo-mcp:` marks Elegoo-specific code we wrote; hooks copied verbatim from the fork keep the fork's own comments (easier re-sync with MaxEllis). All touch points outside the new files: `git diff v1.5.3.5..remote-api -- . ':!src/slic3r/GUI/RemoteAPI' ':!docs'`.

Tests (build `tools\build.cmd slicer`, ~17 min, 0 errors; app run with `--datadir tools\test-datadir` prepared by `tools\phase1_setup.py`):
- `/status`: `app` ElegooSlicer, `app_version` 1.5.3.5, `orca_compatible_version` 2.4.2, `mcp_build` 1; no token → 401; listens on 127.0.0.1:13130 only; window title "… - ElegooSlicer MCP".
- `orcaslicer-mcp/scripts/remote-api-runtime-check.py` (ORCA_BASE=…:13130): **16 checks, 0 failed**.
- MCP 0.1.17 smoke over MCP stdio (`tools\phase1_smoke.py`, model `resources/handy_models/ElegooToleranceTest.stl`, printer "Elegoo Centauri Carbon 2 0.4 nozzle"): **12/12 without error** — get_status, list_plates, select_preset, load_model, render_plate editor + preview (PNG ok), set_config, slice_and_wait (27m49s estimate), get_slice_breakdown, describe_plate (per-object mode works here), list_plates. Results in `tools\logs\phase1\`.
- M6211: present in the G-code (`M6211 A1 L200 …` from `machine_start_gcode`, single filament); breakdown/describe_plate unaffected. Multi-colour purge time is in the total but not in any role (see D15) — not re-tested with several filaments yet.
- Updater: queries only `api.github.com/repos/ShiftSad/ElegooSlicer-MCP/releases/latest` (404 today: no release), no Elegoo update server contacted.
- Second instance: crashed before the review fix (acceptor/io_context order). After the fix (rebuild 21:42→21:57, 15 min, 0 errors): the 2nd instance stays up, logs `failed to bind port 13130 … 10048` + `port 13130 in use, API is off in this instance`, and the 1st keeps serving the API. MCP smoke re-run: 12/12. (The "Projeto não salvo anterior detectado" prompt seen in that test is Elegoo's normal crash-recovery dialog after the force-killed test runs, not API-related.)
- Not runtime-tested: `Server::stop()` with a connected `/api/v1/events` client (fixed by code review; needs a WS client test).
- D17: the 6 test G-code files in `%USERPROFILE%\.orcaslicer-mcp` were deleted (directory had been created by the test run).

Still open after Phase 1:
- Orca-inherited modal dialogs reachable via the API (section 5.1: ConfigManipulation on PUT /config with edge values, model-load info boxes, Temperature Safety Check, async error dialogs). Planned as a separate generic commit (D8, second half).
- D9/D10 (telemetry, not LAN-only) to be documented in the README in Phase 6; D11, D13–D15, D17 as listed in section 9.

### 11.1 Feedback from Victor's first Claude Desktop session (2026-10-13 22:40)
- `load_model` → "not available on this OrcaSlicer build (needs M4a)": NOT a missing route. `POST /model` answers `404 {"error":"not_found"}` when the file path does not exist (`boost::filesystem::exists`), and MCP 0.1.17 `errors.py` treats a 404 `not_found` as "route missing". Verified working with a real path (smoke 12/12). Proposed fix (response-format change → needs Victor's OK, rule 9): return `{"error":"file_not_found","path":...}` so the MCP reports the real cause. Bundled models are mostly `.drc`; the `.stl` ones are `ElegooToleranceTest.stl` and `elegoo_cube.stl`.
- `check_profile_physics` blocks the stock CC2 profile (generic PLA melt estimate ~12.5 mm³/s vs 18.9 asked; the Elegoo preset caps at 21). MCP-side (Python), phase 6: use the preset's `filament_max_volumetric_speed` or downgrade to a warning.
- No Elegoo printer/CANVAS capabilities yet: expected (phases 2–6).
- Second Claude Desktop session (helmet test, 2026-10-13 ~23:20) — triage:
  - C++ (fork bugs carried over, generic): `POST /objects/{id}/transform` adds Euler angles (`set_rotation(get_rotation() + rotate)`), so after Y -90 the decomposed angles hit gimbal lock and +90 gave 180°; fix = compose rotation matrices like the GUI rotate gizmo. It also never drops the object back onto the bed after a rotation (one part ended 4 mm below the bed); fix = `ensure_on_bed()` as the GUI's do_rotate does.
  - MCP Python (phase 6): `check_placement` ignores other plates and per-object brim; `save_gcode` names the file after the first object of the project, not the saved plate; `slice_and_wait` can exceed the ~60 s Claude Desktop tool-call limit on big plates (workaround: `slice_and_wait(timeout=50)` or `slice` + `get_slice_status`; fix = return before the bridge limit).
  - Deleted at Victor's request: `%USERPROFILE%\.orcaslicer-mcp\gcode\capacete_placa6_viseira.gcode` (107 MB).
- Fixed ("arruma tudo ai"), C++ on `remote-api`: `9f1c0fcee` (`POST /model` missing file → `404 {"error":"file_not_found","path"}`; Victor approved the format change with "arruma tudo"), `c16b7ad15` (transform composes world-axis rotations), `d8485e8f7` + `9a457d844` (drop onto the bed after any transform, as the GUI's `do_move/do_rotate/do_scale`). MCP Python on `elegoo`: `253cde4`, `bfbab3e`, `f60f654`, `930fa45`, `0a56e86`, `9be76c2`, `d3f5f2b`, `f985825`.

### 11.2 Pulled-forward features (2026-10-14, "implementa tudo isso")
Victor asked for: the stale-slice bug (other plates kept `slice_result_valid` after a config change), orient one object, view switch, part volumes, project save, region painting; plus `PORT_PLAN.md` updates (Phase 3 gets orient-one/add-volume, painting is its own Phase 3B; done).

C++ (`remote-api`, new file `src/slic3r/GUI/RemoteAPI/RemoteAPIModelOps.cpp`, dispatch hooks in `RemoteAPIController.cpp`):

| Commit | What |
|---|---|
| `042fca99f` | Global config/preset writes mark **every** plate unsliced when `full_config` changed; per-object writes mark the object's plates. |
| `53f85f5e9`, `4824bb69a`, `5f83e8943` | `POST /orient {"object_id"}` (select + window's OrientJob); `409 object_not_printable` (OrientJob would otherwise orient every *other* object), `409 select_failed`; plates marked unsliced when the job starts. No body clears the window selection first. |
| `fd62d6d95` | `POST /view {"view":"prepare|preview"}`, `/status.view`; `prepare` → `409 preview_only` while a G-code file is open. |
| `03ff16011` | `GET/POST /objects/{id}/volumes`, `PUT …/volumes/{vid}/config`, `DELETE …/volumes/{vid}` (`409 last_part`, `409 cut_object`). Shared `stage_object_config` key policy. |
| `d2d3af124`, `fc0dce6bd` | `POST /project/save {path?, overwrite?}` with Plater's save steps minus dialogs (120 s UI timeout); `409 preview_only` with a G-code / `.gcode.3mf` open, `409 gizmo_editing`. |
| `b188e02df`, `5d760d6ed`, `27b22d309` | `POST /objects/{id}/paint` (support/seam/color via `TriangleSelector`, whole triangles by centroid on the first instance; `max_overhang_deg`: `n.z < 0 && -n.z >= sin(a)`, 0..90; `clear` + box → `422 clear_takes_no_box`). |
| `d9384916f` | capabilities `orient_object, view, volumes, project_save, paint`; README + openapi. |

MCP (`orcaslicer-mcp`, branch `elegoo`): `ecd1b94` `auto_orient(object_id)` (checks `orient_object` capability first: older builds ignore the id and orient everything), `be4d47b` `set_view`, `09a3d1e` `list_parts/add_part/set_part_config/set_part_filament/remove_part`, `e917b64` + `af1c879` `save_project` (50 s, `save_still_running`), `2e755bd` + `4251bf4` + `f5c812f` `paint_region`, `8958422`, `593320b`, `bef4641` (README). pytest: 15 failed (same baseline), 1247 passed.

Review: one implement → review → fix round per side; all findings fixed (5 C++, 6 Python). C++ checked with `cl /Zs` only.

Tests (build `tools\build.cmd slicer` with `BUILD_AFFINITY=0xFFFFFFFF`, 02:14→02:29, 0 errors; app run with `--datadir tools\test-datadir`; `/status` lists the five new capabilities):
- `tools\phase1_rotation_check.py` (ElegooToleranceTest.stl): **8/8**: missing file → `404 file_not_found` + path; Y −90 swaps the X/Z extents and Y +90 returns to rotation `[0,0,0]`; min z = 0 after each rotate; a 5 mm upward translate drops back onto the bed.
- `tools\phase1b_features_check.py` (two `elegoo_cube.stl` on two plates): **39/39**: both plates sliced → global `PUT /config` → `[False, False]`; re-slice both → `PUT /objects/{A}/config` → `[False, True]`; view switch + `/status.view`; orient of A alone lays A flat and leaves B tilted; volumes add (modifier box at the exact size/centre, support blocker cylinder), config (200, and 422 with nothing applied), `409 last_part`, delete, `404 unknown_volume`; paint support/seam/color, `bad_slot`, `bad_overhang`, `clear_takes_no_box`, clear; project save (`not_3mf`, `bad_path`, new path, `409 file_exists`, overwrite, no body = current file). The saved 3MF holds `paint_seam` (3834 triangles), `paint_color` (26713), no `paint_supports` (cleared), the `support_blocker` volume and the object's `wall_loops`.
- `tools\phase1b_mcp_smoke.py` over MCP stdio: **all 22 calls as expected**, incl. the local `blocked_by_local_policy` refusal for `post_process` on a part and `clear_takes_no_box`.
- Phase 1 smoke (`tools\phase1_smoke.py`) re-run on the new build and the updated MCP: **12/12**.
- Note: the startup "Projeto não salvo anterior detectado" prompt (crash-recovery after force-killed test runs) stays open while the API works behind it: API calls are not blocked by it.

Open:
- The 422 replies of the config writes have no `"error"` token (`{"applied":[],"errors":{…}}`); adding one changes an existing response format → ask Victor (rule 9).
- The MCP keeps only `error`/`errors` from slicer error bodies, so extra fields (e.g. `path` on `file_exists`) do not reach the model.
- Paint is triangle-granular (big triangles crossing the box edge are all-or-nothing); colour has no in-box erase, only `clear`.

## 12. Overnight phases 2–6 (2026-10-14)

Done by a 15-agent workflow (implement → review → fix per line, `cl /Zs` only), then merged, built and tested by the lead. Contract: `tools/overnight/CONTRACT.md`.

Branches and commits (all on `remote-api` after `ca9cc885e`):
- Elegoo C++ (`RemoteAPIElegoo.cpp`, ~2470 lines): printers + status + tasks/files (`da6050db8`), CANVAS read (`578dea2a4`), sync (`63349e016`, PlaterExt split into shared functions), mapping (`74d8cd3cb`, `PrintSendDialogEx::getPlateFilamentList` extracted), print-control preference (`9adace3bd`), prepare/print/uploads (`82d9a64d9`), pause/resume/stop (`ab3f7244c`), review fixes `85ee3e8dd`..`468db16a9` (no print control on WAN, Print-dialog type_mismatch rule, upload/cancel race, stop only in print states, PrinterUploadManager lock hook, command_in_progress, HMAC ids, raw-status secret scrub, 25 s request budget).
- Generic C++ (branch `qol`, merged in `d425adb8a`; `RemoteAPIProject.cpp`, `RemoteAPICalibration.cpp`, `RemoteAPIDialogs.{hpp,cpp}`): `config_rejected` token, `/filaments`, split, undo/redo, layer changes, purge breakdown, calibration, D8 modal guards (GUI.cpp, ConfigManipulation.cpp, Tab.cpp, Plater.cpp, GCodeViewer.cpp hooks), review fixes.
- Lead fixes after runtime tests: `951f3c689` (API move/rotate/scale, delete and duplicate now take the window's undo snapshot: before, `POST /undo` after an API move removed the object loaded before it), `26ddf519e` (pause/resume/stop notifications in words).
- MCP: `mcp/` (subtree, `057239318` + pulls). New tools: printers, CANVAS, sync, mapping, prepare/start (MCP elicitation or `user_confirmed`), uploads, pause/resume/stop, camera snapshot, filament slots, split, undo/redo, layer changes, calibration. Lead fix `37e6adf` (stop asks only in the states the slicer stops in; hints for `command_in_progress`, `unsupported_on_wan`).
- Docs cherry-picked from `ci`: `UPSTREAM_SYNC.md`, README notice, "Privacy and network" (+ the SDK log note below), release-notes draft. The workflow file itself stays on `ci` (not pushed).

New hooks in Elegoo files (all `// elegoo-mcp:`): `PlaterExt.{hpp,cpp}` (sync split), `Elegoo/PrintSendDialogEx.{hpp,cpp}` (filament-list helper), `Utils/Elegoo/PrinterMmsManager.{hpp,cpp}` (optional nozzle list, so the worker reads no GUI data), `Utils/Elegoo/PrinterUploadManager.cpp` (task read/reap under its lock), `Preferences.cpp` (checkbox), `GUI.cpp`, `ConfigManipulation.cpp`, `Tab.cpp`, `Plater.cpp`, `GCodeViewer.cpp` (D8). List: `git diff ca9cc885e..remote-api -- . ':!src/slic3r/GUI/RemoteAPI' ':!docs' ':!mcp'`.

Build: `tools\build.cmd slicer` 05:10→05:25 (14.5 min, 0 errors). New `tools\build_incremental.cmd` (no CMake re-configure: `cmake --build` + install) rebuilds after a few .cpp edits in ~50 s; `build_release_windows.bat` re-configures every time, which recompiles everything.

Fake printer: `tools\fakecc2` (stdlib-only CC2: MQTT broker, HTTP upload, discovery, MJPEG camera, control port 18080), checked against the real elegoo-link SDK (37/37) and Paho (11/11). Register it with `tools\fakecc2\add_fake_printer.py tools\test-datadir` (app closed); toggle print control with `tools\set_print_control.py tools\test-datadir on|off`.

Runtime results (test datadir, fake CC2 at 127.0.0.1):
- `tools\phase4_fakecc2_e2e.py off`: **36/36**: printers/status/exceptions/501 on LAN/404, CANVAS (4 trays, matched presets, nulls), sync, mapping, every print route `403 print_control_disabled` with a warning notification, the fake got no print command, no secret in 28 printer replies.
- `tools\phase4_fakecc2_e2e.py on`: **61/61** (twice): unknown_field, bad_mapping, printer_busy, plan expiry on a setting change (410), prepare summary, 202 upload → success, **the printer got the slicer's G-code byte for byte** (sha256 = `GET /gcode`), one 1020 start with the plan's options and slot map, plan used once, `too_late`, pause, `resume_unsupported`, `confirm_required`, stop, `not_printing`, a stop answered after 17 s → `504 printer_timeout` at 15 s and the printer stopped, offline → 409 and back, no secret in 66 replies.
- `tools\phase4_mcp_e2e.py` (MCP stdio → app → fake): **24/24**: list/canvas/sync/slots/slice/mapping/status, prepare, start refused without confirmation, start with `user_confirmed`, upload success, `wait_for_printer(printing)`, camera JPEG, pause, resume_unsupported, stop refused/confirmed; a declined MCP prompt beats `user_confirmed=True`; an accepted prompt starts and stops.
- GUI parity: the window's Upload and its Print (with CANVAS mapping) sent the **same bytes** (sha256 `d42298…`) and the **same 1020 request** (slot map, `print_layout` A, leveling off) as the API; the window's Sync button and `POST .../canvas/sync` from the same state left **identical** `filament_colour`, `filament_settings_id`, `filament_type` and `flush_volumes_matrix`. Phase 4.3 and the Phase 3 acceptance are met (on the fake).
- `tools\phase2_project_check.py`: **42/42**: config_rejected, filaments (colour, unknown slot/preset, slot_in_use + force), undo/redo of move, duplicate and delete, layer changes (bad_z/slot/type, delete, unknown plate), a tool change at 10 mm writes `M6211 T1 …` and purge reports 1 change, split to parts, calibration (409 unsaved_project, discard, bad_type).
- Regressions: rotation 8/8, phase1b features 39/39, phase1 MCP smoke 12/12, phase1b MCP smoke 22/22, `remote-api-runtime-check.py` 39 checks / 0 failed / 1 skipped.
- MCP pytest: 1438 passed, 4 skipped; the real `%USERPROFILE%\.orcaslicer-mcp\gcode` stays empty.

Found while testing:
- The elegoo-link SDK writes the access code in clear text to `<datadir>/log/elegoolink.log` (`lan_service.cpp:347`); ElegooSlicer's own logs hold neither the code nor the serial. Documented in README "Privacy and network"; not patched (SDK code).
- `409 secondary_instance` is not runtime-tested: the master lock is per data dir and a second window on the same data dir cannot bind the API port, so it is reachable only when the first window started with the API off.

- `tools\phase5_dialogs_check.py` (D8 at runtime, the app's visible windows listed after every step): **33/33**: a 1.5 mm cube ("too small, inches?") and a 3000 mm cube ("too large, scale down?") load with the question answered No and listed in `skipped_dialogs`; an unreadable STL answers `422 load_failed` with its error box text in `skipped_dialogs`; `spiral_mode` + 3 walls is refused whole (`422 config_rejected`, `errors._question`, nothing changed); all six calibrations (pressure advance, temperature, retraction, max volumetric speed, VFA, flow rate) build and slice; `bad_params`; no extra window at any point.

QA round (2026-10-14 ~06:00, 5 reviewers + 5 adversarial verifiers on the merged branch, read-only): 11 confirmed findings, none blocker. Fixed:
- `8f15f2284`: after `DELETE /uploads/{id}` the status is `cancelling` (not `cancelled`) until the job knows whether the print started; `GET /uploads` has `done`; a transfer lost in a window rebuild is `unknown` (the print may have started); cancelling a transfer that failed before the whole file went out is `409 already_ended`; `plate`, mapping `slot` and `unit` read as int64 (no wrap-around); the CANVAS sync refuses when the Filament tab is gone.
- `b3063bf02`: `UiTask::run` answers `503 gui_rebuilding` (printer routes: `printers_unavailable`) while the window is rebuilt (wx can run queued API tasks from the rebuild's progress dialog); an API auto-slice that never starts drops its D8 mark, and the window's Slice buttons clear it.
- MCP `db53d29`: `get_upload_status`/`cancel_upload` never say "nothing is printing" while a cancel is pending; `already_ended`; `upload_in_progress` after a cancel says to wait; `split_object` waits 50 s and answers `split_unconfirmed`; `preview_filament_mapping` says which issues block `prepare_print`.
After the fixes (`tools\build_incremental.cmd`, `tools\run_all_checks.sh`): e2e on 61/61, MCP e2e 24/24, project 42/42, dialogs 33/33, rotation 8/8, features 39/39, smoke 12/12, MCP 1B 22/22, runtime-check 37/0 failed (2 skipped: the 403 checks, print control on); MCP pytest 1442 passed, 4 skipped.

Branch `ci` was rebuilt as `remote-api` + the workflow commits (+ `mcp/**` and `tools/remote-api/**` in `paths-ignore`); merged into `remote-api` with `--no-ff` on 2026-10-14, bringing only `.github/workflows/build_elegoo_mcp.yml`.

Afternoon 2026-10-14 (Victor: "commita e push"): the test tools moved into `tools/remote-api/` (`58b1c9e63`); `ci` pushed, Actions enabled by Victor.
- CI run 38578762396 failed at the very last link (`LNK1181 agora_rtm_sdk.dll.lib`): elegoolink's installed `elegoolink_targets.cmake` links that import library by absolute path inside `deps/build/dep_elegoolink-prefix/src/...`, which the workflow's cleanup step had deleted. Fix `e40c8e909` on `ci`: cache that folder with the prefix, no cleanup step (218 GB free anyway), check for the lib before the slicer build, cache key = `deps/` + `cmake/modules/` + `DEPS_CACHE_VERSION` (CI-only edits keep the cache).
- CI run 38592205059: **success** in 2 h 14 min (cold dependency build). The zip's file list equals the local install tree plus `data_dir/README.txt`; `sha256sum -c SHA256SUMS` OK. The zipped `elegoo-slicer.exe`, started with `--datadir` on the test data dir, passed the whole battery (same numbers as above). Its portable `data_dir` mode (no `--datadir`) was only checked in the source (`GUI_App.cpp`), not started, so as not to risk Victor's `%APPDATA%` profile.
- Extracted under a ~150-character folder, the zip's first start fails with `CriticalException: Copying directory ... failed` (a profile file passes Windows' 260-character limit; the longest path inside the zip is 130 characters). The release notes' install step says to extract to a short path.
- Battery from `tools/remote-api/` on the local build: all pass, e2e off 36/36. `phase1_setup.py` now drops `app.last_backup_path`; before that, the "restore the unsaved project?" prompt left by a killed test app failed 16 steps of the dialogs check (`c68af243f`).

Still open: real-printer checks after 17/10 (model string, camera URL, tray numbering, type names such as "PLA+", Phase 4.4 cube print with Victor), `.mcpb` packing, D11 before any release. Actions caches are per branch: the first `remote-api` run builds the dependencies again (about 2 h 15 min); later runs reuse them (about 70 min).
