param(
    [string]$Python312 = "python",
    [string]$ExpectedTag = ""
)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
Set-Location (Split-Path $PSScriptRoot -Parent)
function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed ($LASTEXITCODE): $Program $Arguments" }
}
Invoke-Checked $Python312 @("-c", "import sys,platform; assert sys.version_info[:2] == (3,12) and platform.machine().lower() in ('amd64','x86_64'), 'CPython 3.12 x64 required'")
New-Item -ItemType Directory -Force "packaging/.work/tool-tmp" | Out-Null
$env:TEMP = Join-Path (Get-Location) "packaging/.work/tool-tmp"
$env:TMP = $env:TEMP
$env:PIP_CACHE_DIR = Join-Path (Get-Location) "packaging/.work/pip-cache"
$env:PIP_DISABLE_PIP_VERSION_CHECK = "1"
Remove-Item Env:PYTHONHOME, Env:PYTHONPATH -ErrorAction SilentlyContinue
if (Test-Path "packaging/.venv-build") { Remove-Item -Recurse -Force "packaging/.venv-build" }
Invoke-Checked $Python312 @("-m", "venv", "packaging/.venv-build")
$Python = Join-Path (Get-Location) "packaging/.venv-build/Scripts/python.exe"
Invoke-Checked $Python @("-m", "pip", "install", "--only-binary=:all:", "--require-hashes", "-r", "packaging/locks/bootstrap.txt")
Invoke-Checked $Python @("-m", "pip", "install", "--only-binary=:all:", "--no-binary=sacremoses", "--no-build-isolation", "--require-hashes", "-r", "packaging/locks/windows-x64.txt")
Invoke-Checked $Python @("-m", "pip", "check")
$Arguments = @("packaging/build.py", "build", "--target", "windows-x64")
if ($ExpectedTag) { $Arguments += @("--expected-tag", $ExpectedTag) }
Invoke-Checked $Python $Arguments
