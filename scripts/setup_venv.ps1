# 최초 설치: 가상환경 생성 + 의존성 설치 + Playwright Chromium 다운로드
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

Set-Location $root

if (-not (Test-Path ".venv")) {
    Write-Host "가상환경(.venv) 생성 중..."
    py -m venv .venv
}

$python = Join-Path $root ".venv\Scripts\python.exe"

Write-Host "pip 업그레이드..."
& $python -m pip install --upgrade pip

Write-Host "패키지 설치 (requirements.txt)..."
& $python -m pip install -r requirements.txt

Write-Host "nrw 패키지 설치 (editable)..."
& $python -m pip install -e .

Write-Host "Playwright Chromium 설치..."
& $python -m playwright install chromium

Write-Host ""
Write-Host "설치 완료." -ForegroundColor Green
Write-Host "다음 단계: scripts\login_naver.py 를 실행해 네이버에 한 번 로그인하세요."
