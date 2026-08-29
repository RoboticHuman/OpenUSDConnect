<#
.SYNOPSIS
Activates an OpenUSD runtime in the current PowerShell process.

.PARAMETER UsdRoot
OpenUSD install prefix. Omit this for the active project-managed build.

.PARAMETER PythonPath
Directory containing pxr when the bindings are outside UsdRoot.

.PARAMETER PythonExecutable
Matching Python executable to place first on PATH. By default, the script uses
the repository's .venv when present, then falls back to Python on PATH.

.EXAMPLE
.\scripts\openusd_env.ps1

.EXAMPLE
.\scripts\openusd_env.ps1 "D:\OpenUSDInstall"

.EXAMPLE
.\scripts\openusd_env.ps1 "E:\OpenUSDInstall" `
    -PythonExecutable "E:\OpenUSD-venv\Scripts\python.exe"
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string] $UsdRoot,

    [string] $PythonPath,

    [string] $PythonExecutable,

    [string] $RenderManRoot,

    [string[]] $PluginPath = @(),

    [string[]] $DllDir = @()
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot

if ($UsdRoot) {
    if (-not (Test-Path -LiteralPath $UsdRoot -PathType Container)) {
        throw "OpenUSD install prefix does not exist: $UsdRoot"
    }
    $resolvedUsdRoot = (Resolve-Path -LiteralPath $UsdRoot).Path
    $usdBin = Join-Path $resolvedUsdRoot "bin"
    if (-not (Test-Path -LiteralPath $usdBin -PathType Container)) {
        throw (
            "OpenUSD install prefix has no bin directory: $resolvedUsdRoot`n" +
            "For the project-managed build, omit UsdRoot: .\scripts\openusd_env.ps1"
        )
    }
    $UsdRoot = $resolvedUsdRoot
}

if ($PythonExecutable) {
    if (-not (Test-Path -LiteralPath $PythonExecutable -PathType Leaf)) {
        throw "Python executable does not exist: $PythonExecutable"
    }
    $python = (Resolve-Path -LiteralPath $PythonExecutable).Path
} else {
    $repoPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $repoPython -PathType Leaf) {
        $python = (Resolve-Path -LiteralPath $repoPython).Path
    } else {
        $pythonCommand = Get-Command python -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if (-not $pythonCommand) {
            $pythonCommand = Get-Command python3 -CommandType Application -ErrorAction SilentlyContinue |
                Select-Object -First 1
        }
        if (-not $pythonCommand) {
            throw "Python was not found. Create .venv or pass -PythonExecutable."
        }
        $python = $pythonCommand.Source
    }
}

$resolver = Join-Path $PSScriptRoot "openusd_runtime.py"
$arguments = @(
    $resolver
    "--python-executable"
    $python
    "--format"
    "json"
)
if ($UsdRoot) {
    $arguments += @("--usd-root", $UsdRoot)
} else {
    $arguments += "--managed"
}
if ($PythonPath) {
    $arguments += @("--python-path", $PythonPath)
}
if ($RenderManRoot) {
    $arguments += @("--renderman-root", $RenderManRoot)
}
foreach ($path in $PluginPath) {
    $arguments += @("--plugin-path", $path)
}
foreach ($path in $DllDir) {
    $arguments += @("--dll-dir", $path)
}

# Relax the Stop preference so the resolver's own stderr prints as written,
# instead of being wrapped in a NativeCommandError with a PowerShell stack trace.
$previousErrorAction = $ErrorActionPreference
$ErrorActionPreference = "Continue"
$json = & $python @arguments
$resolverExitCode = $LASTEXITCODE
$ErrorActionPreference = $previousErrorAction
if ($resolverExitCode -ne 0) {
    return
}
$configuration = $json | ConvertFrom-Json
foreach ($property in $configuration.environment.PSObject.Properties) {
    [Environment]::SetEnvironmentVariable(
        $property.Name,
        [string] $property.Value,
        "Process"
    )
}

Write-Host "OpenUSD environment ready: $($configuration.usd_root)"
Write-Host "Python bindings: $($configuration.python_path)"
Write-Host "Python executable (uv environment): $python"
$usdviewCommand = Get-Command usdview.cmd, usdview.exe, usdview `
    -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
if ($usdviewCommand) {
    Write-Host "usdview executable: $($usdviewCommand.Source)"
}
if ($configuration.renderman_root) {
    Write-Host "RenderMan: $($configuration.renderman_root)"
}
