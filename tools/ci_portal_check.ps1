$ErrorActionPreference = "Stop"

Write-Host "==> Portal compile + ORM mapper check" -ForegroundColor Cyan

# Repo root (tools/ is one level down)
$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $repoRoot

function Fail([string]$msg) {
  Write-Host ("ERROR: " + $msg) -ForegroundColor Red
  exit 1
}

# Use python.exe from PATH (avoid py launcher weirdness)
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
  Fail "python executable not found in PATH. Install Python and ensure 'python' works in this terminal."
}

function RunChecked([string]$label, [string[]]$pyArgs) {
  Write-Host ("==> " + $label) -ForegroundColor Yellow

  # IMPORTANT: do NOT use $args here; it's a special automatic variable in PowerShell.
  & python @pyArgs

  if ($LASTEXITCODE -ne 0) {
    Fail ($label + " failed (exit code " + $LASTEXITCODE + ")")
  }
}

# Auto-detect where the Python package "app" lives by locating app\db.py
$appDb = Get-ChildItem -Path $repoRoot -Recurse -File -Filter "db.py" |
  Where-Object { $_.FullName -match "\\app\\db\.py$" } |
  Select-Object -First 1

if (-not $appDb) {
  Fail ("Could not find app\db.py anywhere under " + $repoRoot)
}

$appDir     = Split-Path $appDb.FullName -Parent      # ...\app
$pythonRoot = Split-Path $appDir -Parent               # parent of ...\app
$mainPath   = Join-Path $appDir "main.py"              # ...\app\main.py

# Set PYTHONPATH so `import app.models` works
$env:PYTHONPATH = $pythonRoot.ToString()

Write-Host ("Detected portal package root: " + $env:PYTHONPATH) -ForegroundColor DarkGray

if (Test-Path $mainPath) {
  Write-Host ("Detected portal app entrypoint: " + $mainPath) -ForegroundColor Green
  RunChecked "py_compile app\main.py" @("-m","py_compile",$mainPath)
} else {
  Write-Host ("WARNING: app\main.py not found at: " + $mainPath + ". Compiling all Python files under app\ instead.") -ForegroundColor DarkYellow
  $pyFiles = Get-ChildItem -Path $appDir -Recurse -File -Filter "*.py"
  foreach ($f in $pyFiles) {
    RunChecked ("py_compile " + $f.FullName) @("-m","py_compile",$f.FullName)
  }
}

# Force SQLAlchemy mapper configuration (catches back_populates errors)
$pyCode = @"
import app.models
from app.db import Base
print('tables:', sorted(list(Base.metadata.tables.keys())))
"@

RunChecked "Force ORM mapper config (Base.metadata.tables)" @("-c", $pyCode)

Write-Host "OK: Portal checks passed" -ForegroundColor Green
exit 0
