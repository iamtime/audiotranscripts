# Set up voice-rec on Windows: a Python 3.12 virtual env in .venv, PyTorch (CUDA build when
# an NVIDIA GPU is present, otherwise CPU) and the dependencies. Then start it with webui.bat.
#
#   powershell -ExecutionPolicy Bypass -File setup_windows.ps1          # auto: GPU if available
#   powershell -ExecutionPolicy Bypass -File setup_windows.ps1 -Cpu     # CPU only (smaller)
param([switch]$Cpu)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

function Run($exe, [string[]]$argv) {
    & $exe @argv
    if ($LASTEXITCODE -ne 0) { throw "Failed: $exe $($argv -join ' ')" }
}

# torch, ctranslate2 and friends publish wheels for 3.10-3.12 first; 3.12 is the tested one.
$py = $null
try { $py = (& py -3.12 -c "import sys; print(sys.executable)" 2>$null) } catch { }
if (-not $py) {
    throw "Python 3.12 is required: https://www.python.org/downloads/ or: winget install Python.Python.3.12"
}
if (-not (Test-Path .venv\Scripts\python.exe)) {
    Write-Host "Creating .venv with $py"
    Run $py @('-m', 'venv', '.venv')
}
$venv = (Resolve-Path .venv\Scripts\python.exe).Path
Run $venv @('-m', 'pip', 'install', '--upgrade', 'pip')

$gpu = (-not $Cpu) -and [bool](Get-Command nvidia-smi -ErrorAction SilentlyContinue)
$index = if ($gpu) { 'https://download.pytorch.org/whl/cu128' } else { 'https://download.pytorch.org/whl/cpu' }
Write-Host ("Installing PyTorch ({0})" -f $(if ($gpu) { 'CUDA 12.8' } else { 'CPU' }))
Run $venv @('-m', 'pip', 'install', 'torch', 'torchaudio', 'torchvision', '--index-url', $index)

Write-Host "Installing the dependencies"
Run $venv @('-m', 'pip', 'install', '-r', 'packaging\requirements-windows.txt')
if ($gpu) {
    # faster-whisper (CTranslate2) on the GPU needs cuBLAS and cuDNN 9 for CUDA 12.
    Run $venv @('-m', 'pip', 'install', '-r', 'packaging\requirements-windows-cuda.txt')
}

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Host "ffmpeg is not on PATH: the bundled one from imageio-ffmpeg is used."
}
Write-Host ""
Write-Host "Done. Start the web UI with:  .\webui.bat"
