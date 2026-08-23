$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir
$WorkerDir = Join-Path $ProjectDir "source\worker"
$VenvDir = Join-Path $WorkerDir ".venv"

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

function Find-Python {
    $candidates = @("python3.13", "python3.12", "python3.11", "python3.10", "python")
    foreach ($name in $candidates) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command) {
            try {
                $version = & $command.Source -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
                if ($version -match '^3\.(10|11|12|13)$') {
                    return $command.Source
                }
            } catch { }
        }
    }
    return $null
}

$python = Find-Python
if (-not $python) {
    Write-Host "[setup] Installing Python 3.10..."
    & $winget.Source install --id Python.Python.3.10 --exact --accept-source-agreements --accept-package-agreements
    $env:Path = "$env:LOCALAPPDATA\Programs\Python\Python310;$env:LOCALAPPDATA\Programs\Python\Python310\Scripts;$env:Path"
    $python = Find-Python
}
if (-not $python) {
    Fail "Python 3.10-3.13 was not found after installation. Open a new PowerShell and run install.ps1 again."
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

$venvPython = Join-Path $VenvDir "Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "[setup] Creating Python virtual environment..."
    & $python -m venv $VenvDir
}

Write-Host "[setup] Installing Python dependencies..."
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -r (Join-Path $WorkerDir "requirements.txt")

& (Join-Path $ScriptDir "build_exe.ps1")

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
