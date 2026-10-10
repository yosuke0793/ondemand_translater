$TranslatePythonVersions = @("3.12", "3.13", "3.11", "3.10")

function Invoke-TranslatePythonProcess {
    param([string]$Executable, [string[]]$Arguments, [switch]$StreamOutput)

    # Windows PowerShell turns native stderr into error records; inspect the exit code instead.
    $ErrorActionPreference = "Continue"
    try {
        $output = [Collections.Generic.List[string]]::new()
        $global:LASTEXITCODE = -1
        & $Executable @Arguments 2>&1 | ForEach-Object {
            $output.Add("$_")
            if ($StreamOutput) {
                Write-Host "$_"
            }
        }
        $exitCode = $global:LASTEXITCODE
        return [pscustomobject]@{
            ExitCode = $exitCode
            Output = $output -join "`n"
        }
    } catch {
        if ($StreamOutput) {
            Write-Host $_.Exception.Message
        }
        return [pscustomobject]@{ ExitCode = -1; Output = $_.Exception.Message }
    }
}

function Invoke-TranslatePythonChecked {
    param([string]$Executable, [string[]]$Arguments, [string]$Step)

    $result = Invoke-TranslatePythonProcess -Executable $Executable -Arguments $Arguments -StreamOutput
    if ($result.ExitCode -ne 0) {
        throw "$Step failed (exit code $($result.ExitCode)). Python: $Executable"
    }
}

function Get-TranslatePythonInfo {
    param([string]$Executable, [string]$ExpectedVenv = "")

    if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) {
        Write-Host "[python] Rejected '$Executable': executable not found."
        return $null
    }
    if ($Executable -match '[\\/]Microsoft[\\/]WindowsApps[\\/]') {
        Write-Host "[python] Rejected '$Executable': Windows app execution alias."
        return $null
    }
    $probe = @'
import importlib
import json
import platform
import struct
import sys

missing = {}
for name in ("tkinter", "venv", "ensurepip", "pip"):
    try:
        importlib.import_module(name)
    except (ImportError, OSError) as exc:
        missing[name] = str(exc)
print(json.dumps({
    "version": "{}.{}".format(*sys.version_info[:2]),
    "full_version": sys.version.split()[0],
    "bits": struct.calcsize("P") * 8,
    "machine": platform.machine(),
    "executable": sys.executable,
    "prefix": sys.prefix,
    "base_prefix": sys.base_prefix,
    "missing": missing,
}))
'@
    $result = Invoke-TranslatePythonProcess -Executable $Executable -Arguments @("-I", "-c", $probe)
    if ($result.ExitCode -ne 0) {
        Write-Host "[python] Rejected '$Executable': probe failed ($($result.ExitCode)): $($result.Output)"
        return $null
    }
    try {
        $info = $result.Output | ConvertFrom-Json -ErrorAction Stop
        $version = [string]$info.version
        $bits = [int]$info.bits
        $machine = [string]$info.machine
        $missing = $info.missing
        $prefix = [string]$info.prefix
        $basePrefix = [string]$info.base_prefix
        $actualExecutable = [string]$info.executable
    } catch {
        Write-Host "[python] Rejected '$Executable': invalid probe response: $($_.Exception.Message)"
        return $null
    }
    if ($TranslatePythonVersions -notcontains $version -or $bits -ne 64 -or $machine -notin @("AMD64", "x86_64")) {
        Write-Host "[python] Rejected '$Executable': Python $version, $bits-bit, $machine; requires x64 Python 3.10-3.13."
        return $null
    }
    $requiredModules = @("tkinter", "venv", "ensurepip")
    if ($ExpectedVenv) {
        $requiredModules += "pip"
    }
    foreach ($name in $requiredModules) {
        $failure = $missing.PSObject.Properties[$name]
        if ($failure) {
            Write-Host "[python] Rejected '$Executable': $name is unavailable: $($failure.Value)"
            return $null
        }
    }
    if ($ExpectedVenv) {
        $expectedPrefix = [IO.Path]::GetFullPath($ExpectedVenv).TrimEnd("\", "/")
        if ($prefix.TrimEnd("\", "/") -ine $expectedPrefix -or $prefix -ieq $basePrefix) {
            Write-Host "[python] Rejected '$Executable': not a virtual environment at '$ExpectedVenv'."
            return $null
        }
    }
    if (-not (Test-Path -LiteralPath $actualExecutable -PathType Leaf)) {
        Write-Host "[python] Rejected '$Executable': reported interpreter '$actualExecutable' does not exist."
        return $null
    }
    Write-Host "[python] Usable: Python $($info.full_version), $bits-bit, '$actualExecutable'"
    return $info
}

function Get-TranslateLauncherPythonPaths {
    $launchers = @(
        Get-Command py.exe -CommandType Application -All -ErrorAction SilentlyContinue |
            ForEach-Object { $_.Source }
        if ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA "Programs\Python\Launcher\py.exe" }
        if ($env:WINDIR) { Join-Path $env:WINDIR "py.exe" }
    )
    foreach ($launcher in ($launchers | Select-Object -Unique)) {
        if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
            continue
        }
        foreach ($version in $TranslatePythonVersions) {
            $result = Invoke-TranslatePythonProcess -Executable $launcher -Arguments @(
                "-$version", "-I", "-c", "import sys; print(sys.executable)"
            )
            if ($result.ExitCode -eq 0 -and $result.Output.Trim()) {
                $result.Output.Trim()
            } else {
                Write-Host "[python] Launcher '$launcher' could not resolve $version ($($result.ExitCode)): $($result.Output)"
            }
        }
    }
}

function Get-TranslateRegisteredPythonPaths {
    foreach ($root in @("HKCU:\Software\Python", "HKLM:\Software\Python", "HKLM:\Software\WOW6432Node\Python")) {
        if (-not (Test-Path -LiteralPath $root)) {
            continue
        }
        foreach ($company in Get-ChildItem -LiteralPath $root -ErrorAction Stop) {
            foreach ($tag in Get-ChildItem -LiteralPath $company.PSPath -ErrorAction Stop) {
                $installKeyPath = Join-Path $tag.PSPath "InstallPath"
                if (-not (Test-Path -LiteralPath $installKeyPath)) {
                    continue
                }
                $key = Get-Item -LiteralPath $installKeyPath -ErrorAction Stop
                $executable = $key.GetValue("ExecutablePath")
                if ($executable) {
                    [string]$executable
                }
                $directory = $key.GetValue("")
                if ($directory) {
                    Join-Path ([string]$directory) "python.exe"
                }
            }
        }
    }
}

function Get-TranslateStandardPythonPaths {
    $roots = @(
        if ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA "Programs\Python" }
        if ($env:ProgramFiles) { $env:ProgramFiles }
        if (${env:ProgramFiles(x86)}) { ${env:ProgramFiles(x86)} }
    )
    foreach ($root in $roots) {
        foreach ($version in $TranslatePythonVersions) {
            $tag = $version.Replace(".", "")
            Join-Path $root "Python$tag\python.exe"
        }
    }
}

function Get-TranslatePathPythonPaths {
    foreach ($name in @("python3.12.exe", "python3.13.exe", "python3.11.exe", "python3.10.exe", "python.exe", "python3.exe")) {
        Get-Command $name -CommandType Application -All -ErrorAction SilentlyContinue |
            ForEach-Object { $_.Source }
    }
}

function Find-TranslatePython {
    param([string]$PythonPath = "")

    if ($PythonPath) {
        $info = Get-TranslatePythonInfo -Executable $PythonPath
        if (-not $info -or $info.prefix -ine $info.base_prefix) {
            throw "The specified Python is not a usable base interpreter: '$PythonPath'. Specify an x64 Python 3.10-3.13 python.exe with Tkinter and venv support."
        }
        return $info
    }
    $paths = @(
        Get-TranslateLauncherPythonPaths
        Get-TranslateRegisteredPythonPaths
        Get-TranslateStandardPythonPaths
        Get-TranslatePathPythonPaths
    )
    $usable = @(
        foreach ($path in ($paths | Select-Object -Unique)) {
            if (Test-Path -LiteralPath $path -PathType Leaf) {
                $info = Get-TranslatePythonInfo -Executable $path
                if ($info -and $info.prefix -ieq $info.base_prefix) {
                    $info
                } elseif ($info) {
                    Write-Host "[python] Skipping '$path': virtual environment, not a base installation."
                }
            }
        }
    )
    if ($usable.Count -gt 0) {
        return $usable | Sort-Object { [Array]::IndexOf($TranslatePythonVersions, $_.version) } | Select-Object -First 1
    }
    return $null
}

function Initialize-TranslateVenv {
    param([string]$VenvDir, $BasePython)

    $venvPython = Join-Path $VenvDir "Scripts\python.exe"
    if (Test-Path -LiteralPath $VenvDir) {
        $existing = Get-TranslatePythonInfo -Executable $venvPython -ExpectedVenv $VenvDir
        if ($existing -and $existing.base_prefix -ieq $BasePython.prefix -and $existing.version -eq $BasePython.version) {
            return $venvPython
        }
        $backup = "$VenvDir.backup-$([DateTime]::Now.ToString('yyyyMMdd-HHmmss'))-$([Guid]::NewGuid().ToString('N').Substring(0, 8))"
        Write-Host "[setup] Preserving incompatible or broken environment: $backup"
        Move-Item -LiteralPath $VenvDir -Destination $backup -ErrorAction Stop
    }
    Write-Host "[setup] Creating virtual environment with '$($BasePython.executable)'"
    Invoke-TranslatePythonChecked -Executable $BasePython.executable -Arguments @("-I", "-m", "venv", $VenvDir) -Step "Virtual environment creation"
    $created = Get-TranslatePythonInfo -Executable $venvPython -ExpectedVenv $VenvDir
    if (-not $created -or $created.version -ne $BasePython.version -or $created.base_prefix -ine $BasePython.prefix) {
        throw "The new virtual environment failed validation: '$venvPython'. Installation stopped; rerun after fixing the reported error."
    }
    return $venvPython
}

function Assert-TranslateVenv {
    param([string]$VenvDir)

    $venvPython = Join-Path $VenvDir "Scripts\python.exe"
    if (-not (Get-TranslatePythonInfo -Executable $venvPython -ExpectedVenv $VenvDir)) {
        throw "The application Python environment is unusable. Close the application and rerun win\install.cmd. Environment: '$VenvDir'"
    }
    return $venvPython
}
