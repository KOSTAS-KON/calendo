<#
Convenience wrapper so you can run the CI check from inside the ./portal folder:

  PS> powershell -ExecutionPolicy Bypass -File .\tools\ci_portal_check.ps1

This delegates to the repo-root script at ../.. /tools/ci_portal_check.ps1.
#>

$rootScript = Join-Path $PSScriptRoot "..\..\tools\ci_portal_check.ps1"

if (-Not (Test-Path $rootScript)) {
  Write-Error "Could not find root CI script: $rootScript"
  exit 1
}

& $rootScript
