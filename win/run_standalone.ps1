$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir
$WorkerDir = Join-Path $ProjectDir "source\worker"
$VenvPython = Join-Path $WorkerDir ".venv\Scripts\python.exe"
$PackagedApp = Join-Path $ScriptDir "dist\TranslateService\TranslateService.exe"

if (-not (Test-Path $VenvPython)) {
    Write-Error "Python environment not found. Run win\install.ps1 first."
    exit 1
}
if (-not (Get-Command ffmpeg.exe -ErrorAction SilentlyContinue)) {
    Write-Error "FFmpeg was not found in PATH. Run win\install.ps1 first."
    exit 1
}

$env:PYTHONPATH = $WorkerDir
$env:TRANSLATE_WORKER_PYTHON = $VenvPython
Set-Location $WorkerDir
if (Test-Path $PackagedApp) {
    & $PackagedApp
} else {
    & $VenvPython (Join-Path $WorkerDir "standalone_app.py")
}
exit $LASTEXITCODE
