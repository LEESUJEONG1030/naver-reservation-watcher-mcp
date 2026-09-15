# Windows 작업 스케줄러에 watcher 서비스를 등록합니다.
# - 트리거: 이 사용자로 로그온할 때(재부팅 후에도 로그인하면 자동 시작)
# - 실패 시 재시작, 실행시간 제한 없음(장시간 상시 실행)
#
# 주의: 이 스크립트는 시스템에 "영구적으로" 남는 예약 작업을 만듭니다.
# 실행 전에 scripts\setup_venv.ps1 로 .venv 가 준비되어 있어야 합니다.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$taskName = "NaverReservationWatcher"

$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) {
    $pythonw = Join-Path $root ".venv\Scripts\python.exe"
}

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "-m nrw.watcher.service" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable

if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Write-Host "기존 '$taskName' 작업을 제거하고 다시 등록합니다."
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "naver-reservation-watcher-mcp 백그라운드 감시 서비스" | Out-Null

Write-Host "작업 스케줄러에 '$taskName' 등록 완료." -ForegroundColor Green
Write-Host "지금 바로 시작하려면: Start-ScheduledTask -TaskName '$taskName'"
Write-Host "상태 확인: Get-ScheduledTask -TaskName '$taskName' | Get-ScheduledTaskInfo"
Write-Host "로그 위치: $root\data\logs\watcher.log"
