# ==========================================================================
# install_unattended_service.ps1
#
# Installs the app as a Windows Scheduled Task that:
#   - starts automatically when the machine boots (not just at a fixed
#     daily time -- the existing create_scheduled_task.ps1 only fires once
#     a day, which isn't the same as "always running")
#   - runs whether or not you're logged in (LogonType S4U -- no stored
#     password needed, unlike a plain "run as user" task)
#   - restarts itself automatically if it crashes
#   - runs the production server (run_server.py, via waitress) instead of
#     the Flask dev server app.py uses when run directly -- the dev server
#     explicitly isn't meant for unattended 24/7 use
#
# This replaces the daily-9pm task from create_scheduled_task.ps1 for the
# purpose of running the app itself continuously. Keep run_fetch_daily.bat
# / create_scheduled_task.ps1 only if you specifically want a SEPARATE
# one-shot fetch outside of what the app's own background schedulers
# already do (Scheduler page under Tools shows/controls all of those).
#
# Run this once, from an elevated ("Run as Administrator") PowerShell.
# ==========================================================================

$ErrorActionPreference = "Stop"

# --- CONFIGURATION - adjust these two paths if your setup differs ---
$taskName   = "OIAppServer"
$projectDir = $PSScriptRoot                                    # folder this script lives in
$pythonExe  = Join-Path $projectDir ".venv\Scripts\pythonw.exe" # pythonw = no console window
$serverScript = Join-Path $projectDir "run_server.py"

if (-not (Test-Path $pythonExe)) {
    Write-Host "ERROR: Could not find $pythonExe -- update the `$pythonExe line in this script if your venv is named/located differently." -ForegroundColor Red
    exit 1
}
if (-not (Test-Path $serverScript)) {
    Write-Host "ERROR: Could not find $serverScript -- run this script from the project root." -ForegroundColor Red
    exit 1
}

# --- Remove any old task with the same name ---
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Host "Old task '$taskName' removed."
}

# --- Action: run the production server, working directory = project root ---
$action = New-ScheduledTaskAction -Execute $pythonExe -Argument "`"$serverScript`"" -WorkingDirectory $projectDir

# --- Triggers: at boot, AND at logon as a fallback ---
$triggerBoot  = New-ScheduledTaskTrigger -AtStartup
$triggerLogon = New-ScheduledTaskTrigger -AtLogOn

# --- Run whether logged in or not, without needing to store a password ---
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Highest

# --- Keep it running: restart on failure, don't stop on battery/idle rules ---
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 0) `
    -MultipleInstances IgnoreNew

try {
    Register-ScheduledTask -TaskName $taskName -Action $action `
        -Trigger @($triggerBoot, $triggerLogon) -Principal $principal -Settings $settings `
        -Description "Runs the options/trading app's Flask server unattended (waitress) so its scheduled scans, Signal Notifier, and journal checks all run without anyone opening the UI." `
        -ErrorAction Stop | Out-Null
} catch {
    Write-Host "ERROR: Failed to create task: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host ""
    Write-Host "This is most often 'Access is denied' because:" -ForegroundColor Yellow
    Write-Host "  1) PowerShell wasn't actually elevated -- check the title bar says" -ForegroundColor Yellow
    Write-Host "     'Administrator: Windows PowerShell', not just 'Windows PowerShell'." -ForegroundColor Yellow
    Write-Host "  2) A managed/corporate machine's Group Policy blocks 'run whether" -ForegroundColor Yellow
    Write-Host "     logged on or not' tasks even for admins. If so, use" -ForegroundColor Yellow
    Write-Host "     install_startup_shortcut.ps1 instead -- it needs no admin rights" -ForegroundColor Yellow
    Write-Host "     at all (runs at login instead of at full machine boot)." -ForegroundColor Yellow
    exit 1
}

# Verify it's actually there before claiming success -- Register-ScheduledTask
# can sometimes report a non-terminating CIM error that a try/catch alone
# doesn't reliably catch, so don't just trust "no exception was thrown".
if (-not (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue)) {
    Write-Host "ERROR: Task registration did not actually succeed (no exception was raised, but the task isn't there)." -ForegroundColor Red
    Write-Host "Try install_startup_shortcut.ps1 instead -- it needs no admin rights." -ForegroundColor Yellow
    exit 1
}
Write-Host "OK: Task '$taskName' created. It will start at boot/logon and auto-restart on crash." -ForegroundColor Green

# --- Start it now too, so you don't have to reboot to verify it works ---
Start-ScheduledTask -TaskName $taskName
Start-Sleep -Seconds 5
Write-Host "Checking http://localhost:5050/healthz ..."
try {
    $health = Invoke-RestMethod -Uri "http://localhost:5050/healthz" -TimeoutSec 10
    Write-Host "OK: App responded:" -ForegroundColor Green
    $health | ConvertTo-Json -Compress
} catch {
    Write-Host "WARNING: Could not reach /healthz yet -- it may still be starting. Check logs\server.log, or re-run check_service_status.ps1 in a minute." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Logs: $projectDir\logs\server.log (rotates daily, keeps 14 days)"
Write-Host "To stop:      Stop-ScheduledTask -TaskName $taskName"
Write-Host "To uninstall: .\uninstall_unattended_service.ps1"
