<# 
sms3 tools/dev_run.ps1
- Creates venv if missing
- Installs requirements
- Runs Streamlit app
Works on Windows PowerShell 5.x and PowerShell 7+
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Info($msg) { Write-Host "[sms3] $msg" }
function Write-Warn($msg) { Write-Host "[sms3] WARNING: $msg" -ForegroundColor Yellow }
function Write-Err($msg)  { Write-Host "[sms3] ERROR: $msg" -ForegroundColor Red }

# Resolve project root as parent of tools/
$scriptPath = $MyInvocation.MyCommand.Path
$toolsDir = Split-Path -Parent $scriptPath
$rootDir  = Split-Path -Parent $toolsDir

Write-Info "Project root: $rootDir"
Set-Location $rootDir

$venvDir = Join-Path $rootDir "venv"
$venvPy  = Join-Path $venvDir "Scripts\python.exe"

function Invoke-Python {
  param(
    [Parameter(Mandatory=$true)][string[]]$Args,
    [switch]$UseVenv
  )
  if ($UseVenv) {
    if (-not (Test-Path $venvPy)) { throw "Venv python not found at $venvPy" }
    & $venvPy @Args
    return
  }

  # Use the Windows py launcher if available
  $pyCmd = Get-Command py -ErrorAction SilentlyContinue
  if ($null -ne $pyCmd) {
    & py @Args
    return
  }

  # Fallback to python on PATH
  $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
  if ($null -ne $pythonCmd) {
    & python @Args
    return
  }

  throw "No Python found. Install Python 3.10+ (Windows installer) and ensure 'py' launcher is available."
}

# --- ensure venv exists ---
if (-not (Test-Path $venvPy)) {
  Write-Info "No venv found. Creating: $venvDir"

  # Try create venv with py -3.11 first, then py -3.10, then generic py, then python
  $created = $false

  $candidates = @(
    @("-3.11","-m","venv",$venvDir),
    @("-3.10","-m","venv",$venvDir),
    @("-m","venv",$venvDir)
  )

  foreach ($cand in $candidates) {
    try {
      # Ensure array of strings (so .Count exists)
      [string[]]$pythonArgs = @($cand)
      Write-Info ("Creating venv with: py " + ($pythonArgs -join " "))
      & py @pythonArgs
      $created = $true
      break
    } catch {
      Write-Warn ("Venv create attempt failed: " + $_.Exception.Message)
    }
  }

  if (-not $created) {
    try {
      Write-Info "Creating venv with: python -m venv $venvDir"
      & python -m venv $venvDir
      $created = $true
    } catch {
      Write-Err "Failed to create venv. Ensure Python includes 'venv' (in Windows installer: Modify -> ensure 'pip' and 'venv' are selected)."
      throw
    }
  }
}

# --- install deps ---
Write-Info "Upgrading pip..."
Invoke-Python -UseVenv -Args @("-m","pip","install","--upgrade","pip")

if (Test-Path (Join-Path $rootDir "requirements.txt")) {
  Write-Info "Installing requirements..."
  Invoke-Python -UseVenv -Args @("-m","pip","install","-r", (Join-Path $rootDir "requirements.txt"))
} else {
  Write-Warn "requirements.txt not found; skipping dependency install."
}

# --- run app ---
$appPath = Join-Path $rootDir "apps\fullcalendar_app.py"
if (-not (Test-Path $appPath)) { throw "App file not found: $appPath" }

Write-Info "Launching Streamlit..."
Invoke-Python -UseVenv -Args @("-m","streamlit","run",$appPath)
