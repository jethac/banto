# banto on Windows

Requirements: Python 3.10+ (`winget install Python.Python.3.12`). `nvidia-smi`
ships with the NVIDIA driver, banto finds it automatically.

## Task Scheduler (no extra tools)
```powershell
schtasks /Create /TN banto /SC ONSTART /RU $env:USERNAME /TR `
  "pythonw.exe C:\banto\banto.py"
schtasks /Run /TN banto
```

## NSSM (proper service, auto-restart)
```powershell
winget install nssm
nssm install banto "C:\Python312\pythonw.exe" "C:\banto\banto.py"
nssm set banto AppExit Default Restart
nssm start banto
```

Config lives at `%USERPROFILE%\.config\banto\`. Verify: `curl http://localhost:7777/health`.
The gaming guard reads GPU utilization via nvidia-smi — profiles with `"gpu": true`
are refused with HTTP 409 while you're playing.
