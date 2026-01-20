$ErrorActionPreference = "Continue"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$pidsFile = Join-Path $root ".pids.json"
if (Test-Path $pidsFile) {
  $pids = Get-Content $pidsFile | ConvertFrom-Json
  if ($pids.portal_pid) { Stop-Process -Id $pids.portal_pid -Force -ErrorAction SilentlyContinue }
  if ($pids.sms_pid)    { Stop-Process -Id $pids.sms_pid -Force -ErrorAction SilentlyContinue }
  Remove-Item $pidsFile -Force -ErrorAction SilentlyContinue
}

# Also attempt to stop anything listening on our ports
try {
  $p = (Get-NetTCPConnection -LocalPort 8010 -State Listen -ErrorAction SilentlyContinue).OwningProcess
  if ($p) { Stop-Process -Id $p -Force -ErrorAction SilentlyContinue }
} catch {}
try {
  $p = (Get-NetTCPConnection -LocalPort 8501 -State Listen -ErrorAction SilentlyContinue).OwningProcess
  if ($p) { Stop-Process -Id $p -Force -ErrorAction SilentlyContinue }
} catch {}

Write-Host "Stopped Portal and SMS Tool (if running)."
