#!/usr/bin/env python3
"""banto (番頭) — a lightweight compute steward for home AI fleets.

One tiny daemon per machine. It knows what models this box *can* serve
(profiles), whether the GPU is actually free (gaming guard), and how to spin
things up and down on request. Orchestrators ask; banto accepts or refuses.

HTTP API (default :7777):
  GET  /health          host, OS, GPU utilization/VRAM, busy verdict, running profiles
  GET  /gpu             raw GPU snapshot
  GET  /shape           the INVARIANT hardware shape (accelerator, envelope,
                        bandwidth, engines) — detected once at startup, cached,
                        keyed by shape_hash. Orchestrators: cache this per host.
  GET  /usage           live state only (free/allocated memory, GPU util, busy
                        verdict, running profiles) — cheap to poll.
  GET  /capability      back-compat merged view: cached shape + live usage
  POST /fit             {"params_b": 120, "active_params_b": 12, "quant_bits": 4,
                         "context": 32768, "kv_bits": 8}
                        -> fits? headroom? rough batch-1 decode tok/s (roofline:
                        bandwidth / active weight bytes). Estimates, clearly labeled.
  GET  /artifacts       workstream artifact inventory: git repos under
                        artifact_roots — branch, dirty files, UNPUSHED commits,
                        workstream tag, at_risk list (machine-only work)
  POST /archive         {"path": "/repo"} -> snapshot machine-only state to
                        archive_dir: all-refs bundle + dirty.patch + untracked tar
  GET  /profiles        configured profiles
  POST /serve           {"profile": "name"}  -> 200 started | 208 already | 409 refused
  POST /stop            {"profile": "name"}  -> 200 stopped | 404
  GET  /update           self-update status: current vs latest GitHub release
  POST /update          {"apply": true} -> pull, verify, replace, restart in place
Optional auth: send  X-Banto-Token: <token>  when a token is configured.

CLI:  banto.py --version | --check-update | --self-update
Self-update pulls the newest release from update_repo (default jethac/banto),
applies only strictly-newer versions, compile-checks + checksum-verifies before
an atomic swap (keeps <banto>.bak), and restarts in place. auto_update:true in
config applies automatically; update_check_interval_hours:0 disables it.

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
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("BANTO_CONFIG_DIR", Path.home() / ".config" / "banto"))
STATE_DIR = Path(os.environ.get("BANTO_STATE_DIR", Path.home() / ".local" / "state" / "banto"))
VERSION = "0.5.1"


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
UPDATE_REPO = CFG.get("update_repo", "jethac/banto")   # owner/repo to pull releases from
AUTO_UPDATE = bool(CFG.get("auto_update", False))      # apply updates automatically when found
UPDATE_INTERVAL_H = float(CFG.get("update_check_interval_hours", 24))  # 0 disables the check


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


# ---------------------------------------------------------------- capability
# Rough sequential-read bandwidth by accelerator, GB/s. The dominant term for
# batch-1 decode speed. Override with "bandwidth_gbps" in config.json.
BANDWIDTH_TABLE = [
    ("GB10", 273), ("GB200", 8000), ("B200", 8000),
    ("RTX 5090", 1792), ("RTX 5080", 960), ("RTX 5070 Ti", 896),
    ("RTX 5060 Ti", 448), ("RTX 5060", 448),
    ("RTX 4090", 1008), ("RTX 4080", 717), ("RTX 4070", 504), ("RTX 4060", 272),
    ("RTX 3090", 936), ("RTX 3080", 760), ("RTX 3060", 360),
    ("Apple M4 Max", 546), ("Apple M4 Pro", 273), ("Apple M4", 120),
    ("Apple M3 Max", 400), ("Apple M3 Pro", 150), ("Apple M3", 100),
    ("Apple M2 Ultra", 800), ("Apple M2 Max", 400), ("Apple M2 Pro", 200), ("Apple M2", 100),
    ("Apple M1 Ultra", 800), ("Apple M1 Max", 400), ("Apple M1 Pro", 200), ("Apple M1", 68),
]


def _mac_chip() -> str:
    try:
        return subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"],
                              capture_output=True, text=True, timeout=3).stdout.strip()
    except Exception:
        return ""


def _mem_bytes() -> tuple[int, int]:
    """(total, free-ish) system RAM in bytes, best effort per platform."""
    system = platform.system()
    try:
        if system == "Darwin":
            total = int(subprocess.run(["sysctl", "-n", "hw.memsize"],
                                       capture_output=True, text=True, timeout=3).stdout)
            vm = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=3).stdout
            page = 16384 if "page size of 16384" in vm else 4096
            counts = {}
            for line in vm.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    try:
                        counts[k.strip()] = int(v.strip().rstrip("."))
                    except ValueError:
                        continue  # header line: "(page size of N bytes)"
            free = (counts.get("Pages free", 0) + counts.get("Pages inactive", 0)
                    + counts.get("Pages purgeable", 0)) * page
            return total, free
        if system == "Linux":
            info = Path("/proc/meminfo").read_text()
            kv = {l.split(":")[0]: int(l.split()[1]) for l in info.splitlines() if ":" in l}
            return kv.get("MemTotal", 0) * 1024, kv.get("MemAvailable", 0) * 1024
        if system == "Windows":
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-CimInstance Win32_OperatingSystem | "
                 "Select-Object TotalVisibleMemorySize,FreePhysicalMemory | ConvertTo-Json)"],
                capture_output=True, text=True, timeout=10).stdout
            d = json.loads(out)
            return int(d["TotalVisibleMemorySize"]) * 1024, int(d["FreePhysicalMemory"]) * 1024
    except Exception:
        pass
    return 0, 0


def _bandwidth(accel_name: str) -> float:
    if CFG.get("bandwidth_gbps"):
        return float(CFG["bandwidth_gbps"])
    for key, bw in BANDWIDTH_TABLE:
        if key.lower() in accel_name.lower():
            return float(bw)
    return 0.0


_SHAPE: dict = {}


def shape() -> dict:
    """The invariant hardware shape — detected ONCE at startup, then cached.

    Orchestrators should cache this per-host (key on shape_hash) and poll only
    /usage for live state. Restart banto to re-detect (hardware changed).
    """
    global _SHAPE
    if _SHAPE:
        return _SHAPE
    gpus = gpu_snapshot()
    total_ram, _ = _mem_bytes()
    if gpus:  # discrete NVIDIA: the envelope is VRAM
        g = gpus[0]
        envelope_gb = g["vram_total_mb"] / 1024
        accel, unified = g["name"], False
    else:
        chip = _mac_chip() if platform.system() == "Darwin" else platform.processor() or platform.machine()
        accel, unified = chip, platform.system() == "Darwin"
        envelope_gb = total_ram / 1e9
    usable_frac = float(CFG.get("usable_fraction", 0.75 if unified else 0.85))
    engines = [e for e in ("docker", "vllm", "llama-server", "ollama", "lms")
               if shutil.which(e)]
    for name, url in (CFG.get("engine_probes") or {}).items():
        if health_ok(url, 1.0):
            engines.append(name)
    _SHAPE = {
        "host": platform.node(), "os": platform.system(),
        "accelerator": accel, "unified_memory": unified,
        "envelope_gb": round(envelope_gb, 1),
        "usable_gb": round(envelope_gb * usable_frac, 1),
        "system_ram_total_gb": round(total_ram / 1e9, 1),
        "bandwidth_gbps_est": _bandwidth(accel),
        "engines": engines,
        "detected_at": int(time.time()),
        "note": "static shape — cache me; poll /usage for live state; restart banto after hardware changes",
    }
    import hashlib
    _SHAPE["shape_hash"] = hashlib.sha256(
        json.dumps({k: v for k, v in _SHAPE.items() if k != "detected_at"},
                   sort_keys=True).encode()).hexdigest()[:12]
    return _SHAPE


def usage() -> dict:
    """Live state only — cheap to poll."""
    gpus = gpu_snapshot()
    _, free_ram = _mem_bytes()
    s = shape()
    if gpus:
        g = gpus[0]
        free_gb = (g["vram_total_mb"] - g["vram_used_mb"]) / 1024
    else:
        free_gb = free_ram / 1e9
    busy, why = gpu_busy()
    return {
        "shape_hash": s["shape_hash"],
        "free_gb": round(free_gb, 1),
        "allocated_gb": round(max(s["envelope_gb"] - free_gb, 0), 1),
        "gpus": gpus, "gpu_busy": busy, "gpu_busy_reason": why,
        "profiles_running": [n for n in profiles() if running(n)],
        "time": int(time.time()),
    }


def capability() -> dict:
    """Back-compat merged view: cached shape + live usage."""
    return {**shape(), **usage()}


def fit(req: dict) -> dict:
    """Can a model fit here, and roughly how fast is batch-1 decode?

    weight bytes = params * quant_bits/8 ; KV estimate = 16MB per 1B params per
    1k tokens at fp16 (matches ~8B/GQA models), scaled by kv_bits/16. Decode
    tok/s roofline = bandwidth / bytes touched per token (active params for MoE).
    """
    cap = shape()
    params_b = float(req.get("params_b", 0))
    if params_b <= 0:
        return {"error": "params_b required (billions of parameters)"}
    active_b = float(req.get("active_params_b", params_b))
    qbits = float(req.get("quant_bits", 4))
    kvbits = float(req.get("kv_bits", 16))
    context = float(req.get("context", 8192))
    weight_gb = params_b * qbits / 8
    kv_gb = params_b * 0.016 * (context / 1000.0) * (kvbits / 16.0)
    if req.get("kv_gb_override") is not None:
        kv_gb = float(req["kv_gb_override"])
    overhead_gb = float(req.get("overhead_gb", 1.5))
    total_gb = weight_gb + kv_gb + overhead_gb
    usable = cap["usable_gb"]
    headroom = usable - total_gb
    bw = cap["bandwidth_gbps_est"]
    active_gb = active_b * qbits / 8
    tps = round(bw / active_gb, 1) if bw and active_gb else None
    verdict = ("no" if headroom < 0 else
               "tight" if headroom < 0.15 * usable else "comfortable")
    return {
        "verdict": verdict, "fits": headroom >= 0,
        "weight_gb": round(weight_gb, 1), "kv_cache_gb": round(kv_gb, 1),
        "overhead_gb": overhead_gb, "total_needed_gb": round(total_gb, 1),
        "usable_gb": usable, "headroom_gb": round(headroom, 1),
        "est_decode_tok_s": tps,
        "assumptions": f"{params_b:g}B params @ {qbits:g}-bit"
                       + (f" ({active_b:g}B active/MoE)" if active_b != params_b else "")
                       + f", {context:g} ctx @ kv{kvbits:g}; roofline bw {bw} GB/s",
        "note": "estimate — validate with a real load before trusting in anger",
    }


# ---------------------------------------------------------------- artifacts
def _git(repo: str, *args) -> str:
    try:
        return subprocess.run(["git", "-C", repo] + list(args),
                              capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception:
        return ""


def artifacts() -> dict:
    """Workstream artifact inventory: git repos under artifact_roots with the
    state that matters for recovery — branch, dirty files, UNPUSHED commits,
    last activity, workstream tag. 'Where are the artifacts for X' should be a
    query, not a memory."""
    roots = [os.path.expanduser(r) for r in CFG.get("artifact_roots", [])]
    ws_map = {os.path.expanduser(k): v for k, v in (CFG.get("workstreams") or {}).items()}
    repos = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, _ in os.walk(root):
            if dirpath.count(os.sep) - root.count(os.sep) > 2:
                dirnames[:] = []
                continue
            if ".git" in dirnames or os.path.isfile(os.path.join(dirpath, ".git")):
                dirnames[:] = []
                dirty = _git(dirpath, "status", "--porcelain")
                unpushed = _git(dirpath, "rev-list", "--count", "@{u}..HEAD") or "no-upstream"
                repos.append({
                    "path": dirpath,
                    "workstream": next((v for k, v in sorted(ws_map.items(), key=lambda x: -len(x[0]))
                                        if dirpath.startswith(k)), None),
                    "branch": _git(dirpath, "rev-parse", "--abbrev-ref", "HEAD"),
                    "dirty_files": len(dirty.splitlines()),
                    "unpushed_commits": unpushed,
                    "last_activity_days": round((time.time() - os.path.getmtime(dirpath)) / 86400, 1),
                })
    at_risk = [r["path"] for r in repos
               if r["dirty_files"] or (str(r["unpushed_commits"]).isdigit() and int(r["unpushed_commits"]) > 0)]
    return {"host": platform.node(), "roots": roots, "repos": repos,
            "at_risk": at_risk,
            "note": "at_risk = dirty or unpushed work that exists ONLY on this machine"}


def archive_repo(req: dict) -> tuple[int, dict]:
    """Snapshot a repo's machine-only state to the archive sink: bundle of all
    local refs + dirty.patch + untracked tar (size-capped). Recovery beats
    remembering."""
    path = os.path.expanduser(req.get("path", ""))
    roots = [os.path.expanduser(r) for r in CFG.get("artifact_roots", [])]
    if not path or not any(path.startswith(r) for r in roots):
        return 400, {"error": "path required and must be under artifact_roots"}
    sink = os.path.expanduser(CFG.get("archive_dir", ""))
    if not sink:
        return 400, {"error": "archive_dir not configured"}
    ts = time.strftime("%Y%m%d-%H%M%S")
    dest = Path(sink) / platform.node().split(".")[0] / f"{Path(path).name}-{ts}"
    dest.mkdir(parents=True, exist_ok=True)
    out = {"dest": str(dest), "parts": []}
    if subprocess.run(["git", "-C", path, "bundle", "create",
                       str(dest / "all-refs.bundle"), "--all"],
                      capture_output=True, timeout=300).returncode == 0:
        out["parts"].append("all-refs.bundle")
    diff = _git(path, "diff", "HEAD")
    if diff:
        (dest / "dirty.patch").write_text(diff)
        out["parts"].append("dirty.patch")
    untracked = [f for f in _git(path, "ls-files", "--others", "--exclude-standard").splitlines() if f]
    if untracked:
        big = [f for f in untracked
               if os.path.getsize(os.path.join(path, f)) > 100 * 2**20
               if os.path.exists(os.path.join(path, f))]
        keep = [f for f in untracked if f not in big]
        if keep:
            subprocess.run(["tar", "czf", str(dest / "untracked.tar.gz"), "-C", path] + keep,
                           capture_output=True, timeout=300)
            out["parts"].append(f"untracked.tar.gz ({len(keep)} files)")
        if big:
            (dest / "SKIPPED-large-files.txt").write_text("\n".join(big))
            out["parts"].append(f"SKIPPED {len(big)} files >100MB (listed)")
    notify_registry("archive", {"path": path, "dest": str(dest)})
    return 200, out


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


# ---------------------------------------------------------------- self-update
# banto watches its own GitHub releases and can replace itself in place. Pure
# stdlib. Only strictly-NEWER versions are applied; the download is compile-
# checked (and checksum-verified when the release ships SHA256SUMS) before the
# swap; the previous file is kept as <banto>.bak. Restart is an in-place
# os.execv so the supervisor (launchd/systemd) sees the same PID and treats it
# as still-alive — no crash, no flap.
def _ver_tuple(v: str) -> tuple:
    out = []
    for part in str(v).strip().lstrip("vV").split("."):
        digits = "".join(c for c in part if c.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out)


def _gh_api(path: str):
    req = urllib.request.Request(
        f"https://api.github.com/repos/{UPDATE_REPO}{path}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": f"banto/{VERSION}"})
    tok = CFG.get("github_token") or os.environ.get("GITHUB_TOKEN")
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _asset_preference() -> list:
    """Release-asset names to try, most platform-specific first, portable .py last.
    banto is pure Python today so the universal banto.py wins on every host, but
    the order lets a future compiled build (banto-darwin-arm64, ...) take over
    per platform without changing this code."""
    osname = platform.system().lower()               # darwin / linux / windows
    arch = platform.machine().lower()                # arm64 / x86_64 / amd64
    arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(arch, arch)
    return [f"banto-{osname}-{arch}", f"banto-{osname}-{arch}.py",
            f"banto-{osname}", "banto.py"]


def check_update() -> dict:
    """Compare our VERSION to the latest GitHub release. Never raises."""
    try:
        rel = _gh_api("/releases/latest")
    except Exception as e:
        return {"current": VERSION, "repo": UPDATE_REPO, "error": f"{type(e).__name__}: {e}"}
    tag = rel.get("tag_name", "")
    assets = rel.get("assets", [])
    available = bool(tag) and _ver_tuple(tag) > _ver_tuple(VERSION)
    chosen = None
    if available:
        by_name = {a.get("name"): a for a in assets}
        chosen = next((by_name[n] for n in _asset_preference() if n in by_name), None)
    return {
        "current": VERSION, "latest": tag, "repo": UPDATE_REPO,
        "update_available": available, "published_at": rel.get("published_at"),
        "asset": chosen.get("name") if chosen else None,
        "_asset_url": chosen.get("browser_download_url") if chosen else None,
        "_assets": assets,
    }


def update_status() -> dict:
    """Public view of check_update() (private _fields stripped) + this box's policy,
    so `GET /update` shows both 'is there a newer release' and 'will I apply it'."""
    out = {k: v for k, v in check_update().items() if not k.startswith("_")}
    out["auto_update"] = AUTO_UPDATE
    out["check_interval_hours"] = UPDATE_INTERVAL_H
    return out


def _download(url: str) -> bytes:
    req = urllib.request.Request(
        url, headers={"User-Agent": f"banto/{VERSION}", "Accept": "application/octet-stream"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def _checksum_ok(data: bytes, assets: list, asset_name: str) -> tuple:
    """Verify against the release's SHA256SUMS asset if present. Corruption/partial
    -download guard — NOT a substitute for signing (a compromised release could
    ship matching sums). Returns (ok, why)."""
    sums = next((a for a in assets if a.get("name", "").upper().startswith("SHA256")), None)
    if not sums:
        return True, "no SHA256SUMS in release (integrity not verified)"
    import hashlib
    try:
        text = _download(sums["browser_download_url"]).decode()
    except Exception as e:
        return False, f"could not fetch SHA256SUMS: {e}"
    want = next((ln.split()[0].lower() for ln in text.splitlines()
                 if len(ln.split()) >= 2 and ln.split()[1].lstrip("*") == asset_name), None)
    if not want:
        return True, f"{asset_name} not listed in SHA256SUMS"
    got = hashlib.sha256(data).hexdigest().lower()
    return got == want, f"want {want[:12]} got {got[:12]}"


def self_update(apply: bool = True, restart: bool = True) -> tuple:
    """Pull + apply a newer release. Returns (http_code, dict)."""
    info = check_update()
    if info.get("error"):
        return 502, {"status": "check-failed", "current": VERSION, "error": info["error"]}
    if not info.get("update_available"):
        return 200, {"status": "up-to-date", "current": VERSION, "latest": info.get("latest")}
    if not info.get("_asset_url"):
        return 404, {"status": "no-asset-for-platform", "current": VERSION,
                     "latest": info.get("latest"),
                     "hint": "release has no asset matching this platform"}
    if not info["asset"].endswith(".py"):
        return 501, {"status": "binary-swap-unsupported", "asset": info["asset"],
                     "hint": "this release ships a non-.py artifact; swap it via your installer"}
    if not apply:
        return 200, {"status": "update-available", "current": VERSION,
                     "latest": info["latest"], "asset": info["asset"]}
    try:
        data = _download(info["_asset_url"])
    except Exception as e:
        return 502, {"status": "download-failed", "error": str(e)}
    ok, why = _checksum_ok(data, info["_assets"], info["asset"])
    if not ok:
        return 502, {"status": "checksum-failed", "detail": why}
    target = Path(os.path.realpath(__file__))
    tmp = target.with_name(target.name + ".new")
    tmp.write_bytes(data)
    import py_compile
    try:
        py_compile.compile(str(tmp), doraise=True)          # never swap in broken code
    except py_compile.PyCompileError as e:
        tmp.unlink(missing_ok=True)
        return 500, {"status": "download-does-not-compile", "detail": str(e)}
    shutil.copy2(target, target.with_name(target.name + ".bak"))
    os.replace(tmp, target)                                  # atomic
    notify_registry("updated", {"from": VERSION, "to": info["latest"]})
    sys.stderr.write(f"[banto] updated {VERSION} -> {info['latest']} ({why})\n")
    if restart:
        threading.Thread(target=_restart_soon, daemon=True).start()
    return 200, {"status": "updating" if restart else "updated-on-disk",
                 "from": VERSION, "to": info["latest"], "checksum": why,
                 "note": ("restarting in place into the new version" if restart
                          else "file replaced; restart the banto service to activate")}


def _restart_soon():
    time.sleep(0.4)  # let the triggering HTTP response flush first
    os.execv(sys.executable, [sys.executable, os.path.realpath(__file__)] + sys.argv[1:])


def _update_loop():
    delay = 45.0  # first check shortly after boot, then every UPDATE_INTERVAL_H
    while True:
        time.sleep(delay)
        delay = max(UPDATE_INTERVAL_H, 0.1) * 3600
        try:
            info = check_update()
            if info.get("update_available"):
                sys.stderr.write(f"[banto] update available: {VERSION} -> {info.get('latest')}"
                                 + (" (auto-applying)" if AUTO_UPDATE else "") + "\n")
                notify_registry("update-available",
                                {"current": VERSION, "latest": info.get("latest")})
                if AUTO_UPDATE:
                    self_update(apply=True)
        except Exception:
            pass


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
        elif self.path == "/capability":
            self._send(200, capability())
        elif self.path == "/shape":
            self._send(200, shape())
        elif self.path == "/usage":
            self._send(200, usage())
        elif self.path == "/artifacts":
            self._send(200, artifacts())
        elif self.path == "/profiles":
            self._send(200, {n: {k: v for k, v in p.items() if k != "stop"}
                             for n, p in profiles().items()})
        elif self.path == "/update":
            self._send(200, update_status())
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
        if self.path == "/fit":
            return self._send(200, fit(req))
        if self.path == "/archive":
            return self._send(*archive_repo(req))
        if self.path == "/update":
            return self._send(*self_update(apply=bool(req.get("apply", True))))
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
    s = shape()  # detect the hardware shape once, up front
    notify_registry("up", {"port": PORT, "shape_hash": s["shape_hash"]})
    print(f"banto {VERSION} on {BIND}:{PORT} — {s['accelerator']}, "
          f"{s['usable_gb']}GB usable @ ~{s['bandwidth_gbps_est']:.0f}GB/s "
          f"[{s['shape_hash']}], {len(profiles())} profile(s), "
          f"guard: util>{BUSY_UTIL:.0f}% or vram>{BUSY_VRAM:.0f}%, "
          f"updates: {'auto' if AUTO_UPDATE else 'check-only'} from {UPDATE_REPO}"
          if UPDATE_INTERVAL_H > 0 else "updates: off")
    if UPDATE_INTERVAL_H > 0:
        threading.Thread(target=_update_loop, daemon=True).start()
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd in ("--version", "-V", "version"):
            print(VERSION); sys.exit(0)
        if cmd in ("--check-update", "check-update"):
            print(json.dumps(update_status(), indent=1)); sys.exit(0)
        if cmd in ("--self-update", "self-update"):
            # CLI is a one-shot: replace the file on disk, don't exec (the daemon
            # is a separate process — restart the banto service to activate).
            code, res = self_update(apply=True, restart=False)
            print(json.dumps(res, indent=1)); sys.exit(0 if code == 200 else 1)
    main()
