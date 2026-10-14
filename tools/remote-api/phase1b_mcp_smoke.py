"""MCP smoke for the pulled-forward tools (PORT_NOTES.md section 11.2), driven over MCP stdio
against a running ElegooSlicer MCP build with a TEST --datadir.

Run with the MCP server's own venv (it has the `mcp` package):
  mcp\\.venv\\Scripts\\python.exe tools\\remote-api\\phase1b_mcp_smoke.py <token> <model.stl> <outdir>
"""
import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

TOKEN, MODEL, OUT = sys.argv[1], sys.argv[2], os.path.abspath(sys.argv[3])
SERVER = os.path.join(os.path.dirname(sys.executable), "orcaslicer-mcp.exe")


def body_of(res):
    structured = getattr(res, "structured_content", None) or getattr(res, "structuredContent", None)
    if structured is not None:
        return structured.get("result", structured) if isinstance(structured, dict) else structured
    texts = [c.text for c in res.content if c.type == "text"]
    try:
        return json.loads(texts[0]) if texts else {}
    except ValueError:
        return texts[0]


async def main():
    os.makedirs(OUT, exist_ok=True)
    env = dict(os.environ, ORCA_API_TOKEN=TOKEN, ORCA_API_URL="http://127.0.0.1:13130")
    failures = 0
    async with stdio_client(StdioServerParameters(command=SERVER, args=[], env=env)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            async def run(name, args, expect_error=None):
                nonlocal failures
                res = await session.call_tool(name, args)
                body = body_of(res)
                err = body.get("error") if isinstance(body, dict) else None
                ok = str(err or "").startswith(expect_error) if expect_error else (err is None and not getattr(res, "isError", False))
                failures += not ok
                s = json.dumps(body, ensure_ascii=False)
                print(f"{'ok  ' if ok else 'FAIL'} {name}{args} :: {s[:300]}")
                return body

            names = {t.name for t in (await session.list_tools()).tools}
            for t in ("auto_orient", "set_view", "list_parts", "add_part", "set_part_config",
                      "set_part_filament", "remove_part", "save_project", "paint_region"):
                if t not in names:
                    failures += 1
                    print(f"FAIL tool {t} missing")

            for o in (await run("list_objects", {})).get("objects", []):
                await run("delete_object", {"object_id": o["id"]})
            await run("load_model", {"path": MODEL})
            oid = (await run("list_objects", {}))["objects"][0]["id"]
            await run("transform_object", {"object_id": oid, "rotate": [30, 0, 0]})
            await run("auto_orient", {"object_id": oid})
            await run("set_view", {"view": "preview"})
            await run("set_view", {"view": "prepare"})
            parts = await run("list_parts", {"object_id": oid})
            center = parts["volumes"][0]["position_mm"]
            added = await run("add_part", {"object_id": oid, "type": "modifier", "shape": "box",
                                           "size_mm": [10, 10, 4], "position_mm": center,
                                           "config": {"sparse_infill_density": "50%"}})
            pid = added["volume"]["id"]
            await run("set_part_config", {"object_id": oid, "part_id": pid, "changes": {"wall_loops": 3}})
            await run("set_part_filament", {"object_id": oid, "part_id": pid, "slot": 1})
            await run("set_part_config", {"object_id": oid, "part_id": pid,
                                          "changes": {"post_process": "x.exe"}}, expect_error="blocked_by_local_policy")
            await run("remove_part", {"object_id": oid, "part_id": pid})
            await run("paint_region", {"object_id": oid, "layer": "support", "state": "enforce",
                                       "max_overhang_deg": 45})
            await run("paint_region", {"object_id": oid, "layer": "support", "clear": True,
                                       "box_min": [0, 0, 0], "box_max": [1, 1, 1]}, expect_error="clear_takes_no_box")
            await run("paint_region", {"object_id": oid, "layer": "support", "clear": True})
            proj = os.path.join(OUT, "phase1b_mcp_project.3mf")
            if os.path.exists(proj):
                os.remove(proj)
            await run("save_project", {"path": proj})
            await run("save_project", {"path": proj}, expect_error="file_exists")
            await run("save_project", {"path": proj, "overwrite": True})
    print("RESULT:", "ALL PASS" if not failures else f"{failures} FAILED")
    return failures


sys.exit(asyncio.run(main()))
