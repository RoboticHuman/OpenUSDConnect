<#
.SYNOPSIS
Activates an OpenUSD runtime in the current PowerShell process.

.PARAMETER UsdRoot
OpenUSD install prefix.

.PARAMETER PythonPath
Directory containing pxr when the bindings are outside UsdRoot.

.PARAMETER PythonExecutable
Matching Python executable to place first on PATH. The active Python is used by default.

.EXAMPLE
. .\scripts\openusd_env.ps1 "D:\OpenUSDInstall"

.EXAMPLE
. .\scripts\openusd_env.ps1 "D:\OpenUSDInstall" `
    -PythonExecutable "D:\OpenUSD\.venv\Scripts\python.exe"
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory, Position = 0)]
    [string] $UsdRoot,

    [string] $PythonPath,

    [string] $PythonExecutable,

    [string] $RenderManRoot,

    [string[]] $PluginPath = @(),

    [string[]] $DllDir = @()
)

$ErrorActionPreference = "Stop"

if ($PythonExecutable) {
    if (-not (Test-Path -LiteralPath $PythonExecutable -PathType Leaf)) {
        throw "Python executable does not exist: $PythonExecutable"
    }
    $python = (Resolve-Path -LiteralPath $PythonExecutable).Path
} else {
    $pythonCommand = Get-Command python -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $pythonCommand) {
        $pythonCommand = Get-Command python3 -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
    }
    if (-not $pythonCommand) {
        throw "Python was not found. Activate the matching venv or pass -PythonExecutable."
    }
    $python = $pythonCommand.Source
}

$resolver = Join-Path $PSScriptRoot "openusd_runtime.py"
$arguments = @(
    $resolver
    "--usd-root"
    $UsdRoot
    "--python-executable"
    $python
    "--format"
    "json"
)
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

$json = & $python @arguments
if ($LASTEXITCODE -ne 0) {
    throw "OpenUSD environment resolution failed with exit code $LASTEXITCODE."
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
Write-Host "Python executable: $python"
if ($configuration.renderman_root) {
    Write-Host "RenderMan: $($configuration.renderman_root)"
}
