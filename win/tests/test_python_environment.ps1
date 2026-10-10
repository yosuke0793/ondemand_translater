$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$WinDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
. (Join-Path $WinDir "python_environment.ps1")
$NativeProcess = ${function:Invoke-TranslatePythonProcess}
$TestRoot = Join-Path ([IO.Path]::GetTempPath()) ("translate-python-tests-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $TestRoot | Out-Null
$TestCount = 0

function Assert-Equal($Actual, $Expected) {
    if ($Actual -ne $Expected) {
        throw "Expected '$Expected', received '$Actual'."
    }
}

function Assert-Throws([scriptblock]$Action, [string]$Message) {
    try {
        & $Action | Out-Null
    } catch {
        if ($_.Exception.Message -notlike "*$Message*") {
            throw "Expected an error containing '$Message', received '$($_.Exception.Message)'."
        }
        return
    }
    throw "Expected an error containing '$Message', but the operation succeeded."
}

function New-TestPython {
    param([string]$Directory, [string]$Version = "3.12", [int]$Bits = 64, [string]$Machine = "AMD64", [string]$BasePrefix = "")

    New-Item -ItemType Directory -Path $Directory -Force | Out-Null
    $executable = Join-Path $Directory "python.exe"
    Set-Content -LiteralPath $executable -Value "test interpreter fixture"
    if (-not $BasePrefix) {
        $BasePrefix = $Directory
    }
    $info = [pscustomobject]@{
        version = $Version
        full_version = "$Version.10"
        bits = $Bits
        machine = $Machine
        executable = $executable
        prefix = $Directory
        base_prefix = $BasePrefix
        missing = @{}
    }
    $script:ProbeResults[$executable] = [pscustomobject]@{
        ExitCode = 0
        Output = ($info | ConvertTo-Json -Depth 4 -Compress)
    }
    return $info
}

function Set-TestProbe($Info) {
    $script:ProbeResults[$Info.executable].Output = $Info | ConvertTo-Json -Depth 4 -Compress
}

function Invoke-TranslatePythonProcess {
    param([string]$Executable, [string[]]$Arguments, [switch]$StreamOutput)

    $script:Calls.Add([pscustomobject]@{ Executable = $Executable; Arguments = $Arguments })
    if ($script:ProcessHandler) {
        return & $script:ProcessHandler $Executable $Arguments
    }
    if (-not $script:ProbeResults.ContainsKey($Executable)) {
        throw "Unexpected test command: $Executable"
    }
    return $script:ProbeResults[$Executable]
}

function Invoke-Test([string]$Name, [scriptblock]$Action) {
    $script:ProbeResults = @{}
    $script:Calls = [Collections.Generic.List[object]]::new()
    $script:ProcessHandler = $null
    & $Action
    $script:TestCount++
    Write-Host "PASS: $Name"
}

try {
    foreach ($file in Get-ChildItem -LiteralPath $WinDir -Filter "*.ps1") {
        $tokens = $null
        $errors = $null
        [Management.Automation.Language.Parser]::ParseFile($file.FullName, [ref]$tokens, [ref]$errors) | Out-Null
        if ($errors) {
            throw "PowerShell parse failed: $($file.Name): $errors"
        }
    }

    Invoke-Test "Mixed versions: launcher resolves 3.12 despite PATH Python 3.8 and 3.14" {
        $script:old = New-TestPython (Join-Path $TestRoot "old") "3.8"
        $script:new = New-TestPython (Join-Path $TestRoot "new") "3.14"
        $script:ten = New-TestPython (Join-Path $TestRoot "ten") "3.10"
        $script:twelve = New-TestPython (Join-Path $TestRoot "twelve")
        function Get-TranslateLauncherPythonPaths { $script:twelve.executable }
        function Get-TranslateRegisteredPythonPaths { $script:ten.executable }
        function Get-TranslateStandardPythonPaths { }
        function Get-TranslatePathPythonPaths { $script:old.executable; $script:new.executable }
        Assert-Equal (Find-TranslatePython).executable $script:twelve.executable
    }

    Invoke-Test "Registry discovery works without launcher or compatible PATH command" {
        $script:registered = New-TestPython (Join-Path $TestRoot "registered")
        function Get-TranslateLauncherPythonPaths { }
        function Get-TranslateRegisteredPythonPaths { $script:registered.executable }
        function Get-TranslateStandardPythonPaths { }
        function Get-TranslatePathPythonPaths { }
        Assert-Equal (Find-TranslatePython).executable $script:registered.executable
    }

    Invoke-Test "Standard installation discovery works without registry or launcher" {
        $script:standard = New-TestPython (Join-Path $TestRoot "standard")
        function Get-TranslateLauncherPythonPaths { }
        function Get-TranslateRegisteredPythonPaths { }
        function Get-TranslateStandardPythonPaths { $script:standard.executable }
        function Get-TranslatePathPythonPaths { }
        Assert-Equal (Find-TranslatePython).executable $script:standard.executable
    }

    Invoke-Test "PATH discovery considers all executables, not only the first python" {
        $script:pathOld = New-TestPython (Join-Path $TestRoot "path-old") "3.8"
        $script:pathGood = New-TestPython (Join-Path $TestRoot "path-good") "3.11"
        function Get-TranslateLauncherPythonPaths { }
        function Get-TranslateRegisteredPythonPaths { }
        function Get-TranslateStandardPythonPaths { }
        function Get-TranslatePathPythonPaths { $script:pathOld.executable; $script:pathGood.executable }
        Assert-Equal (Find-TranslatePython).executable $script:pathGood.executable
    }

    Invoke-Test "Launcher queries explicit versions and uses the reported executable" {
        $script:launcher = New-TestPython (Join-Path $TestRoot "launcher")
        function Get-Command { [pscustomobject]@{ Source = $script:launcher.executable } }
        $script:ProcessHandler = {
            param($executable, $arguments)
            Assert-Equal $executable $script:launcher.executable
            if ($arguments[0] -eq "-3.12") {
                return [pscustomobject]@{ ExitCode = 0; Output = "C:\Python 3.12\python.exe" }
            }
            return [pscustomobject]@{ ExitCode = 1; Output = "not installed" }
        }
        $paths = @(Get-TranslateLauncherPythonPaths)
        Assert-Equal $paths.Count 1
        Assert-Equal $paths[0] "C:\Python 3.12\python.exe"
        Assert-Equal $script:Calls.Count 4
        Assert-Equal $script:Calls[0].Arguments[0] "-3.12"
    }

    Invoke-Test "PEP 514 registry discovery handles executable and default directory values" {
        $script:registryDirectory = Join-Path $TestRoot "registered directory"
        function Test-Path { $true }
        function Get-ChildItem {
            param($LiteralPath)
            if ($LiteralPath -match '^HK(CU|LM):') {
                [pscustomobject]@{ PSPath = "fixture-company" }
            } else {
                [pscustomobject]@{ PSPath = "fixture-tag" }
            }
        }
        function Get-Item {
            $key = [pscustomobject]@{}
            $key | Add-Member -MemberType ScriptMethod -Name GetValue -Value {
                param($name)
                if ($name -eq "ExecutablePath") { return (Join-Path $script:registryDirectory "python.exe") }
                return $script:registryDirectory
            }
            return $key
        }
        $paths = @(Get-TranslateRegisteredPythonPaths)
        Assert-Equal $paths.Count 6
        Assert-Equal $paths[0] (Join-Path $script:registryDirectory "python.exe")
        Assert-Equal $paths[1] $paths[0]
    }

    Invoke-Test "Standard paths cover user and system installs with 3.12 first" {
        $oldLocal = $env:LOCALAPPDATA
        $oldProgram = $env:ProgramFiles
        $oldProgram32 = ${env:ProgramFiles(x86)}
        try {
            $env:LOCALAPPDATA = Join-Path $TestRoot "local"
            $env:ProgramFiles = Join-Path $TestRoot "program"
            ${env:ProgramFiles(x86)} = Join-Path $TestRoot "program32"
            $paths = @(Get-TranslateStandardPythonPaths)
            Assert-Equal $paths.Count 12
            Assert-Equal ($paths[0] -match 'Python312[\\/]python.exe$') $true
            Assert-Equal ($paths[4] -match 'program[\\/]Python312[\\/]python.exe$') $true
        } finally {
            $env:LOCALAPPDATA = $oldLocal
            $env:ProgramFiles = $oldProgram
            ${env:ProgramFiles(x86)} = $oldProgram32
        }
    }

    Invoke-Test "Windows application aliases are skipped without invoking the Store stub" {
        $alias = New-TestPython (Join-Path $TestRoot "Microsoft/WindowsApps")
        Assert-Equal (Get-TranslatePythonInfo $alias.executable) $null
        Assert-Equal $script:Calls.Count 0
    }

    Invoke-Test "Explicit path with spaces is used as one argument" {
        $info = New-TestPython (Join-Path $TestRoot "explicit path with spaces")
        Assert-Equal (Find-TranslatePython -PythonPath $info.executable).executable $info.executable
        Assert-Equal $script:Calls[0].Executable $info.executable
        Assert-Equal $script:Calls[0].Arguments[0] "-I"
    }

    Invoke-Test "Unsupported explicit interpreter fails instead of silently falling back" {
        $info = New-TestPython (Join-Path $TestRoot "unsupported") "3.14"
        Assert-Throws { Find-TranslatePython -PythonPath $info.executable } "not a usable base interpreter"
    }

    Invoke-Test "32-bit and ARM64 interpreters are rejected" {
        $x86 = New-TestPython (Join-Path $TestRoot "x86") "3.12" 32 "x86"
        $arm = New-TestPython (Join-Path $TestRoot "arm") "3.12" 64 "ARM64"
        Assert-Equal (Get-TranslatePythonInfo $x86.executable) $null
        Assert-Equal (Get-TranslatePythonInfo $arm.executable) $null
    }

    Invoke-Test "Missing Tkinter, venv and ensurepip are rejected with diagnostics" {
        foreach ($module in @("tkinter", "venv", "ensurepip")) {
            $info = New-TestPython (Join-Path $TestRoot "missing-$module")
            $info.missing[$module] = "missing test module"
            Set-TestProbe $info
            Assert-Equal (Get-TranslatePythonInfo $info.executable) $null
        }
    }

    Invoke-Test "Broken interpreter and malformed metadata are rejected" {
        $info = New-TestPython (Join-Path $TestRoot "broken")
        $script:ProbeResults[$info.executable] = [pscustomobject]@{ ExitCode = 1; Output = "DLL load failed" }
        Assert-Equal (Get-TranslatePythonInfo $info.executable) $null
        $script:ProbeResults[$info.executable] = [pscustomobject]@{ ExitCode = 0; Output = "not JSON" }
        Assert-Equal (Get-TranslatePythonInfo $info.executable) $null
    }

    Invoke-Test "Valid matching virtual environment is preserved without recreation" {
        $base = New-TestPython (Join-Path $TestRoot "reuse-base")
        $venvDir = Join-Path $TestRoot "reuse-venv"
        $info = New-TestPython (Join-Path $venvDir "Scripts") -BasePrefix $base.prefix
        $info.prefix = $venvDir
        Set-TestProbe $info
        Assert-Equal (Initialize-TranslateVenv $venvDir $base) $info.executable
        Assert-Equal (Assert-TranslateVenv $venvDir) $info.executable
        Assert-Equal @(Get-ChildItem -Path "$venvDir.backup-*").Count 0
    }

    Invoke-Test "Missing pip invalidates an otherwise supported virtual environment" {
        $venvDir = Join-Path $TestRoot "no-pip-venv"
        $info = New-TestPython (Join-Path $venvDir "Scripts") -BasePrefix (Join-Path $TestRoot "base")
        $info.prefix = $venvDir
        $info.missing["pip"] = "pip unavailable"
        Set-TestProbe $info
        Assert-Throws { Assert-TranslateVenv $venvDir } "rerun win\install.cmd"
    }

    Invoke-Test "An environment copied from another location is rejected" {
        $venvDir = Join-Path $TestRoot "copied-venv"
        $info = New-TestPython (Join-Path $venvDir "Scripts") -BasePrefix (Join-Path $TestRoot "base")
        $info.prefix = Join-Path $TestRoot "different-location"
        Set-TestProbe $info
        Assert-Equal (Get-TranslatePythonInfo $info.executable $venvDir) $null
    }

    Invoke-Test "Invalid environment is backed up intact before creating a new one" {
        $script:base = New-TestPython (Join-Path $TestRoot "repair-base")
        $script:venvDir = Join-Path $TestRoot "repair-venv"
        $old = New-TestPython (Join-Path $script:venvDir "Scripts") "3.14" -BasePrefix $script:base.prefix
        $old.prefix = $script:venvDir
        Set-TestProbe $old
        Set-Content (Join-Path $script:venvDir "keep.txt") "original environment"
        $script:ProcessHandler = {
            param($executable, $arguments)
            if ($arguments[1] -eq "-m" -and $arguments[2] -eq "venv") {
                $created = New-TestPython (Join-Path $script:venvDir "Scripts") -BasePrefix $script:base.prefix
                $created.prefix = $script:venvDir
                Set-TestProbe $created
                return [pscustomobject]@{ ExitCode = 0; Output = "" }
            }
            return $script:ProbeResults[$executable]
        }
        Assert-Equal (Initialize-TranslateVenv $script:venvDir $script:base) (Join-Path $script:venvDir "Scripts\python.exe")
        $backup = @(Get-ChildItem -Path "$script:venvDir.backup-*")
        Assert-Equal $backup.Count 1
        Assert-Equal (Get-Content (Join-Path $backup[0].FullName "keep.txt")) "original environment"
        Assert-Equal (Test-Path (Join-Path $script:venvDir "keep.txt")) $false
    }

    Invoke-Test "A valid environment from a different selected base is preserved and rebuilt" {
        $base = New-TestPython (Join-Path $TestRoot "switch-base")
        $venvDir = Join-Path $TestRoot "switch-venv"
        $old = New-TestPython (Join-Path $venvDir "Scripts") "3.10" -BasePrefix (Join-Path $TestRoot "other-base")
        $old.prefix = $venvDir
        Set-TestProbe $old
        $script:ProcessHandler = {
            param($executable, $arguments)
            if ($arguments[1] -eq "-m") { return [pscustomobject]@{ ExitCode = 1; Output = "creation failed" } }
            return $script:ProbeResults[$executable]
        }
        Assert-Throws { Initialize-TranslateVenv $venvDir $base } "Virtual environment creation failed"
        Assert-Equal @(Get-ChildItem -Path "$venvDir.backup-*").Count 1
    }

    Invoke-Test "Nonzero pip and dependency checks abort installation" {
        $script:ProcessHandler = { [pscustomobject]@{ ExitCode = 7; Output = "pip failure" } }
        foreach ($step in @("pip upgrade", "Dependency installation", "Dependency verification")) {
            Assert-Throws { Invoke-TranslatePythonChecked "fixture.exe" @("-I", "-m", "pip", "check") $step } "$step failed"
        }
    }

    Invoke-Test "Creation success without a working virtual environment is not accepted" {
        $base = New-TestPython (Join-Path $TestRoot "no-result-base")
        $venvDir = Join-Path $TestRoot "no-result-venv"
        $script:ProcessHandler = { [pscustomobject]@{ ExitCode = 0; Output = "" } }
        Assert-Throws { Initialize-TranslateVenv $venvDir $base } "new virtual environment failed validation"
    }

    Invoke-Test "No supported interpreter returns no selection" {
        function Get-TranslateLauncherPythonPaths { }
        function Get-TranslateRegisteredPythonPaths { }
        function Get-TranslateStandardPythonPaths { }
        function Get-TranslatePathPythonPaths { }
        Assert-Equal (Find-TranslatePython) $null
    }

    Invoke-Test "Native process adapter captures stderr and the real exit code" {
        if ($env:OS -eq "Windows_NT") {
            $result = & $NativeProcess "$env:WINDIR\System32\cmd.exe" @("/d", "/c", "echo test-error 1>&2 & exit /b 9")
        } else {
            $result = & $NativeProcess "/bin/sh" @("-c", "printf test-error >&2; exit 9")
        }
        Assert-Equal $result.ExitCode 9
        Assert-Equal ($result.Output -match "test-error") $true
    }

    Invoke-Test "Native invocation failures cannot inherit a prior successful exit code" {
        $global:LASTEXITCODE = 0
        $result = & $NativeProcess (Join-Path $TestRoot "not-installed.exe") @("-c", "print('test')")
        Assert-Equal ($result.ExitCode -ne 0) $true
        Assert-Equal ([string]::IsNullOrWhiteSpace($result.Output)) $false
    }

    Write-Host "All $TestCount Python environment tests passed."
} finally {
    Remove-Item -LiteralPath $TestRoot -Recurse -Force
}
