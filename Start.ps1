$ErrorActionPreference = "Stop"

# One-command launcher (Windows fallback) for: Portal (FastAPI) + SMS Tool (Streamlit)
# Runs: Portal on http://127.0.0.1:8010  |  SMS Tool on http://localhost:8501

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$venv = Join-Path $root ".venv"
$py   = Join-Path $venv "Scripts\python.exe"

if (-not (Test-Path $py)) {
  py -3.11 -m venv .venv
}

& $py -m pip install --upgrade pip | Out-Null
& $py -m pip install -r "portal\requirements.txt" | Out-Null
& $py -m pip install -r "sms\requirements.txt" | Out-Null

# Start Portal
$portalLog = Join-Path $root "portal.log"
$portalCmd = "-m uvicorn app.main:app --host 127.0.0.1 --port 8010"
$portalProc = Start-Process -FilePath $py -ArgumentList $portalCmd -WorkingDirectory (Join-Path $root "portal") -PassThru -WindowStyle Minimized -RedirectStandardOutput $portalLog -RedirectStandardError $portalLog

# Start SMS Tool
$env:THERAPY_PORTAL_URL = "http://127.0.0.1:8010"
$smsLog = Join-Path $root "sms.log"
$smsArgs = "-m streamlit run apps/fullcalendar_app.py --server.port 8501 --server.address 127.0.0.1"
$smsProc = Start-Process -FilePath $py -ArgumentList $smsArgs -WorkingDirectory (Join-Path $root "sms") -PassThru -WindowStyle Minimized -RedirectStandardOutput $smsLog -RedirectStandardError $smsLog

# Store PIDs for Stop.ps1
@{
  portal_pid = $portalProc.Id
  sms_pid    = $smsProc.Id
} | ConvertTo-Json | Set-Content -Encoding UTF8 (Join-Path $root ".pids.json")

Start-Sleep -Seconds 2
Start-Process "http://localhost:8501"
Start-Process "http://127.0.0.1:8010/timeline"

Write-Host "Started Portal (8010) + SMS Tool (8501)."
Write-Host "Logs: portal.log, sms.log"
