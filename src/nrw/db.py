"""SQLite storage layer. Shared between the MCP server process and the watcher
service process - both open independent connections to the same file, so we
turn on WAL + a busy timeout to tolerate concurrent access safely.

This module is intentionally free of any Playwright/FastMCP imports so it can
be unit-tested in isolation.
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

from nrw.config import CONFIG

SCHEMA = """
CREATE TABLE IF NOT EXISTS watches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    store_name TEXT NOT NULL,
    store_url TEXT,
    target_date TEXT NOT NULL,        -- YYYY-MM-DD
    time_min TEXT NOT NULL,           -- HH:MM
    time_max TEXT NOT NULL,           -- HH:MM
    time_priority TEXT,               -- JSON list[str] or NULL
    party_size INTEGER NOT NULL,
    watch_start_at TEXT NOT NULL,     -- ISO datetime
    watch_end_at TEXT NOT NULL,       -- ISO datetime
    mode TEXT NOT NULL,               -- AUTO | ASK | NOTIFY
    status TEXT NOT NULL,
    last_snapshot TEXT,               -- JSON
    last_checked_at TEXT,
    next_check_at TEXT,
    consecutive_errors INTEGER NOT NULL DEFAULT 0,
    reservation_id INTEGER,
    last_message TEXT,
    pending_candidate TEXT             -- JSON {time, party_size}, set while AWAITING_APPROVAL
);

CREATE TABLE IF NOT EXISTS reservations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    watch_id INTEGER,
    store_name TEXT NOT NULL,
    date TEXT NOT NULL,
    time TEXT NOT NULL,
    party_size INTEGER NOT NULL,
    naver_reservation_no TEXT,
    confirmed_at TEXT NOT NULL,
    raw_confirmation TEXT,
    coupon_name TEXT,
    coupon_discount INTEGER
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    type TEXT NOT NULL,
    watch_id INTEGER,
    payload TEXT,                     -- JSON
    status TEXT NOT NULL DEFAULT 'PENDING',
    result TEXT,                      -- JSON
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    watch_id INTEGER,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS heartbeat (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    ts TEXT NOT NULL,
    pid INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_watches_status ON watches(status);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_events_watch ON events(watch_id);
"""


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


@contextlib.contextmanager
def get_connection(db_path: Path | None = None):
    path = db_path or CONFIG.db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=8000;")
    conn.execute("PRAGMA foreign_keys=ON;")
    try:
        yield conn
    finally:
        conn.close()


# 이미 만들어진 DB 파일에 나중에 추가된 컬럼을 채워넣기 위한 경량 마이그레이션.
# "CREATE TABLE IF NOT EXISTS" 는 기존 테이블에 새 컬럼을 추가해주지 않으므로,
# 새 컬럼이 생길 때마다 여기에 (테이블, 컬럼, 타입) 을 추가한다.
_MIGRATIONS: list[tuple[str, str, str]] = [
    ("reservations", "coupon_name", "TEXT"),
    ("reservations", "coupon_discount", "INTEGER"),
]


def _run_migrations(conn: sqlite3.Connection) -> None:
    for table, column, col_type in _MIGRATIONS:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")


def init_db(db_path: Path | None = None) -> None:
    with get_connection(db_path) as conn:
        conn.executescript(SCHEMA)
        _run_migrations(conn)


def _row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    for key in ("time_priority", "last_snapshot", "payload", "result", "detail", "pending_candidate"):
        if key in d and d[key] is not None:
            try:
                d[key] = json.loads(d[key])
            except (json.JSONDecodeError, TypeError):
                pass
    return d


# ---------------------------------------------------------------------------
# watches
# ---------------------------------------------------------------------------

def create_watch(
    *,
    store_name: str,
    store_url: str | None,
    target_date: str,
    time_min: str,
    time_max: str,
    time_priority: list[str] | None,
    party_size: int,
    watch_start_at: str,
    watch_end_at: str,
    mode: str,
    status: str,
) -> int:
    ts = now_iso()
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO watches (
                created_at, updated_at, store_name, store_url, target_date,
                time_min, time_max, time_priority, party_size,
                watch_start_at, watch_end_at, mode, status, next_check_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts, ts, store_name, store_url, target_date,
                time_min, time_max,
                json.dumps(time_priority) if time_priority else None,
                party_size, watch_start_at, watch_end_at, mode, status,
                watch_start_at,
            ),
        )
        return cur.lastrowid


def get_watch(watch_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM watches WHERE id = ?", (watch_id,)).fetchone()
        return _row(row)


def list_watches(include_inactive: bool = True) -> list[dict]:
    terminal = ("RESERVED", "EXPIRED", "CANCELLED")
    with get_connection() as conn:
        if include_inactive:
            rows = conn.execute("SELECT * FROM watches ORDER BY id").fetchall()
        else:
            placeholders = ",".join("?" for _ in terminal)
            rows = conn.execute(
                f"SELECT * FROM watches WHERE status NOT IN ({placeholders}) ORDER BY id",
                terminal,
            ).fetchall()
        return [_row(r) for r in rows]


def update_watch(watch_id: int, **fields) -> None:
    if not fields:
        return
    fields = dict(fields)
    for json_field in ("time_priority", "last_snapshot", "pending_candidate"):
        if json_field in fields and not isinstance(fields[json_field], (str, type(None))):
            fields[json_field] = json.dumps(fields[json_field])
    fields["updated_at"] = now_iso()
    cols = ", ".join(f"{k} = ?" for k in fields)
    with get_connection() as conn:
        conn.execute(f"UPDATE watches SET {cols} WHERE id = ?", (*fields.values(), watch_id))


def delete_watch(watch_id: int) -> bool:
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM watches WHERE id = ?", (watch_id,))
        return cur.rowcount > 0


def due_watches(as_of_iso: str) -> list[dict]:
    """watcher가 지금 체크해야 하는 watch 목록 (활성 상태 + next_check_at 지남).

    NEEDS_HUMAN 과 AWAITING_APPROVAL 은 제외된다 - 둘 다 사용자 개입이 필요한
    상태이므로, CAPTCHA/인증 화면을 반복해서 두드리거나 승인 대기 중에 자동으로
    다시 시도하지 않고 명시적인 재개(ad-hoc check_availability / approve/decline)가
    있을 때까지 감시를 일시 중지한다."""
    active = ("PENDING", "WAITING_OPEN", "WATCHING")
    with get_connection() as conn:
        placeholders = ",".join("?" for _ in active)
        rows = conn.execute(
            f"""
            SELECT * FROM watches
            WHERE status IN ({placeholders})
              AND (next_check_at IS NULL OR next_check_at <= ?)
            ORDER BY next_check_at
            """,
            (*active, as_of_iso),
        ).fetchall()
        return [_row(r) for r in rows]


# ---------------------------------------------------------------------------
# reservations
# ---------------------------------------------------------------------------

def create_reservation(
    *,
    watch_id: int | None,
    store_name: str,
    date: str,
    time_: str,
    party_size: int,
    naver_reservation_no: str | None,
    raw_confirmation: str | None,
    coupon_name: str | None = None,
    coupon_discount: int | None = None,
) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO reservations (
                watch_id, store_name, date, time, party_size,
                naver_reservation_no, confirmed_at, raw_confirmation,
                coupon_name, coupon_discount
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (watch_id, store_name, date, time_, party_size,
             naver_reservation_no, now_iso(), raw_confirmation,
             coupon_name, coupon_discount),
        )
        return cur.lastrowid


def get_reservation(reservation_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM reservations WHERE id = ?", (reservation_id,)).fetchone()
        return _row(row)


def list_reservations(watch_id: int | None = None) -> list[dict]:
    with get_connection() as conn:
        if watch_id is None:
            rows = conn.execute("SELECT * FROM reservations ORDER BY id DESC").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM reservations WHERE watch_id = ? ORDER BY id DESC", (watch_id,)
            ).fetchall()
        return [_row(r) for r in rows]


# ---------------------------------------------------------------------------
# jobs (MCP -> watcher command queue)
# ---------------------------------------------------------------------------

def create_job(*, type_: str, watch_id: int | None, payload: dict | None) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO jobs (created_at, type, watch_id, payload, status) VALUES (?, ?, ?, ?, 'PENDING')",
            (now_iso(), type_, watch_id, json.dumps(payload) if payload else None),
        )
        return cur.lastrowid


def get_job(job_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _row(row)


def claim_pending_jobs(limit: int = 5) -> list[dict]:
    """watcher 쪽에서 호출: PENDING 작업들을 RUNNING으로 표시하고 가져온다."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id FROM jobs WHERE status = 'PENDING' ORDER BY id LIMIT ?", (limit,)
        ).fetchall()
        ids = [r["id"] for r in rows]
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        conn.execute(f"UPDATE jobs SET status = 'RUNNING' WHERE id IN ({placeholders})", ids)
        rows = conn.execute(f"SELECT * FROM jobs WHERE id IN ({placeholders})", ids).fetchall()
        return [_row(r) for r in rows]


def complete_job(job_id: int, *, status: str, result: dict | None) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE jobs SET status = ?, result = ?, completed_at = ? WHERE id = ?",
            (status, json.dumps(result) if result is not None else None, now_iso(), job_id),
        )


def wait_for_job(job_id: int, timeout_sec: float, poll_interval: float = 0.5) -> dict | None:
    """MCP 서버 쪽에서 호출: job이 끝날 때까지 대기 (블로킹)."""
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        job = get_job(job_id)
        if job and job["status"] in ("DONE", "ERROR"):
            return job
        time.sleep(poll_interval)
    return get_job(job_id)


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------

def add_event(*, watch_id: int | None, level: str, message: str, detail: dict | None = None) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO events (ts, watch_id, level, message, detail) VALUES (?, ?, ?, ?, ?)",
            (now_iso(), watch_id, level, message, json.dumps(detail) if detail else None),
        )
        return cur.lastrowid


def list_events(watch_id: int | None = None, limit: int = 50) -> list[dict]:
    with get_connection() as conn:
        if watch_id is None:
            rows = conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM events WHERE watch_id = ? ORDER BY id DESC LIMIT ?",
                (watch_id, limit),
            ).fetchall()
        return [_row(r) for r in rows]


# ---------------------------------------------------------------------------
# heartbeat
# ---------------------------------------------------------------------------

def update_heartbeat(pid: int) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO heartbeat (id, ts, pid) VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET ts = excluded.ts, pid = excluded.pid
            """,
            (now_iso(), pid),
        )


def get_heartbeat() -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM heartbeat WHERE id = 1").fetchone()
        return _row(row)


def is_watcher_alive(stale_after_sec: int) -> bool:
    hb = get_heartbeat()
    if not hb:
        return False
    last = datetime.fromisoformat(hb["ts"])
    return (datetime.now(UTC) - last).total_seconds() <= stale_after_sec
