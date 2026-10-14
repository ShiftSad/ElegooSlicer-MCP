#!/usr/bin/env bash
# Prepare an ElegooSlicer MCP server release: bump the version markers, pin the server source to
# the release's git tag, run the tests and pack the Claude Desktop extension (.mcpb).
#
#   scripts/cut-release.sh 0.1.18 v1.5.3.5-mcp.2
#
# The tag is the one the ElegooSlicer-MCP release will get (v<ElegooSlicer version>-mcp.<N>). The
# launcher (mcpb/server/index.js), server.json and the README run the server from git at that
# tag, never from a branch: a later push must not change what an installed extension runs.
#
# Nothing is published. This fork is not on PyPI, the MCP registry or LobeHub (the PyPI package
# orcaslicer-mcp is the upstream OrcaSlicer server): users run it from git, as the .mcpb launcher
# does (mcpb/server/index.js). Committing, tagging and attaching the .mcpb to a GitHub release of
# ShiftSad/ElegooSlicer-MCP stay manual steps, done after a review.
set -euo pipefail
VER="${1:?version, e.g. 0.1.18}"
TAG="${2:?release tag, e.g. v1.5.3.5-mcp.2}"
if [[ ! "$TAG" =~ ^v[0-9]+(\.[0-9]+)+-mcp\.[0-9]+$ ]]; then
  echo "FAIL: the tag must look like v1.5.3.5-mcp.2, got '$TAG'" >&2; exit 1
fi
cd "$(dirname "$0")/.."

echo "== 1/4 bumping the version markers to $VER =="
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

echo "== 2/4 pinning the server source to $TAG =="
PINNED=(mcpb/server/index.js server.json README.md)
sed -i "s|ElegooSlicer-MCP@[^#\"]*#subdirectory=mcp|ElegooSlicer-MCP@$TAG#subdirectory=mcp|g" "${PINNED[@]}"
mapfile -t refs < <(grep -ho 'ElegooSlicer-MCP@[^#"]*#subdirectory=mcp' "${PINNED[@]}" | sort -u)
if [ "${#refs[@]}" -ne 1 ] || [ "${refs[0]}" != "ElegooSlicer-MCP@$TAG#subdirectory=mcp" ] ||
   ! grep -q "ElegooSlicer-MCP@$TAG#subdirectory=mcp" mcpb/server/index.js; then
  echo "FAIL: the server source is not pinned to $TAG everywhere: ${refs[*]}" >&2; exit 1
fi
echo "   launcher, server.json and README run @$TAG"

echo "== 3/4 tests =="
uv run --extra dev pytest -q
uv run python scripts/gen_lhm_tools.py --check

echo "== 4/4 pack the Claude Desktop extension =="
npx --yes @anthropic-ai/mcpb pack mcpb
mv mcpb.mcpb "elegooslicer-mcp-$VER.mcpb"
sha256sum "elegooslicer-mcp-$VER.mcpb" > "elegooslicer-mcp-$VER.mcpb.sha256"

echo "DONE: elegooslicer-mcp-$VER.mcpb (+ .sha256). Review, commit, and tag that commit $TAG by hand."
