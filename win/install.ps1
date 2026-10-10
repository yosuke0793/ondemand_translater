param([string]$PythonPath = "")

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir
$WorkerDir = Join-Path $ProjectDir "source\worker"
$VenvDir = Join-Path $WorkerDir ".venv"
. (Join-Path $ScriptDir "python_environment.ps1")

function Fail([string]$Message) {
    Write-Error $Message
    exit 1
}

if (-not [Environment]::Is64BitOperatingSystem) {
    Fail "64-bit Windows is required."
}
$buildNumber = [int](Get-ItemPropertyValue -Path "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion" -Name CurrentBuildNumber)
if ($buildNumber -lt 22000) {
    Fail "Windows 11 is required (build 22000 or later)."
}
if (-not (Test-Path $WorkerDir)) {
    Fail "Common worker directory was not found: $WorkerDir"
}

$winget = Get-Command winget.exe -ErrorAction SilentlyContinue
if (-not $winget) {
    Fail "winget was not found. Install App Installer from Microsoft Store, then run install.ps1 again."
}

$venvPython = Join-Path $VenvDir "Scripts\python.exe"
$existingVenv = $null
if (-not $PythonPath -and (Test-Path -LiteralPath $VenvDir)) {
    $existingVenv = Get-TranslatePythonInfo -Executable $venvPython -ExpectedVenv $VenvDir
}
if ($existingVenv) {
    Write-Host "[setup] Reusing validated application environment: $venvPython"
} else {
    $python = Find-TranslatePython -PythonPath $PythonPath
    if (-not $python) {
        foreach ($fallbackVersion in @("3.12", "3.10")) {
            Write-Host "[setup] Installing Python $fallbackVersion..."
            & $winget.Source install --id "Python.Python.$fallbackVersion" --exact --accept-source-agreements --accept-package-agreements
            $installExitCode = $LASTEXITCODE
            if ($installExitCode -ne 0) {
                Write-Warning "winget Python $fallbackVersion installation returned exit code $installExitCode. Checking installed interpreters before proceeding."
            }
            $python = Find-TranslatePython
            if ($python) {
                break
            }
        }
        if (-not $python) {
            Fail "No usable x64 Python 3.10-3.13 with Tkinter and venv support was found. See the [python] diagnostics above. Use install.ps1 -PythonPath 'C:\path\to\Python312\python.exe' to select an installed base interpreter. No existing Python was removed."
        }
    }
    Write-Host "[setup] Selected Python $($python.full_version): $($python.executable)"
    $venvPython = Initialize-TranslateVenv -VenvDir $VenvDir -BasePython $python
}

$ffmpeg = Get-Command ffmpeg.exe -ErrorAction SilentlyContinue
if (-not $ffmpeg) {
    Write-Host "[setup] Installing FFmpeg..."
    & $winget.Source install --id Gyan.FFmpeg.Shared --exact --accept-source-agreements --accept-package-agreements
    $env:Path = "$env:LOCALAPPDATA\Microsoft\WinGet\Packages;$env:Path"
    $ffmpeg = Get-Command ffmpeg.exe -ErrorAction SilentlyContinue
}
if (-not $ffmpeg) {
    Fail "FFmpeg was not found after installation. Open a new PowerShell and run install.ps1 again."
}

Write-Host "[setup] Installing Python dependencies..."
Invoke-TranslatePythonChecked -Executable $venvPython -Arguments @("-I", "-m", "pip", "install", "--upgrade", "pip") -Step "pip upgrade"
Invoke-TranslatePythonChecked -Executable $venvPython -Arguments @("-I", "-m", "pip", "install", "-r", (Join-Path $WorkerDir "requirements.txt")) -Step "Dependency installation"
$venvPython = Assert-TranslateVenv -VenvDir $VenvDir

& (Join-Path $ScriptDir "build_exe.ps1")
Invoke-TranslatePythonChecked -Executable $venvPython -Arguments @("-I", "-m", "pip", "check") -Step "Dependency verification"

$runScript = Join-Path $ScriptDir "run_standalone.ps1"
$shortcutPath = Join-Path ([Environment]::GetFolderPath("Desktop")) "Translate Service.lnk"
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = "powershell.exe"
$shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$runScript`""
$shortcut.WorkingDirectory = $WorkerDir
$shortcut.Description = "Translate Japanese MP4 video into English audio with embedded subtitles"
$shortcut.Save()

Write-Host ""
Write-Host "Installation complete. Desktop shortcut created: $shortcutPath"
Write-Host "Set GEMINI_API_KEY in the environment before launching the application."
