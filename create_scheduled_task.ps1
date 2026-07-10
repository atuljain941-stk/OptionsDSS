# ==========================================
# Create Windows Scheduled Task to run run_fetch_daily.bat
# Executes daily at 9:00 PM
# ==========================================

# --- CONFIGURATION ---
$taskName  = "OIApp"
$batPath   = "T:\ajain33\Learn\Python\spy_oi_app_v3\app.py"   # <-- Update path if needed
$startTime = "21:00"   # 9:00 PM (24-hour format)

# --- Remove any old task with same name ---
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Host "Old task '$taskName' removed."
}

# --- Define the action ---
# /c runs the command and then terminates
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$batPath`""

# --- Define the trigger ---
$trigger = New-ScheduledTaskTrigger -Daily -At $startTime

# --- Define principal and settings ---
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

# --- Register the task ---
try {
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings
    Write-Host "✅ Task '$taskName' created successfully. It will run daily at $startTime."
} catch {
    Write-Host "❌ Failed to create task: $($_.Exception.Message)"
}

# --- Verify ---
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($task) {
    Write-Host "`n✅ Task visible in Task Scheduler under 'Task Scheduler Library'."
    Write-Host "   Next Run Time: $($task.Triggers[0].StartBoundary)"
} else {
    Write-Host "`n⚠️ Task creation failed or not visible. Try running PowerShell as Administrator."
}
