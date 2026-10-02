# Watermelon Remaster Studio: starts the local web app and opens it in the browser.
#   powershell -ExecutionPolicy Bypass -File tools\studio\studio.ps1 [-Port 8765] [-NoBrowser]
# Uses the HD pipeline's Python (tools\hd_remaster\.venv) when it exists; any Python 3.10+ works
# for the studio itself (the Library page can run the pipeline's setup from there).
param(
    [int]$Port = 0,            # default: the port in studio_settings.json (8765)
    [switch]$NoBrowser
)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$venv = Join-Path $here "..\hd_remaster\.venv\Scripts\python.exe"

if (Test-Path $venv) {
    $py = (Resolve-Path $venv).Path
} else {
    $found = Get-Command python -ErrorAction SilentlyContinue
    if (-not $found) {
        Write-Host "Python was not found. Install Python 3.10 or newer (python.org), then run this again." -ForegroundColor Red
        exit 1
    }
    $py = $found.Source
    Write-Host "The HD pipeline isn't set up yet; starting with $py. Use Setup on the Library page to install it."
}

$studioArgs = @((Join-Path $here "studio.py"))
if ($Port -gt 0) { $studioArgs += @("--port", "$Port") }
if ($NoBrowser) { $studioArgs += "--no-browser" }
& $py @studioArgs
exit $LASTEXITCODE
