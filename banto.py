#!/usr/bin/env python3
"""banto (番頭) — a lightweight compute steward for home AI fleets.

One tiny daemon per machine. It knows what models this box *can* serve
(profiles), whether the GPU is actually free (gaming guard), and how to spin
things up and down on request. Orchestrators ask; banto accepts or refuses.

HTTP API (default :7777):
  GET  /health          host, OS, GPU utilization/VRAM, busy verdict, running profiles
  GET  /gpu             raw GPU snapshot
  GET  /profiles        configured profiles
  POST /serve           {"profile": "name"}  -> 200 started | 208 already | 409 refused
  POST /stop            {"profile": "name"}  -> 200 stopped | 404
Optional auth: send  X-Banto-Token: <token>  when a token is configured.

Config:   ~/.config/banto/config.json    (see config.example.json)
Profiles: ~/.config/banto/profiles.json  (see profiles.example.json)
State:    ~/.local/state/banto/          (pids + logs)

Stdlib only. MIT license.
"""
from __future__ import annotations

import json
import os
import platform
import shlex
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("BANTO_CONFIG_DIR", Path.home() / ".config" / "banto"))
STATE_DIR = Path(os.environ.get("BANTO_STATE_DIR", Path.home() / ".local" / "state" / "banto"))
VERSION = "0.1.0"


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


CFG = load_json(CONFIG_DIR / "config.json", {})
PORT = int(CFG.get("port", 7777))
BIND = CFG.get("bind", "0.0.0.0")
TOKEN = CFG.get("token", "")
BUSY_UTIL = float(CFG.get("gpu_busy_util_pct", 15))
BUSY_VRAM = float(CFG.get("gpu_busy_vram_pct", 60))
REGISTRY_CMD = CFG.get("registry_cmd", "")  # e.g. "agentbus send banto '*' '{event}'"


def profiles() -> dict:
    return load_json(CONFIG_DIR / "profiles.json", {})


# ---------------------------------------------------------------- GPU snapshot
def gpu_snapshot() -> list[dict]:
    """NVIDIA GPUs via nvidia-smi (Linux/Windows). Empty list when none/unknown."""
    smi = shutil.which("nvidia-smi") or (
        r"C:\Windows\System32\nvidia-smi.exe" if platform.system() == "Windows" else None
    )
    if not smi or not Path(smi).exists():
        return []
    try:
        out = subprocess.run(
            [smi, "--query-gpu=name,utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        gpus = []
        for line in out.splitlines():
            name, util, used, total = [p.strip() for p in line.split(",")]
            gpus.append({
                "name": name,
                "util_pct": float(util),
                "vram_used_mb": float(used),
                "vram_total_mb": float(total),
                "vram_pct": round(100.0 * float(used) / max(float(total), 1), 1),
            })
        return gpus
    except Exception:
        return []


def gpu_busy() -> tuple[bool, str]:
    """The gaming guard: is someone (probably the owner) using the GPU?"""
    gpus = gpu_snapshot()
    for g in gpus:
        if g["util_pct"] >= BUSY_UTIL:
            return True, f"{g['name']} at {g['util_pct']:.0f}% util (threshold {BUSY_UTIL:.0f}%)"
        if g["vram_pct"] >= BUSY_VRAM:
            return True, f"{g['name']} VRAM {g['vram_pct']:.0f}% used (threshold {BUSY_VRAM:.0f}%)"
    return False, ""


# ---------------------------------------------------------------- processes
def pid_file(name: str) -> Path:
    return STATE_DIR / f"{name}.pid"


def running(name: str) -> bool:
    pf = pid_file(name)
    if not pf.exists():
        return False
    try:
        pid = int(pf.read_text().strip())
        if platform.system() == "Windows":
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                                 capture_output=True, text=True, timeout=5).stdout
            return str(pid) in out
        os.kill(pid, 0)
        return True
    except (ValueError, ProcessLookupError, PermissionError, subprocess.SubprocessError):
        return False


def health_ok(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout):
            return True
    except Exception:
        return False


def notify_registry(event: str, detail: dict):
    if not REGISTRY_CMD:
        return
    try:
        payload = json.dumps({"event": event, "host": platform.node(), **detail})
        cmd = REGISTRY_CMD.replace("{event}", payload.replace("'", ""))
        subprocess.Popen(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def start_profile(name: str) -> tuple[int, dict]:
    prof = profiles().get(name)
    if not prof:
        return 404, {"error": f"unknown profile '{name}'"}
    if running(name) or (prof.get("health_url") and health_ok(prof["health_url"])):
        return 208, {"status": "already-running", "profile": name}
    if prof.get("gpu", False):
        busy, why = gpu_busy()
        if busy:
            return 409, {"refused": why, "hint": "GPU in use — likely the owner. Try later."}
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    log = open(STATE_DIR / f"{name}.log", "ab")
    start = prof["start"]
    kwargs: dict = {"stdout": log, "stderr": log}
    if platform.system() == "Windows":
        kwargs["creationflags"] = 0x00000208  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(start if isinstance(start, list) else shlex.split(start),
                            cwd=prof.get("cwd") or None, **kwargs)
    pid_file(name).parent.mkdir(parents=True, exist_ok=True)
    pid_file(name).write_text(str(proc.pid))
    notify_registry("serve", {"profile": name, "pid": proc.pid, "port": prof.get("port")})
    return 200, {"status": "started", "profile": name, "pid": proc.pid,
                 "health_url": prof.get("health_url")}


def stop_profile(name: str) -> tuple[int, dict]:
    prof = profiles().get(name)
    if not prof:
        return 404, {"error": f"unknown profile '{name}'"}
    stop = prof.get("stop")
    if stop:
        subprocess.run(stop if isinstance(stop, list) else shlex.split(stop),
                       timeout=60, capture_output=True)
    elif pid_file(name).exists():
        try:
            pid = int(pid_file(name).read_text().strip())
            if platform.system() == "Windows":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, timeout=15)
            else:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
        except Exception:
            pass
    pid_file(name).unlink(missing_ok=True)
    notify_registry("stop", {"profile": name})
    return 200, {"status": "stopped", "profile": name}


# ---------------------------------------------------------------- HTTP server
class Handler(BaseHTTPRequestHandler):
    server_version = f"banto/{VERSION}"

    def _send(self, code: int, obj: dict):
        body = json.dumps(obj, indent=1).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self) -> bool:
        return not TOKEN or self.headers.get("X-Banto-Token", "") == TOKEN

    def do_GET(self):
        if not self._authed():
            return self._send(401, {"error": "bad token"})
        if self.path == "/health":
            busy, why = gpu_busy()
            self._send(200, {
                "banto": VERSION, "host": platform.node(), "os": platform.system(),
                "gpus": gpu_snapshot(), "gpu_busy": busy, "gpu_busy_reason": why,
                "profiles_running": [n for n in profiles() if running(n)],
                "time": int(time.time()),
            })
        elif self.path == "/gpu":
            self._send(200, {"gpus": gpu_snapshot()})
        elif self.path == "/profiles":
            self._send(200, {n: {k: v for k, v in p.items() if k != "stop"}
                             for n, p in profiles().items()})
        else:
            self._send(404, {"error": "unknown path"})

    def do_POST(self):
        if not self._authed():
            return self._send(401, {"error": "bad token"})
        try:
            length = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "bad json"})
        name = req.get("profile", "")
        if self.path == "/serve":
            self._send(*start_profile(name))
        elif self.path == "/stop":
            self._send(*stop_profile(name))
        else:
            self._send(404, {"error": "unknown path"})

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[banto] {self.address_string()} {fmt % args}\n")


def main():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    notify_registry("up", {"port": PORT})
    print(f"banto {VERSION} on {BIND}:{PORT} — {platform.node()} ({platform.system()}), "
          f"{len(profiles())} profile(s), guard: util>{BUSY_UTIL:.0f}% or vram>{BUSY_VRAM:.0f}%")
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
