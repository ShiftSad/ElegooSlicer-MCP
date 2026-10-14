"""Phase 1 parity smoke test: drive the UNMODIFIED orcaslicer-mcp 0.1.17 server over MCP stdio
against a running ElegooSlicer MCP build (PORT_PLAN.md phase 1, step 4).

Run with the MCP server's own venv (it has the `mcp` package):
  mcp\\.venv\\Scripts\\python.exe tools\\remote-api\\phase1_smoke.py <token> <model.stl> <outdir>

Writes every tool result to <outdir>/smoke_<n>_<tool>.json and rendered PNGs next to them.
"""
import asyncio
import base64
import json
import os
import sys
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

TOKEN, MODEL, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
SERVER = os.path.join(os.path.dirname(sys.executable), "orcaslicer-mcp.exe")
CC2 = "Elegoo Centauri Carbon 2 0.4 nozzle"

STEPS = [
    ("get_status", {}),
    ("list_plates", {}),
    ("select_preset", {"type": "printer", "name": CC2}),
    ("get_status", {}),
    ("load_model", {"path": MODEL}),
    ("render_plate", {"view": "editor"}),
    ("set_config", {"changes": {"sparse_infill_density": "15%"}}),
    ("slice_and_wait", {"timeout": 600}),
    ("get_slice_breakdown", {}),
    ("render_plate", {"view": "preview"}),
    ("describe_plate", {}),
    ("list_plates", {}),
]


def short(obj, n=400):
    s = json.dumps(obj, ensure_ascii=False) if not isinstance(obj, str) else obj
    return s if len(s) <= n else s[:n] + "..."


async def main():
    os.makedirs(OUT, exist_ok=True)
    env = dict(os.environ, ORCA_API_TOKEN=TOKEN, ORCA_API_URL="http://127.0.0.1:13130")
    params = StdioServerParameters(command=SERVER, args=[], env=env)
    failures = 0
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print(f"server exposes {len(tools.tools)} tools")
            for i, (name, args) in enumerate(STEPS, 1):
                t0 = time.time()
                res = await session.call_tool(name, args)
                dt = time.time() - t0
                is_error = getattr(res, "is_error", None) if hasattr(res, "is_error") else getattr(res, "isError", False)
                structured = getattr(res, "structured_content", None) if hasattr(res, "structured_content") else getattr(res, "structuredContent", None)
                texts, images = [], 0
                for c in res.content:
                    if c.type == "text":
                        texts.append(c.text)
                    elif c.type == "image":
                        images += 1
                        png = os.path.join(OUT, f"smoke_{i:02d}_{name}_{args.get('view', '')}.png")
                        with open(png, "wb") as f:
                            f.write(base64.b64decode(c.data))
                payload = {"tool": name, "args": args, "isError": is_error, "seconds": round(dt, 1),
                           "structured": structured, "text": texts, "images": images}
                with open(os.path.join(OUT, f"smoke_{i:02d}_{name}.json"), "w", encoding="utf-8") as f:
                    json.dump(payload, f, indent=1, ensure_ascii=False)
                body = structured if structured is not None else (texts[0] if texts else "")
                has_error = is_error or (isinstance(body, dict) and "error" in body) or \
                    (isinstance(body, str) and body.lstrip().startswith('{"error"'))
                failures += bool(has_error)
                print(f"[{i:02d}] {'FAIL' if has_error else 'ok  '} {name}{args if args else ''} "
                      f"{dt:5.1f}s images={images} :: {short(body)}")
    print(f"done: {len(STEPS) - failures}/{len(STEPS)} without error")
    return failures


sys.exit(asyncio.run(main()))
