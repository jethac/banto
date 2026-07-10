# banto one-step installer — Windows (Task Scheduler).
#
#   irm https://github.com/jethac/banto/releases/latest/download/install.ps1 | iex
#
# Downloads the latest banto release, writes a default config (auto-update ON),
# registers a scheduled task, starts it, and verifies. Re-run to update config.
# For a true headless service (runs with no user logged in) use NSSM instead —
# see service/WINDOWS.md. Env overrides: BANTO_TOKEN, BANTO_BIND, BANTO_PORT,
# BANTO_DIR, BANTO_REPO, BANTO_NO_AUTOUPDATE.
$ErrorActionPreference = "Stop"

$repo = if ($env:BANTO_REPO) { $env:BANTO_REPO } else { "jethac/banto" }
$dir  = if ($env:BANTO_DIR)  { $env:BANTO_DIR }  else { "$env:USERPROFILE\banto" }
$port = if ($env:BANTO_PORT) { $env:BANTO_PORT } else { "7777" }
$bind = if ($env:BANTO_BIND) { $env:BANTO_BIND } else { "0.0.0.0" }
$cfgdir = "$env:USERPROFILE\.config\banto"

function Say($m) { Write-Host "banto > $m" }

# --- python (pythonw = no console window)
$py = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = (Get-Command python.exe -ErrorAction SilentlyContinue).Source }
if (-not $py) { throw "Python 3.10+ not found. Run: winget install Python.Python.3.12" }

# --- download
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$base = "https://github.com/$repo/releases/latest/download"
Say "downloading banto from $repo (latest release)..."
curl.exe -fsSL -o "$dir\banto.py" "$base/banto.py"
if ($LASTEXITCODE -ne 0) { throw "could not download banto.py" }
curl.exe -fsSL -o "$dir\banto_lb.py" "$base/banto_lb.py" 2>$null

# --- config (never clobber an existing one)
New-Item -ItemType Directory -Force -Path $cfgdir | Out-Null
$cfgpath = "$cfgdir\config.json"
if (-not (Test-Path $cfgpath)) {
  $auto = -not ($env:BANTO_NO_AUTOUPDATE)
  $cfg = [ordered]@{ bind = $bind; port = [int]$port; auto_update = $auto; update_check_interval_hours = 24 }
  if ($env:BANTO_TOKEN) { $cfg["token"] = $env:BANTO_TOKEN }
  ($cfg | ConvertTo-Json) | Set-Content -Encoding UTF8 $cfgpath
  Say "wrote $cfgpath"
} else { Say "config exists - leaving it" }

# --- scheduled task (pythonw = no console window; runs at logon as you).
# For a true no-login headless service use NSSM (AppExit Default Restart) - see
# service/WINDOWS.md; that also makes self-update's restart reliable on Windows.
$action  = "`"$py`" `"$dir\banto.py`""
schtasks /Create /TN banto /SC ONLOGON /RU $env:USERNAME /TR $action /F | Out-Null
schtasks /Run /TN banto | Out-Null
Say "scheduled task 'banto' installed + started"

# --- open the inbound port so the fleet can reach it (needs an elevated shell)
try {
  if (-not (Get-NetFirewallRule -DisplayName "banto" -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName "banto" -Direction Inbound -Protocol TCP -LocalPort $port -Action Allow | Out-Null
  }
  Say "firewall: inbound TCP :$port allowed"
} catch {
  Say "could NOT open firewall :$port - re-run this in an Administrator PowerShell to allow it."
}

# --- verify
Start-Sleep -Seconds 3
try {
  $h = (curl.exe -fsS "http://127.0.0.1:$port/health" | ConvertFrom-Json)
  Say "OK banto $($h.banto) up on :$port - $($h.host) - $($h.os)"
  Say "done. this box self-updates from here."
} catch {
  Say "installed, but :$port isn't answering yet - check Task Scheduler / the banto window"
}
