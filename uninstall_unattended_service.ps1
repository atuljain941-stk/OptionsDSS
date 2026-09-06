# uninstall_unattended_service.ps1
# Stops and removes the OIAppServer scheduled task installed by
# install_unattended_service.ps1. Does NOT touch your data/options_data.db.

$taskName = "OIAppServer"

if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Host "OK: Task '$taskName' stopped and removed." -ForegroundColor Green
} else {
    Write-Host "No task named '$taskName' found -- nothing to do."
}

Write-Host ""
Write-Host "Note: if the python.exe/pythonw.exe process is still running (rare, if it"
Write-Host "was mid-request when stopped), end it manually in Task Manager -- look for"
Write-Host "pythonw.exe running run_server.py."
