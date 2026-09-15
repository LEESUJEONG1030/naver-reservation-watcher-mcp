# watcher 서비스를 포그라운드에서 실행 (디버깅/수동 실행용).
# Windows 작업 스케줄러는 이 스크립트가 아니라 register_task_scheduler.ps1 로 등록합니다.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"

Set-Location $root
& $python -m nrw.watcher.service
