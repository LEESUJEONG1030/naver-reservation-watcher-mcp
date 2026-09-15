"""Central configuration for nrw.

Config file resolution order:
1. NRW_CONFIG env var (explicit path)
2. <project_root>/config/config.toml
3. falls back to config/config.example.toml defaults (no file required)

Data (sqlite db, browser profile, logs) defaults to <project_root>/data,
overridable with NRW_DATA_DIR.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.toml"
EXAMPLE_CONFIG_PATH = PROJECT_ROOT / "config" / "config.example.toml"


def _load_raw() -> dict:
    path_str = os.environ.get("NRW_CONFIG")
    path = Path(path_str) if path_str else DEFAULT_CONFIG_PATH
    if not path.exists():
        path = EXAMPLE_CONFIG_PATH
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


@dataclass(frozen=True)
class PollingConfig:
    interval_min_sec: int = 45
    interval_max_sec: int = 120
    tight_poll_before_open_sec: int = 120
    tight_interval_min_sec: int = 5
    tight_interval_max_sec: int = 15
    error_backoff_base_sec: int = 30
    error_backoff_max_sec: int = 900
    job_poll_interval_sec: int = 2
    # 결제/동의/캡차 등으로 사용자 확인이 필요할 때, 브라우저 창을 열어둔 채로
    # 대기하는 시간(초). 이 시간 동안 사용자가 직접 화면에서 완료할 수 있다.
    human_pause_sec: int = 180


@dataclass(frozen=True)
class WatcherConfig:
    heartbeat_stale_after_sec: int = 90


@dataclass(frozen=True)
class NotifyConfig:
    channel: str = "windows_toast"


@dataclass(frozen=True)
class BrowserConfig:
    headless: bool = False
    profile_dir: str = ""


@dataclass(frozen=True)
class Config:
    polling: PollingConfig = field(default_factory=PollingConfig)
    watcher: WatcherConfig = field(default_factory=WatcherConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)

    data_dir: Path = field(default_factory=lambda: Path(
        os.environ.get("NRW_DATA_DIR", str(PROJECT_ROOT / "data"))
    ))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "nrw.sqlite3"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def browser_profile_dir(self) -> Path:
        if self.browser.profile_dir:
            return Path(self.browser.profile_dir)
        return self.data_dir / "browser_profile"

    @property
    def browser_lock_path(self) -> Path:
        return self.data_dir / "browser.lock"


def load_config() -> Config:
    raw = _load_raw()
    polling = PollingConfig(**raw.get("polling", {}))
    watcher = WatcherConfig(**raw.get("watcher", {}))
    notify = NotifyConfig(**raw.get("notify", {}))
    browser = BrowserConfig(**raw.get("browser", {}))
    cfg = Config(polling=polling, watcher=watcher, notify=notify, browser=browser)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    return cfg


CONFIG = load_config()
