# Claude Desktop이 이 스크립트 대신 python.exe를 직접 호출하지만,
# 수동으로 MCP 서버 동작을 확인해보고 싶을 때 사용합니다.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"

Set-Location $root
& $python -m nrw.mcp_server.server
