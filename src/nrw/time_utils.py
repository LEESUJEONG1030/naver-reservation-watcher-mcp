"""Time-range / priority matching logic shared by watcher core and MCP tools.

규칙:
- time_priority가 주어지면, 그 목록에서 가장 앞순위이면서 실제로 열려 있는 시간을 고른다.
- time_priority가 없으면, [time_min, time_max] 범위 내에서 가장 빠른 시간을 고른다.
- time_priority가 있어도 항상 [time_min, time_max] 범위 밖의 시간은 후보에서 제외한다
  (우선순위 목록은 범위의 부분집합이어야 하지만, 혹시 범위 밖 값이 들어와도 방어적으로 필터링).
"""
from __future__ import annotations

from datetime import time as _time


def parse_hhmm(value: str) -> _time:
    h, m = value.strip().split(":")
    return _time(int(h), int(m))


def in_range(value: str, time_min: str, time_max: str) -> bool:
    v, lo, hi = parse_hhmm(value), parse_hhmm(time_min), parse_hhmm(time_max)
    return lo <= v <= hi


def pick_best_candidate(
    available_times: list[str],
    time_min: str,
    time_max: str,
    time_priority: list[str] | None,
) -> str | None:
    """available_times 중 조건에 맞는 최선의 시간을 하나 고른다. 없으면 None."""
    in_range_times = {t for t in available_times if in_range(t, time_min, time_max)}
    if not in_range_times:
        return None

    if time_priority:
        for candidate in time_priority:
            if candidate in in_range_times:
                return candidate
        return None  # 우선순위 목록에 지정된 시간이 하나도 열려있지 않으면 매칭 실패로 취급

    return min(in_range_times, key=lambda t: parse_hhmm(t))


def rank_all_candidates(
    available_times: list[str],
    time_min: str,
    time_max: str,
    time_priority: list[str] | None,
) -> list[str]:
    """알림용: 조건에 맞는 모든 후보를 우선순위 순으로 나열."""
    in_range_times = [t for t in available_times if in_range(t, time_min, time_max)]
    if time_priority:
        ordered = [t for t in time_priority if t in in_range_times]
        rest = [t for t in in_range_times if t not in time_priority]
        return ordered + sorted(rest, key=lambda t: parse_hhmm(t))
    return sorted(in_range_times, key=lambda t: parse_hhmm(t))
