# Creates 2 Windows Task Scheduler jobs:
#  1) sms3_queue_reminders  -> queues reminders every 5 minutes
#  2) sms3_send_outbox      -> sends queued messages every 5 minutes
#
# Run as Administrator if your policies require it.

$ErrorActionPreference = "Stop"
$root = (Split-Path -Parent $PSScriptRoot)

$python = Join-Path $root "venv\Scripts\python.exe"
if (!(Test-Path $python)) {
  Write-Host "venv python not found at: $python" -ForegroundColor Yellow
  Write-Host "Create venv first and install requirements, then re-run." -ForegroundColor Yellow
  exit 1
}

# Queue reminders task
schtasks /Create /F `
  /TN "sms3_queue_reminders" `
  /SC MINUTE /MO 5 `
  /TR "`"$python`" -m src.calendar.scheduler --queue-only" `
  /ST 00:00 `
  /RL LIMITED `
  /RU "$env:USERNAME" `
  /NP `
  /WD "$root"

# Send outbox task
schtasks /Create /F `
  /TN "sms3_send_outbox" `
  /SC MINUTE /MO 5 `
  /TR "`"$python`" -m src.calendar.send_outbox" `
  /ST 00:00 `
  /RL LIMITED `
  /RU "$env:USERNAME" `
  /NP `
  /WD "$root"

Write-Host "Created tasks: sms3_queue_reminders and sms3_send_outbox" -ForegroundColor Green
Write-Host "To remove: schtasks /Delete /TN sms3_queue_reminders /F ; schtasks /Delete /TN sms3_send_outbox /F" -ForegroundColor Yellow
