# One-time setup for HD remastering on Windows with an NVIDIA GPU.
#   powershell -ExecutionPolicy Bypass -File tools\hd_remaster\setup.ps1
# Creates tools\hd_remaster\.venv, installs CUDA PyTorch + spandrel, downloads 4x-UltraSharp.
param(
    [string]$Cuda = "cu130",     # PyTorch CUDA build; cu128 for older drivers
    [string]$Python = "python"
)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$venv = Join-Path $here ".venv"
$py = Join-Path $venv "Scripts\python.exe"

if (-not (Test-Path $py)) {
    Write-Host "creating $venv"
    & $Python -m venv $venv
}
& $py -m pip install --upgrade pip

# torch and torchvision must come from the CUDA index together, before spandrel: installing
# spandrel first pulls a CPU-only torch from PyPI that then shadows the CUDA build.
Write-Host "installing PyTorch ($Cuda)"
& $py -m pip install torch torchvision --index-url "https://download.pytorch.org/whl/$Cuda"
& $py -m pip install -r (Join-Path $here "requirements.txt")

$models = Join-Path $here "models"
New-Item -ItemType Directory -Force $models | Out-Null
$model = Join-Path $models "4x-UltraSharp.safetensors"
if (-not (Test-Path $model)) {
    Write-Host "downloading 4x-UltraSharp (Kim2091, CC BY-NC-SA 4.0: personal, non-commercial use)"
    Invoke-WebRequest "https://huggingface.co/Kim2091/UltraSharp/resolve/main/4x-UltraSharp.safetensors" -OutFile $model
}

& $py -c "import torch, spandrel; print('torch', torch.__version__, '| CUDA', torch.cuda.is_available(), '|', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'no GPU found: upscaling will run on the CPU and be very slow')"
Write-Host "ready: $py tools\hd_remaster\hd_remaster.py all <rom.nds>"
