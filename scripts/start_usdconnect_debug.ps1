param(
    [int]$ServerPort = 7200,
    [string]$ServerHost = "127.0.0.1",
    [switch]$TwoBlenders,
    [switch]$StartEmitter,
    [switch]$StartReceiver,
    [int]$DebugPort = 5678,
    [int]$DebugPortB = 5679,
    [switch]$WaitForDebugger,
    [switch]$Reload,
    [string]$BlenderExe = "",
    [string]$BaseUsd = "",
    [string]$LogPath = ""
)

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

if (-not $BaseUsd) {
    $BaseUsd = Join-Path $RepoRoot "test_scene.usda"
}
if (-not $LogPath) {
    $LogPath = Join-Path $RepoRoot "usd_events.db"
}

$AddonZip = Join-Path $RepoRoot "dist\usd_connect_blender.zip"
$BootstrapScript = Join-Path $RepoRoot "scripts\blender_bootstrap_instance.py"
$BlenderCfg = Join-Path $RepoRoot "blender.test.cfg"

if (-not $BlenderExe) {
    if (Test-Path $BlenderCfg) {
        $BlenderExe = (Get-Content $BlenderCfg | Where-Object { $_.Trim() -ne "" } | Select-Object -First 1).Trim()
    }
}

# --- Reload mode: build addon and signal running Blender instances, then exit ---
if ($Reload) {
    Write-Host '[launcher] Building addon...'
    & uv run python scripts/build_blender_addon.py
    if (-not (Test-Path $AddonZip)) {
        throw "Addon build failed: $AddonZip not found"
    }
    $triggerFile = Join-Path $RepoRoot ".reload_addon"
    Set-Content -Path $triggerFile -Value $AddonZip -Encoding UTF8
    Write-Host '[launcher] Addon built and reload triggered. Running Blender instances will pick it up within ~2s.'
    exit 0
}

if (-not $BlenderExe) {
    throw "Blender executable not provided and blender.test.cfg is empty/missing."
}
if (-not (Test-Path $BlenderExe)) {
    throw "Blender executable not found: $BlenderExe"
}
if (-not (Test-Path $BootstrapScript)) {
    throw "Bootstrap script not found: $BootstrapScript"
}
if (-not (Test-Path $BaseUsd)) {
    throw "Base USD file not found: $BaseUsd"
}

if (-not (Test-Path $AddonZip)) {
    Write-Host '[launcher] Addon zip missing. Building addon...'
    & uv run python scripts/build_blender_addon.py
}
if (-not (Test-Path $AddonZip)) {
    throw "Addon zip still missing after build: $AddonZip"
}

$VenvPython = (& uv python find 2>$null)
if (-not $VenvPython -or -not (Test-Path $VenvPython)) {
    throw "Could not find Python via uv. Run 'uv sync' first."
}

Write-Host "[launcher] Starting server on ${ServerHost}:${ServerPort} ..."
$serverArgs = @(
    "-m", "openusdconnect.server",
    "--port", $ServerPort,
    "--base", $BaseUsd,
    "--log", $LogPath
)
$ServerProc = Start-Process -FilePath $VenvPython -ArgumentList $serverArgs -WorkingDirectory $RepoRoot -NoNewWindow -PassThru

Start-Sleep -Seconds 1

function Start-BlenderInstance {
    param(
        [string]$Role,
        [int]$InstanceDebugPort
    )

    $blenderArgs = @(
        "--python", $BootstrapScript,
        "--",
        "--addon-zip", $AddonZip,
        "--host", $ServerHost,
        "--port", $ServerPort,
        "--role", $Role
    )

    if ($InstanceDebugPort -gt 0) {
        $blenderArgs += @("--debug-port", $InstanceDebugPort)
        if ($WaitForDebugger) {
            $blenderArgs += "--wait-for-client"
        }
    }
    if ($StartEmitter) {
        $blenderArgs += "--start-emitter"
    }
    if ($StartReceiver) {
        $blenderArgs += "--start-receiver"
    }

    return Start-Process -FilePath $BlenderExe -ArgumentList $blenderArgs -WorkingDirectory $RepoRoot -PassThru
}

Write-Host '[launcher] Starting Blender instance A (debug target) ...'
$BlenderA = Start-BlenderInstance -Role "A" -InstanceDebugPort $DebugPort

$BlenderB = $null
if ($TwoBlenders) {
    Write-Host '[launcher] Starting Blender instance B (debug target) ...'
    $BlenderB = Start-BlenderInstance -Role "B" -InstanceDebugPort $DebugPortB
}

Write-Host ''
Write-Host '========================================='
Write-Host ' USD Connect Debug Session'
Write-Host '========================================='
Write-Host ''
Write-Host '  Process          PID     Debug Port    Name'
Write-Host '  -------          ---     ----------    ----'
Write-Host "  Server           $($ServerProc.Id.ToString().PadRight(7)) -             $($ServerProc.ProcessName)"
Write-Host "  Blender A        $($BlenderA.Id.ToString().PadRight(7)) :${DebugPort}          $($BlenderA.ProcessName)"
if ($BlenderB) {
    Write-Host "  Blender B        $($BlenderB.Id.ToString().PadRight(7)) :${DebugPortB}          $($BlenderB.ProcessName)"
}
Write-Host ''
if ($WaitForDebugger) {
    Write-Host "[launcher] Blender A is waiting for debugger attach on :${DebugPort}"
    if ($BlenderB) {
        Write-Host "[launcher] Blender B is waiting for debugger attach on :${DebugPortB}"
    }
    Write-Host ''
}

$allPids = @($ServerProc.Id, $BlenderA.Id)
if ($BlenderB) {
    $allPids += $BlenderB.Id
}
$pidList = $allPids -join ", "
Write-Host "[launcher] To stop all: Stop-Process -Id ${pidList}"
Write-Host ''
Write-Host '[launcher] Waiting for Blender to exit (close Blender windows to stop)...'

# Wait for all Blender instances to exit, then stop the server.
$BlenderA.WaitForExit()
Write-Host '[launcher] Blender A exited.'
if ($BlenderB) {
    $BlenderB.WaitForExit()
    Write-Host '[launcher] Blender B exited.'
}

Write-Host "[launcher] Stopping server (PID $($ServerProc.Id))..."
if (-not $ServerProc.HasExited) {
    Stop-Process -Id $ServerProc.Id -ErrorAction SilentlyContinue
    $ServerProc.WaitForExit(5000) | Out-Null
}
Write-Host '[launcher] Session ended.'
