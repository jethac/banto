#!/usr/bin/env python3
"""banto-lb — dumb-simple failover load balancer for OpenAI-compatible model servers.

Per listener: an ordered list of backends. Each new connection is spliced to the
first backend whose health probe passes (cached a few seconds). Pure byte
tunnel — SSE streaming, chunked responses, and websockets all pass through
untouched. When every backend is down, connections are refused (fail closed).

Config (~/.config/banto/lb.json or $BANTO_LB_CONFIG):
{
  "health_cache_s": 5,
  "listeners": [
    {"listen": 8010,
     "backends": [
       {"host": "spark.example.ts.net", "port": 8001, "health": "http://spark.example.ts.net:8001/v1/models"},
       {"host": "desktop.example.ts.net", "port": 8801, "health": "http://desktop.example.ts.net:8801/v1/models"}
     ]}
  ]
}

Stdlib only. MIT license.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

CFG_PATH = Path(os.environ.get("BANTO_LB_CONFIG", Path.home() / ".config" / "banto" / "lb.json"))
CFG = json.loads(CFG_PATH.read_text())
CACHE_S = float(CFG.get("health_cache_s", 5))
_health: dict[str, tuple[float, bool]] = {}


def probe(url: str) -> bool:
    now = time.monotonic()
    ts, ok = _health.get(url, (0.0, False))
    if now - ts < CACHE_S:
        return ok
    try:
        with urllib.request.urlopen(url, timeout=1.5):
            ok = True
    except Exception:
        ok = False
    _health[url] = (now, ok)
    return ok


async def pick(backends: list[dict]) -> dict | None:
    loop = asyncio.get_running_loop()
    for b in backends:
        if await loop.run_in_executor(None, probe, b["health"]):
            return b
    return None


async def splice(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def handle(listener: dict, cr: asyncio.StreamReader, cw: asyncio.StreamWriter):
    b = await pick(listener["backends"])
    if b is None:
        sys.stderr.write(f"[banto-lb] :{listener['listen']} all backends down — refusing\n")
        cw.close()
        return
    try:
        br, bw = await asyncio.wait_for(asyncio.open_connection(b["host"], b["port"]), timeout=5)
    except Exception as e:
        _health[b["health"]] = (time.monotonic(), False)  # punish immediately
        sys.stderr.write(f"[banto-lb] connect {b['host']}:{b['port']} failed: {e}\n")
        cw.close()
        return
    await asyncio.gather(splice(cr, bw), splice(br, cw))


async def main():
    servers = []
    for listener in CFG["listeners"]:
        srv = await asyncio.start_server(
            lambda r, w, L=listener: handle(L, r, w),
            CFG.get("bind", "127.0.0.1"), listener["listen"],
        )
        order = " -> ".join(f"{b['host']}:{b['port']}" for b in listener["backends"])
        print(f"banto-lb :{listener['listen']}  {order}")
        servers.append(srv)
    await asyncio.gather(*(s.serve_forever() for s in servers))


if __name__ == "__main__":
    asyncio.run(main())
