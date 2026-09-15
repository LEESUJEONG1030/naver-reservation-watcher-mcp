# 작업 스케줄러에서 watcher 서비스 등록을 제거합니다.
$ErrorActionPreference = "Stop"
$taskName = "NaverReservationWatcher"

if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Host "'$taskName' 작업을 제거했습니다." -ForegroundColor Green
} else {
    Write-Host "'$taskName' 작업이 등록되어 있지 않습니다."
}
