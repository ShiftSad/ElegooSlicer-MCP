#!/usr/bin/env bash
# Runs every runtime check against the test app (TEST --datadir, port 13130) and the fake CC2
# (tools/remote-api/fakecc2/fakecc2.py running). Print control must be ON in the test datadir
# (tools/remote-api/set_print_control.py <test-datadir> on) before the app starts.
# Usage: bash tools/remote-api/run_all_checks.sh <app pid> <log dir>
# Environment (defaults follow the workspace layout in README.md):
#   DATADIR  the test data dir                    (../tools/test-datadir next to this checkout)
#   MCP_PY   a Python with the MCP server installed (mcp/.venv, else ../orcaslicer-mcp/.venv)
OUT=$(mkdir -p "$2" && cd "$2" && pwd)
PID=$1
cd "$(dirname "$0")/../.." || exit 1
DATADIR=${DATADIR:-../tools/test-datadir}
if [ -z "$MCP_PY" ]; then
    MCP_PY=mcp/.venv/Scripts/python.exe
    [ -x "$MCP_PY" ] || MCP_PY=../orcaslicer-mcp/.venv/Scripts/python.exe
fi
T=$(cat "$DATADIR/api_token.txt")
M="$PWD/resources/handy_models"
D=tools/remote-api

# Wait for the test app. -f: only a 200 counts, so an app started with another data dir (whose
# token is different) is never mistaken for the test app.
up=
for i in $(seq 1 60); do curl -sf -m 2 -H "X-Api-Token: $T" http://127.0.0.1:13130/api/v1/status >/dev/null && up=1 && break; sleep 1; done
[ -n "$up" ] || { echo "no test app accepting the $DATADIR token on 127.0.0.1:13130"; exit 1; }

run() { # name, command...
    local name=$1; shift
    "$@" > "$OUT/$name.log" 2>&1
    echo "$name: exit $? | $(grep -c '^PASS\|^ok ' "$OUT/$name.log") ok | $(grep -c '^FAIL' "$OUT/$name.log") fail | $(tail -1 "$OUT/$name.log" | cut -c1-120)"
}

run e2e_on      py -3 -I $D/phase4_fakecc2_e2e.py on "$T" "$M/elegoo_cube.stl"
run mcp_e2e     "$MCP_PY" $D/phase4_mcp_e2e.py "$T" "$M/elegoo_cube.stl"
run project     py -3 -I $D/phase2_project_check.py "$T" "$M/elegoo_cube.stl"
run dialogs     py -3 -I $D/phase5_dialogs_check.py "$T" "$PID" "$OUT/dialogs"
run rotation    py -3 -I $D/phase1_rotation_check.py "$T" "$M/ElegooToleranceTest.stl"
run features    py -3 -I $D/phase1b_features_check.py "$T" "$M/elegoo_cube.stl" "$OUT"
run smoke       "$MCP_PY" $D/phase1_smoke.py "$T" "$M/ElegooToleranceTest.stl" "$OUT/smoke"
run mcp1b       "$MCP_PY" $D/phase1b_mcp_smoke.py "$T" "$M/elegoo_cube.stl" "$OUT"
ORCA_BASE=http://127.0.0.1:13130 ORCA_TOKEN=$T run runtime "$MCP_PY" mcp/scripts/remote-api-runtime-check.py
echo "all done"
