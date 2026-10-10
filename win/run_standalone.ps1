$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir
$WorkerDir = Join-Path $ProjectDir "source\worker"
. (Join-Path $ScriptDir "python_environment.ps1")
$VenvPython = Assert-TranslateVenv -VenvDir (Join-Path $WorkerDir ".venv")
$PackagedApp = Join-Path $ScriptDir "dist\TranslateService\TranslateService.exe"

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
