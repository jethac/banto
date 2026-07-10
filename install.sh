#!/bin/sh
# banto one-step installer — macOS (launchd) + Linux (systemd).
#
#   curl -fsSL https://github.com/jethac/banto/releases/latest/download/install.sh | sh
#
# Downloads the latest banto release, writes a default config (auto-update ON),
# installs + starts the platform service, and verifies. Idempotent — safe to
# re-run. After this the box self-updates; you never touch it by hand again.
#
# Optional env overrides:
#   BANTO_TOKEN=...             shared fleet auth token (sent as X-Banto-Token)
#   BANTO_BIND=127.0.0.1        listen address (default 0.0.0.0)
#   BANTO_PORT=7777             listen port
#   BANTO_DIR=/path             where banto.py lives (default ~/banto)
#   BANTO_REPO=owner/repo       release source (default jethac/banto)
#   BANTO_LABEL=com.jetha.banto launchd label / systemd unit base
#   BANTO_NO_AUTOUPDATE=1       install check-only (don't auto-apply updates)
set -eu

REPO="${BANTO_REPO:-jethac/banto}"
DIR="${BANTO_DIR:-$HOME/banto}"
CFGDIR="${BANTO_CONFIG_DIR:-$HOME/.config/banto}"
PORT="${BANTO_PORT:-7777}"
BIND="${BANTO_BIND:-0.0.0.0}"
LABEL="${BANTO_LABEL:-com.jetha.banto}"
OS="$(uname -s)"

say() { printf 'banto ▸ %s\n' "$*"; }
die() { printf 'banto ✗ %s\n' "$*" >&2; exit 1; }

# --- 1. python 3.10+
PY="$(command -v python3 || true)"
[ -n "$PY" ] || die "python3 not found — install Python 3.10+ and re-run."
"$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
  || die "python3 too old ($("$PY" -V 2>&1)); need 3.10+."
command -v curl >/dev/null 2>&1 || die "curl not found."

# --- 2. download banto (latest release)
mkdir -p "$DIR"
BASE="https://github.com/$REPO/releases/latest/download"
say "downloading latest banto from $REPO…"
curl -fsSL -o "$DIR/banto.py" "$BASE/banto.py" || die "could not download banto.py"
curl -fsSL -o "$DIR/banto_lb.py" "$BASE/banto_lb.py" 2>/dev/null || true
VER="$(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$DIR/banto.py" | head -1)"
say "got banto ${VER:-?}"

# --- 3. config (never clobber an existing one)
mkdir -p "$CFGDIR"
B_CFG="$CFGDIR/config.json" B_BIND="$BIND" B_PORT="$PORT" B_TOKEN="${BANTO_TOKEN:-}" \
B_AUTO="$([ -n "${BANTO_NO_AUTOUPDATE:-}" ] && echo 0 || echo 1)" \
"$PY" -c '
import json, os
p = os.environ["B_CFG"]
if os.path.exists(p):
    print("banto ▸ config exists — leaving it")
else:
    cfg = {"bind": os.environ["B_BIND"], "port": int(os.environ["B_PORT"]),
           "auto_update": os.environ["B_AUTO"] == "1",
           "update_check_interval_hours": 24}
    if os.environ.get("B_TOKEN"):
        cfg["token"] = os.environ["B_TOKEN"]
    json.dump(cfg, open(p, "w"), indent=2)
    print("banto ▸ wrote", p)
'

# --- 4. install + start the service
case "$OS" in
  Darwin)
    mkdir -p "$HOME/Library/LaunchAgents" "$HOME/.local/state/banto"
    PL="$HOME/Library/LaunchAgents/$LABEL.plist"
    cat > "$PL" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>$DIR/banto.py</string></array>
  <key>EnvironmentVariables</key><dict><key>BANTO_CONFIG_DIR</key><string>$CFGDIR</string></dict>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>$HOME/.local/state/banto/banto.err.log</string>
</dict></plist>
PLIST
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PL"
    say "launchd service '$LABEL' installed"
    ;;
  Linux)
    command -v systemctl >/dev/null 2>&1 || die "no systemd — run '$PY $DIR/banto.py' via your init system."
    SVC="$(printf '%s' "$LABEL" | sed 's/^com\.[^.]*\.//')"   # com.jetha.banto -> banto
    UNIT="[Unit]
Description=banto compute steward
After=network-online.target

[Service]
Environment=BANTO_CONFIG_DIR=$CFGDIR
ExecStart=$PY $DIR/banto.py
Restart=always
RestartSec=3

[Install]
WantedBy=%WB%"
    if [ "$(id -u)" = 0 ]; then
      printf '%s\n' "$UNIT" | sed 's/%WB%/multi-user.target/' > "/etc/systemd/system/$SVC.service"
      systemctl daemon-reload && systemctl enable --now "$SVC"
      say "systemd system service '$SVC' installed"
    elif command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
      printf '%s\n' "$UNIT" | sed "s|\[Service\]|[Service]\nUser=$(id -un)|; s/%WB%/multi-user.target/" \
        | sudo tee "/etc/systemd/system/$SVC.service" >/dev/null
      sudo systemctl daemon-reload && sudo systemctl enable --now "$SVC"
      say "systemd system service '$SVC' installed (User=$(id -un))"
    else
      mkdir -p "$HOME/.config/systemd/user"
      printf '%s\n' "$UNIT" | sed 's/%WB%/default.target/' > "$HOME/.config/systemd/user/$SVC.service"
      systemctl --user daemon-reload && systemctl --user enable --now "$SVC"
      command -v loginctl >/dev/null 2>&1 && loginctl enable-linger "$(id -un)" >/dev/null 2>&1 || true
      say "systemd --user service '$SVC' installed (lingering, no root needed)"
    fi
    ;;
  *) die "unsupported OS '$OS' — on Windows use install.ps1." ;;
esac

# --- 5. verify
i=0
while [ "$i" -lt 12 ]; do
  curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break
  i=$((i + 1)); sleep 1
done
if H="$(curl -fsS "http://127.0.0.1:$PORT/health" 2>/dev/null)"; then
  WHO="$(printf '%s' "$H" | "$PY" -c 'import json,sys;d=json.load(sys.stdin);print(d.get("host"),"·",d.get("os"))' 2>/dev/null)"
  say "✓ banto $VER up on :$PORT — $WHO"
  say "done. this box self-updates from here."
else
  die "installed but :$PORT isn't answering — check $HOME/.local/state/banto/banto.err.log"
fi
