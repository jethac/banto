# banto 番頭

**A lightweight compute steward for home AI fleets.** One tiny daemon per machine. It knows what models the box can serve, whether the GPU is actually free, and how to spin things up and down when an orchestrator asks. Crucially, it can say **no**.

The name: a *bantō* was the head clerk of an Edo-era merchant house — the one who decided what the shop's resources were spent on while the owner was busy.

## Why

If you run agents at home you eventually have: one big inference box that's also your experiment machine, a gaming PC with a perfectly good GPU that's *sometimes* a gaming PC, and a small always-on box. Cloud orchestrators solve this with Kubernetes. Your house does not want Kubernetes.

banto is the minimum viable answer:

- **`banto.py`** — per-machine HTTP daemon (stdlib only, macOS/Linux/Windows):
  - `GET /health` — host, OS, GPU utilization/VRAM, busy verdict, running profiles
  - `POST /serve {"profile": "voice-oracle"}` — start a named profile (docker compose, vLLM, llama-server, anything)
  - `POST /stop` — stop it
  - **The politeness guard**: profiles marked `"gpu": true` are refused with `409` when GPU utilization or VRAM crosses a threshold — because the human playing a game on that GPU outranks your agent. Configurable (`gpu_busy_util_pct`, `gpu_busy_vram_pct`).
  - Optional self-registration into whatever fleet ledger you run (`registry_cmd` — a shell hook fired on up/serve/stop).
  - Optional shared-token auth (`X-Banto-Token`).
- **`banto_lb.py`** — a dumb-simple failover proxy for OpenAI-compatible servers: ordered backends per listener, first healthy backend wins per connection, raw byte splice so SSE/streaming/websockets pass through untouched. All backends down → fail closed.

No database. No queue. No sidecar. Two files.

## Install

```bash
git clone https://github.com/jethac/banto && cd banto
mkdir -p ~/.config/banto
cp config.example.json  ~/.config/banto/config.json
cp profiles.example.json ~/.config/banto/profiles.json   # edit for this machine
python3 banto.py
```

Run it as a service: `service/` has a launchd plist (macOS), a systemd unit (Linux), and Windows instructions (Task Scheduler / NSSM).

For the failover proxy: `cp lb.example.json ~/.config/banto/lb.json`, edit backends, `python3 banto_lb.py`.

## Example: voice stack with a gaming-aware fallback

Big box serves the voice models normally. When it's claimed for experiments, the proxy fails over to the gaming PC — *if* nobody's gaming:

```
clients ──► banto-lb :8010 ──► bigbox:8001   (primary)
                          └──► gamingpc:8801 (fallback — refused while GPU is busy)
```

On the gaming PC, `POST /serve {"profile": "voice-reflex"}` returns `409 {"refused": "RTX 5060 Ti at 93% util"}` mid-game, and `200 {"status": "started"}` at 2am.

## Non-goals

Scheduling, bin-packing, multi-tenant auth, TLS termination, or anything that smells like an enterprise. Put a tailnet in front of it and keep it simple.

## License

MIT.
