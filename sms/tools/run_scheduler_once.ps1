# Run from project root: .\tools\run_scheduler_once.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

if (!(Test-Path ".\venv\Scripts\python.exe")) {
  Write-Host "venv not found. Create it first:" -ForegroundColor Yellow
  Write-Host "  py -3.11 -m venv venv" -ForegroundColor Yellow
  Write-Host "  .\venv\Scripts\Activate.ps1" -ForegroundColor Yellow
  Write-Host "  pip install -r .\requirements.txt" -ForegroundColor Yellow
  exit 1
}

.\venv\Scripts\python.exe -m src.calendar.scheduler --queue-only
