$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir
$WorkerDir = Join-Path $ProjectDir "source\worker"
$VenvPython = Join-Path $WorkerDir ".venv\Scripts\python.exe"
$DistDir = Join-Path $ScriptDir "dist"
Set-Location $ProjectDir

if (-not (Test-Path $VenvPython)) {
    throw "Python environment not found: $VenvPython. Run win\install.ps1 first."
}

Write-Host "[build] Installing/checking PyInstaller..."
& $VenvPython -m pip install pyinstaller
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller installation failed."
}

Write-Host "[build] Building TranslateService.exe..."
& $VenvPython -m PyInstaller `
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
