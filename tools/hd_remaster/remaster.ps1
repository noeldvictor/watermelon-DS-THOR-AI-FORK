# Remaster a DS game in one step: set up if needed, build the HD pack, optionally install it.
#   powershell -ExecutionPolicy Bypass -File tools\hd_remaster\remaster.ps1 game.nds [-Push] [-Scale 2]
# The game's recipe (games\<GAMECODE>\recipe.json) is applied automatically when there is one.
param(
    [Parameter(Mandatory = $true)][string]$Rom,
    [switch]$Push,             # install on the attached device (debuggable build) when done
    [int]$Scale = 0,           # 2 or 4; default is the recipe's (4 without one)
    [string]$Serial = ""       # adb serial; default is the attached AYN Thor
)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = Join-Path $here ".venv\Scripts\python.exe"
$tool = Join-Path $here "hd_remaster.py"

if (-not (Test-Path $Rom)) { throw "ROM not found: $Rom" }
if (-not (Test-Path $py)) {
    Write-Host "first run: setting up (PyTorch with CUDA, the upscale model)"
    & (Join-Path $here "setup.ps1")
}

# game code: 4 bytes at 0x0C of the ROM header
$bytes = [System.IO.File]::ReadAllBytes((Resolve-Path $Rom))
$code = [System.Text.Encoding]::ASCII.GetString($bytes, 0x0C, 4)
Write-Host "game $code"

$allArgs = @($tool, "all", $Rom)
if ($Scale -gt 0) { $allArgs += @("--scale", "$Scale") }
& $py @allArgs
if ($LASTEXITCODE -ne 0) { throw "remaster failed" }

$pack = Join-Path $here "packs\$code"
if ($Push) {
    $pushArgs = @($tool, "push", $pack)
    if ($Serial) { $pushArgs += @("--serial", $Serial) }
    & $py @pushArgs
    if ($LASTEXITCODE -ne 0) { throw "push failed" }
} else {
    Write-Host "pack ready: $pack"
    Write-Host "install it with: $py $tool push $pack"
}
