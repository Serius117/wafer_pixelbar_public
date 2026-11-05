# wafer-pixel startup script (R.5: full process topology)
# Starts all processes in the correct order (design doc §7.2):
#   hub -> bridge -> render -> ctl -> gamedog -> olv-wafer
# Usage: .\scripts\start_all.ps1 [-Sim]   -Sim skips OLV (sim/dev mode)

param(
    [switch]$Sim
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$LogFile = Join-Path $ProjectRoot "logs"

if (-not (Test-Path $LogFile)) { New-Item -ItemType Directory -Path $LogFile | Out-Null }

Write-Host "=== wafer-pixel start_all ===" -ForegroundColor Cyan
Write-Host "Project root: $ProjectRoot"

$script:Started = @()

function Start-WaferProc {
    param([string]$Label, [string[]]$Args, [string]$WorkDir = $ProjectRoot)
    $p = Start-Process -FilePath $VenvPython -ArgumentList $Args `
        -WorkingDirectory $WorkDir -WindowStyle Hidden -PassThru
    $script:Started += $Label
    Write-Host ("  [{0}] {1} (PID {2})" -f $script:Started.Count, $Label, $p.Id) -ForegroundColor Green
    Start-Sleep -Milliseconds 600
}

$total = 6; if ($Sim) { $total = 5 }

# 1. wafer-hub (event bus) -- must be first
Write-Host "[1/$total] wafer-hub..." -ForegroundColor Green
Start-WaferProc "hub" @("-m", "core.bus")
Start-Sleep -Seconds 1

# 2. wafer-bridge (independent opencode channel, D23)
Write-Host "[2/$total] wafer-bridge..." -ForegroundColor Green
Start-WaferProc "bridge" @("-m", "bridge")

# 3. wafer-render (face + LED sink, subscribes via RenderEventBridge)
Write-Host "[3/$total] wafer-render..." -ForegroundColor Green
Start-WaferProc "render" @("-m", "render")

# 4. wafer-ctl (hotkeys + notifications)
Write-Host "[4/$total] wafer-ctl..." -ForegroundColor Green
Start-WaferProc "ctl" @("-m", "ctl", "--no-hotkeys")

# 5. wafer-gamedog (game whitelist watcher)
Write-Host "[5/$total] wafer-gamedog..." -ForegroundColor Green
Start-WaferProc "gamedog" @("-m", "gamedog")

# 6. olv-wafer (audio pipeline host; skipped in sim mode)
if (-not $Sim) {
    Write-Host "[6/$total] olv-wafer..." -ForegroundColor Green
    $olvDir = Join-Path $ProjectRoot "olv\upstream\Open-LLM-VTuber"
    $olvPython = Join-Path $olvDir ".venv\Scripts\python.exe"
    if (Test-Path $olvPython) {
        $olv = Start-Process -FilePath $olvPython -ArgumentList "run_server.py" `
            -WorkingDirectory $olvDir -WindowStyle Hidden -PassThru
        Write-Host "  OLV server starting (port 12393, PID $($olv.Id))"
    } else {
        Write-Host "  OLV not found at $olvDir -- run T0.3 setup first" -ForegroundColor Red
    }
} else {
    Write-Host "[6] olv skipped (-Sim)" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "=== All processes started ===" -ForegroundColor Cyan
Write-Host "  Stop with: .\scripts\stop_all.ps1"
