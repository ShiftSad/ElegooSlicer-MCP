# Remote API test tools

Build wrappers, runtime checks and a fake Centauri Carbon 2 used to develop and verify the
Remote API and the MCP server (see `PORT_NOTES.md` section 12 for the results). Nothing here
is part of the slicer build, and nothing here talks to a real printer.

## Layout

The defaults assume this checkout sits in a workspace folder next to a `tools\` folder that
holds machine-local things (never committed):

```
<workspace>\
  ElegooSlicer\              this repository
  tools\strawberry-perl-5.42.3.1\   portable Perl for the OpenSSL dependency (build.cmd)
  tools\test-datadir\        the TEST data dir the app runs with (never %APPDATA%\ElegooSlicer)
  orcaslicer-mcp\.venv\      optional: a Python with the MCP server installed
```

Each default can be overridden: `PERL_BIN` and `VS_BUILDTOOLS` for the build wrappers,
`DATADIR` and `MCP_PY` for `run_all_checks.sh`. A Python for the MCP checks can also be made
inside this repository with `uv sync --extra dev` in `mcp\` (it creates `mcp\.venv`).

## Files

| File | What it does |
|---|---|
| `build.cmd` | sets up VS 2022 Build Tools, its CMake 3.31 and the portable Perl, then runs `build_release_windows.bat` with the given arguments (`onlydeps`, `dlweb slicer`, ...) at below-normal priority on 24 threads (`BUILD_PRIORITY`, `BUILD_AFFINITY`) |
| `build_incremental.cmd` | rebuilds and installs `build\ElegooSlicer` without CMake's configure step (about a minute after a few `.cpp` edits, instead of ~15) |
| `phase1_setup.py <datadir> [port]` | prepares a TEST data dir: CC2 printer model, Remote API on 127.0.0.1 with a fresh token in `<datadir>\api_token.txt` |
| `set_print_control.py <datadir> on\|off` | toggles "Allow sending and controlling prints" in that data dir (app closed) |
| `fakecc2\` | the fake CC2 (MQTT, HTTP upload, discovery, camera); see its README |
| `phase1_rotation_check.py`, `phase1b_features_check.py`, `phase2_project_check.py`, `phase5_dialogs_check.py` | runtime checks of the generic routes |
| `phase4_fakecc2_e2e.py on\|off` | printer, CANVAS and print routes against the fake |
| `phase1_smoke.py`, `phase1b_mcp_smoke.py`, `phase4_mcp_e2e.py` | the same through the MCP server over stdio (run with `MCP_PY`) |
| `run_all_checks.sh <app pid> <log dir>` | all of the above plus `mcp\scripts\remote-api-runtime-check.py` |
| `winshot.ps1`, `winclick.ps1` | capture or click one window of a process (GUI checks without full-screen screenshots) |
| `CONTRACT.md` | the route and tool contract the phase 2-6 work was built against |

## Running every check

From the repository root, with the app built:

```
py -3 -I tools\remote-api\phase1_setup.py ..\tools\test-datadir
py -3 -I tools\remote-api\fakecc2\add_fake_printer.py ..\tools\test-datadir
py -3 -I tools\remote-api\set_print_control.py ..\tools\test-datadir on
py -3 -I tools\remote-api\fakecc2\fakecc2.py                (in its own terminal)
build\ElegooSlicer\elegoo-slicer.exe --datadir ..\tools\test-datadir
bash tools/remote-api/run_all_checks.sh <app pid> <log dir>
py -3 -I tools\remote-api\set_print_control.py ..\tools\test-datadir off   (app closed)
```

Every check replaces the project in that window. `phase4_fakecc2_e2e.py off` is the one to run
with print control off.
