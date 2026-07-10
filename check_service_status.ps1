# check_service_status.ps1
# Quick health check for the OIAppServer scheduled task + the app itself.

$taskName = "OIAppServer"

$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Host "ERROR: Task '$taskName' is not installed. Run install_unattended_service.ps1 first." -ForegroundColor Red
    exit 1
}

$info = Get-ScheduledTaskInfo -TaskName $taskName
Write-Host "Task state: $($task.State)"
Write-Host "Last run:   $($info.LastRunTime)   (result code: $($info.LastTaskResult))"
Write-Host "Next run:   $($info.NextRunTime)"
Write-Host ""

try {
    $health = Invoke-RestMethod -Uri "http://localhost:5050/healthz" -TimeoutSec 10
    Write-Host "OK: App is responding:" -ForegroundColor Green
    $health | ConvertTo-Json
    if ($health.stale_jobs -and $health.stale_jobs.Count -gt 0) {
        Write-Host ""
        Write-Host "WARNING: These enabled jobs haven't run in over 6 hours -- worth checking the Scheduler page:" -ForegroundColor Yellow
        $health.stale_jobs | ForEach-Object { Write-Host "   - $_" }
    }
} catch {
    Write-Host "ERROR: App is NOT responding on http://localhost:5050/healthz" -ForegroundColor Red
    Write-Host "   Check logs\server.log for errors, or try: Start-ScheduledTask -TaskName $taskName"
}
