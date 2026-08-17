<#
.SYNOPSIS
Activates a project OpenUSD installation in the current PowerShell terminal.

.PARAMETER UsdRoot
OpenUSD install directory containing bin and lib.

.EXAMPLE
.\scripts\openusd_env.ps1 "C:\path\to\OpenUSDInstall"
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory, Position = 0)]
    [string] $UsdRoot,

    [string] $RenderManRoot,

    [string[]] $PluginPath = @(),

    [string[]] $DllDir = @()
)

$ErrorActionPreference = "Stop"

function Resolve-RequiredDirectory {
    param(
        [string] $Path,
        [string] $Label
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw "$Label does not exist: $Path"
    }
    return (Resolve-Path -LiteralPath $Path).Path.TrimEnd('\', '/')
}

function Add-EnvironmentPath {
    param(
        [string] $Name,
        [string[]] $Path
    )

    $values = @($Path | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Container) })
    $current = [Environment]::GetEnvironmentVariable($Name, "Process")
    if ($current) {
        $values += $current -split [IO.Path]::PathSeparator
    }

    $seen = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    $unique = foreach ($value in $values) {
        $resolved = if (Test-Path -LiteralPath $value -PathType Container) {
            (Resolve-Path -LiteralPath $value).Path.TrimEnd('\', '/')
        } else {
            $value
        }
        if ($seen.Add($resolved)) { $resolved }
    }
    if ($unique) {
        [Environment]::SetEnvironmentVariable(
            $Name,
            ($unique -join [IO.Path]::PathSeparator),
            "Process"
        )
    }
}

$usd = Resolve-RequiredDirectory $UsdRoot "OpenUSD root"
$usdPython = Join-Path $usd "lib\python"
if (-not (Test-Path -LiteralPath (Join-Path $usdPython "pxr\__init__.py") -PathType Leaf)) {
    throw "No pxr Python package found under OpenUSD install: $usd"
}

$plugins = foreach ($path in $PluginPath) {
    Resolve-RequiredDirectory $path "Plugin path"
}
$nativeDirs = foreach ($path in $DllDir) {
    Resolve-RequiredDirectory $path "Native-library directory"
}

$env:OPENUSDCONNECT_USD_ROOT = $usd
Add-EnvironmentPath "PYTHONPATH" @($usdPython)
Add-EnvironmentPath "PATH" @((Join-Path $usd "bin"), (Join-Path $usd "lib"))
Add-EnvironmentPath "PATH" @($nativeDirs)
Add-EnvironmentPath "PXR_PLUGINPATH_NAME" @($plugins)

if ($RenderManRoot) {
    $rman = Resolve-RequiredDirectory $RenderManRoot "RenderMan root"
    $env:RMANTREE = $rman
    Add-EnvironmentPath "PATH" @((Join-Path $rman "bin"), (Join-Path $rman "lib"))

    $rmanPlugins = Join-Path $rman "lib\plugins"
    $usdPlugin = Join-Path $usd "plugin\usd"
    $env:RMAN_SHADERPATH = @(
        (Join-Path $rman "lib\shaders")
        (Join-Path $usdPlugin "resources\shaders")
    ) -join [IO.Path]::PathSeparator
    $env:RMAN_RIXPLUGINPATH = $rmanPlugins
    $env:RMAN_TEXTUREPATH = @(
        (Join-Path $rman "lib\textures")
        $rmanPlugins
        $usdPlugin
    ) -join [IO.Path]::PathSeparator
    $env:RMAN_DISPLAYPATH = $rmanPlugins
    $env:RMAN_PROCEDURALPATH = $rmanPlugins
}

Write-Host "OpenUSD environment ready: $usd"
Write-Host "Python bindings: $usdPython"
if ($RenderManRoot) {
    Write-Host "RenderMan: $env:RMANTREE"
}
