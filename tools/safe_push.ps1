param(
  [Parameter(Mandatory=$true)]
  [string]$Message
)

Write-Host "==> CI check" -ForegroundColor Cyan
powershell -ExecutionPolicy Bypass -File .\tools\ci_portal_check.ps1
if ($LASTEXITCODE -ne 0) { throw "CI check failed" }

Write-Host "==> Ensure main branch" -ForegroundColor Cyan
git switch main

Write-Host "==> Pull (rebase) before commit" -ForegroundColor Cyan
git pull --rebase origin main
if ($LASTEXITCODE -ne 0) { throw "Pull/rebase failed. Resolve conflicts then re-run." }

Write-Host "==> Stage + commit" -ForegroundColor Cyan
git add -A
git commit -m $Message
if ($LASTEXITCODE -ne 0) {
  Write-Host "No commit created (maybe no changes?)." -ForegroundColor Yellow
}

Write-Host "==> Pull (rebase) again" -ForegroundColor Cyan
git pull --rebase origin main
if ($LASTEXITCODE -ne 0) { throw "Pull/rebase failed after commit. Resolve and re-run." }

Write-Host "==> Push" -ForegroundColor Cyan
git push origin main
if ($LASTEXITCODE -ne 0) { throw "Push failed." }

Write-Host "✅ Done" -ForegroundColor Green
