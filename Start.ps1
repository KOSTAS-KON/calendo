$ErrorActionPreference = "Stop"

Write-Host "Starting Therapy Portal + SMS Calendar..." -ForegroundColor Cyan

# Prefer docker compose if available
function HasCommand($name) {
  return [bool](Get-Command $name -ErrorAction SilentlyContinue)
}

if (-not (HasCommand "docker")) {
  Write-Host "Docker not found. Please install Docker Desktop first." -ForegroundColor Red
  exit 1
}

# Compose file in current folder
$composeFile = Join-Path $PSScriptRoot "docker-compose.yml"
if (-not (Test-Path $composeFile)) {
  Write-Host "docker-compose.yml not found in: $PSScriptRoot" -ForegroundColor Red
  exit 1
}

# Start stack
docker compose -f $composeFile up -d --build

Write-Host ""
Write-Host "Services starting..." -ForegroundColor Green
Write-Host "Portal (after containers are healthy): http://localhost:8080/" -ForegroundColor Yellow
Write-Host "SMS Calendar (if exposed):           http://localhost:8501/" -ForegroundColor Yellow
Write-Host ""
Write-Host "Tip: Use 'docker compose ps' to verify status." -ForegroundColor DarkGray
