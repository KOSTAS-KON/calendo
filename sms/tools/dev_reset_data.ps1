# sms3 developer data reset (safe for demos)
# - Backs up existing data/calendar to data/_backup/<timestamp>/
# - Recreates empty CSVs with headers

$ErrorActionPreference = "Stop"

function Info($m) { Write-Host "[sms3-dev] $m" -ForegroundColor Cyan }
function Fail($m) { Write-Host "[sms3-dev] $m" -ForegroundColor Red; exit 1 }

$ROOT = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $ROOT

$dataDir = Join-Path $ROOT "data\calendar"
$backupRoot = Join-Path $ROOT "data\_backup"

if (!(Test-Path $dataDir)) {
    Info "No data/calendar folder found. Creating..."
    New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
}

# Backup existing
$ts = Get-Date -Format "yyyyMMdd_HHmmss"
$backupDir = Join-Path $backupRoot $ts
New-Item -ItemType Directory -Force -Path $backupDir | Out-Null

Get-ChildItem -Path $dataDir -File -ErrorAction SilentlyContinue | ForEach-Object {
    Copy-Item $_.FullName -Destination $backupDir -Force
}
Info "Backed up existing CSVs to: $backupDir"

# Recreate CSV headers
$customers = @"
customer_id,name,phone
"@
$appointments = @"
appointment_id,customer_id,title,start_iso,end_iso,status,notes,updated_at_iso
"@
$outbox = @"
outbox_id,ts_iso,to,customer_id,appointment_id,message_type,text,status,provider,provider_status,provider_message_id,error,dedupe_key
"@

Set-Content -Path (Join-Path $dataDir "customers.csv") -Value $customers -Encoding UTF8
Set-Content -Path (Join-Path $dataDir "appointments.csv") -Value $appointments -Encoding UTF8
Set-Content -Path (Join-Path $dataDir "outbox.csv") -Value $outbox -Encoding UTF8

Info "Reset complete: customers.csv, appointments.csv, outbox.csv re-created."
