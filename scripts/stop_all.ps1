# wafer-pixel stop script
# Stops all wafer processes (matched by command line, reverse order)
# Usage: .\scripts\stop_all.ps1

$ErrorActionPreference = "SilentlyContinue"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

Write-Host "=== wafer-pixel stop_all ===" -ForegroundColor Cyan

# Our python -m entry points, stopped by command-line match
$modules = @(
    "core.bus",       # hub
    "bridge",         # wafer-bridge
    "render",         # wafer-render
    "ctl",            # wafer-ctl
    "gamedog"         # wafer-gamedog
)

foreach ($mod in $modules) {
    $procs = Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
        Where-Object { $_.CommandLine -like "*-m $mod*" -and $_.CommandLine -like "*$ProjectRoot*" }
    foreach ($p in $procs) {
        Stop-Process -Id $p.ProcessId -Force
        Write-Host "  Stopped $mod (PID $($p.ProcessId))" -ForegroundColor Green
    }
}

# Stop OLV (runs as python.exe in the upstream dir)
$olvProcs = Get-Process python* -ErrorAction SilentlyContinue | Where-Object {
    $_.Path -like "*Open-LLM-VTuber*"
}
if ($olvProcs) {
    $olvProcs | Stop-Process -Force
    Write-Host "  Stopped olv-wafer" -ForegroundColor Green
}

Write-Host "=== All stopped ===" -ForegroundColor Cyan
