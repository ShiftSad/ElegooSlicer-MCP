#!/usr/bin/env bash
# Prepare an ElegooSlicer MCP server release: bump the version markers, run the tests and pack
# the Claude Desktop extension (.mcpb).
#
#   scripts/cut-release.sh 0.1.18
#
# Nothing is published. This fork is not on PyPI, the MCP registry or LobeHub (the PyPI package
# orcaslicer-mcp is the upstream OrcaSlicer server): users run it from git, as the .mcpb launcher
# does (mcpb/server/index.js). Committing, tagging and attaching the .mcpb to a GitHub release of
# ShiftSad/ElegooSlicer-MCP stay manual steps, done after a review.
set -euo pipefail
VER="${1:?version, e.g. 0.1.18}"
cd "$(dirname "$0")/.."

echo "== 1/3 bumping the version markers to $VER =="
sed -i "s/^version = \".*\"/version = \"$VER\"/" pyproject.toml
sed -i "s/\"version\": \"[^\"]*\"/\"version\": \"$VER\"/g" server.json mcpb/manifest.json lhm.plugin.json

# All must agree or the extension ships mismatched metadata. Anchored so fields like
# manifest_version don't leak in.
mapfile -t found < <({ grep -ho '^version = "[^"]*"' pyproject.toml; \
                       grep -ho '^ *"version": "[^"]*"' server.json mcpb/manifest.json lhm.plugin.json; } \
                     | grep -o '[0-9][^"]*' | sort -u)
if [ "${#found[@]}" -ne 1 ] || [ "${found[0]}" != "$VER" ]; then
  echo "FAIL: version markers disagree: ${found[*]}" >&2; exit 1
fi
echo "   all markers at $VER"

echo "== 2/3 tests =="
uv run --extra dev pytest -q
uv run python scripts/gen_lhm_tools.py --check

echo "== 3/3 pack the Claude Desktop extension =="
npx --yes @anthropic-ai/mcpb pack mcpb
mv mcpb.mcpb "elegooslicer-mcp-$VER.mcpb"
sha256sum "elegooslicer-mcp-$VER.mcpb" > "elegooslicer-mcp-$VER.mcpb.sha256"

echo "DONE: elegooslicer-mcp-$VER.mcpb (+ .sha256). Review, commit and tag by hand."
