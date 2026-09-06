# ==========================================================================
# install_startup_shortcut.ps1
#
# Alternative to install_unattended_service.ps1 for machines where Task
# Scheduler's "run whether logged on or not" mode is blocked by Group
# Policy (common on managed/corporate laptops) even from an elevated
# prompt -- that shows up as "Access is denied" on Register-ScheduledTask.
#
# This approach needs NO admin rights and NO Task Scheduler at all: it
# just drops a shortcut into your own Startup folder, which Windows already
# lets any normal user do. The trade-off is it only starts the app when
# YOU log in (not at full machine boot before anyone logs in, and not if
# you're logged out) -- but for "don't make me manually start this every
# time," that's normally exactly what you want anyway.
#
# Run this as your normal user -- no "Run as Administrator" needed.
# ==========================================================================

$ErrorActionPreference = "Stop"

$projectDir   = $PSScriptRoot
$pythonExe    = Join-Path $projectDir ".venv\Scripts\pythonw.exe"
$serverScript = Join-Path $projectDir "run_server.py"
$startupDir   = [Environment]::GetFolderPath("Startup")
$shortcutPath = Join-Path $startupDir "OIAppServer.lnk"

if (-not (Test-Path $pythonExe)) {
    Write-Host "ERROR: Could not find $pythonExe -- update `$pythonExe in this script if your venv is named/located differently." -ForegroundColor Red
    exit 1
}
if (-not (Test-Path $serverScript)) {
    Write-Host "ERROR: Could not find $serverScript -- run this script from the project root." -ForegroundColor Red
    exit 1
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $pythonExe
$shortcut.Arguments = "`"$serverScript`""
$shortcut.WorkingDirectory = $projectDir
$shortcut.WindowStyle = 7   # minimized
$shortcut.Description = "Starts the options/trading app server at login"
$shortcut.Save()

Write-Host "OK: Startup shortcut created at:" -ForegroundColor Green
Write-Host "    $shortcutPath"
Write-Host ""
Write-Host "It will run automatically next time you log in. To start it right now"
Write-Host "without logging out/in, either double-click that shortcut, or run:"
Write-Host "    Start-Process -FilePath `"$pythonExe`" -ArgumentList `"$serverScript`" -WorkingDirectory `"$projectDir`""
Write-Host ""
Start-Sleep -Seconds 1
$launch = Read-Host "Start it now? (y/n)"
if ($launch -eq "y") {
    Start-Process -FilePath $pythonExe -ArgumentList "`"$serverScript`"" -WorkingDirectory $projectDir
    Start-Sleep -Seconds 5
    try {
        $health = Invoke-RestMethod -Uri "http://localhost:5050/healthz" -TimeoutSec 10
        Write-Host "OK: App responded:" -ForegroundColor Green
        $health | ConvertTo-Json -Compress
    } catch {
        Write-Host "WARNING: Could not reach /healthz yet -- check logs\server.log" -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "To remove: delete this file:"
Write-Host "    $shortcutPath"
