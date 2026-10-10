$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir
$WorkerDir = Join-Path $ProjectDir "source\worker"
. (Join-Path $ScriptDir "python_environment.ps1")
$VenvPython = Assert-TranslateVenv -VenvDir (Join-Path $WorkerDir ".venv")
$DistDir = Join-Path $ScriptDir "dist"
Set-Location $ProjectDir

Write-Host "[build] Installing/checking PyInstaller..."
Invoke-TranslatePythonChecked -Executable $VenvPython -Arguments @("-I", "-m", "pip", "install", "pyinstaller") -Step "PyInstaller installation"

Write-Host "[build] Building TranslateService.exe..."
& $VenvPython -I -m PyInstaller `
    --noconfirm `
    --clean `
    --windowed `
    --onedir `
    --name TranslateService `
    --distpath $DistDir `
    --workpath (Join-Path $ScriptDir "build") `
    --specpath $ScriptDir `
    --paths $WorkerDir `
    --hidden-import local_job_manager `
    (Join-Path $WorkerDir "standalone_app.py")
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed."
}

$builtExe = Join-Path $DistDir "TranslateService\TranslateService.exe"
if (-not (Test-Path $builtExe)) {
    throw "Build completed but executable was not found: $builtExe"
}
Write-Host "Built: $builtExe"
