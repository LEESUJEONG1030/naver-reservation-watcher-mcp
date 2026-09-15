"""Thin launcher bundled inside the naver-reservation-watcher-mcp .mcpb extension.

This file intentionally does NOT vendor fastmcp or any dependencies. The
extension's only job is to be an MCP *interface* (stdio) to the existing
project at NRW_INSTALL_PATH - the same project whose venv already has
fastmcp installed, and whose data/nrw.sqlite3 the always-on watcher service
(running separately, e.g. via Windows Task Scheduler) reads and writes.

At runtime it just adds <install_path>/src to sys.path and delegates to the
real nrw.mcp_server.server module - so this extension and the standalone
`python -m nrw.mcp_server.server` invocation run the exact same code, talking
to the exact same SQLite DB. It never touches Playwright or the watcher
process directly; all it does is read/write the shared `jobs`/`watches`
tables, exactly like the CLI entry point does.
"""
from __future__ import annotations

import os
import sys


def _resolve_install_path() -> str:
    install_path = os.environ.get("NRW_INSTALL_PATH")
    if not install_path:
        sys.stderr.write(
            "[naver-reservation-watcher-mcp] NRW_INSTALL_PATH가 설정되지 않았습니다.\n"
            "Claude Desktop 확장 설정(Extensions)에서 '설치 경로'에 이 프로젝트를 "
            "내려받은/복제한 폴더를 직접 지정해주세요.\n"
        )
        sys.exit(1)
    return install_path


def main() -> None:
    install_path = _resolve_install_path()
    src_path = os.path.join(install_path, "src")

    if not os.path.isdir(src_path):
        sys.stderr.write(
            f"[naver-reservation-watcher-mcp] 프로젝트 소스를 찾을 수 없습니다: {src_path}\n"
            "Claude Desktop 확장 설정(Extensions)에서 '설치 경로'가 "
            "naver-reservation-watcher-mcp 프로젝트 루트를 가리키는지 확인해주세요.\n"
        )
        sys.exit(1)

    sys.path.insert(0, src_path)

    try:
        from nrw.mcp_server.server import main as real_main
    except ImportError as e:
        sys.stderr.write(
            f"[naver-reservation-watcher-mcp] nrw 패키지를 불러오지 못했습니다: {e}\n"
            f"이 확장은 {install_path}\\.venv 의 Python으로 실행되어야 합니다 "
            "(fastmcp 등 의존성이 그 venv에 설치되어 있습니다).\n"
        )
        sys.exit(1)

    real_main()


if __name__ == "__main__":
    main()
