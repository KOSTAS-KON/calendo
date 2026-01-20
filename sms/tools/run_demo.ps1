\
# Run from project root: .\tools\run_demo.ps1
# Ensures venv python is used (avoids calling Anaconda's streamlit.exe)
$ErrorActionPreference = "Stop"

if (!(Test-Path ".\venv\Scripts\python.exe")) {
  Write-Host "venv not found. Create it first:" -ForegroundColor Yellow
  Write-Host "  python -m venv venv" -ForegroundColor Yellow
  exit 1
}

.\venv\Scripts\python.exe -m streamlit run .\apps\fullcalendar_app.py
