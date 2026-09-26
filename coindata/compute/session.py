"""세션 표시 (PRD FR-4.9).

세션은 현지 시각과 IANA 시간대로 정의하고, 기준 시각 시점의 규칙으로 판정한다. 서머타임이 자동 반영되고
과거 시점 요약도 그 시각의 규칙을 쓰므로 결정적이다. 휴장일과 경제 일정은 반영하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from coindata.config import SessionsConfig, SessionWindow

SESSION_ORDER = ("asia", "europe", "us")
OFF_SESSION = "off_session"
TIMEZONE_DATA_UNAVAILABLE = "timezone_data_unavailable"


@dataclass(frozen=True, slots=True)
class SessionResult:
    label: str | None
    active: tuple[str, ...]
    null_reason: str | None


def _in_window(ref_time: int, window: SessionWindow) -> bool:
    """판정 날짜는 기준 시각의 현지 날짜다. 시작 포함, 끝 미포함."""
    local = datetime.fromtimestamp(ref_time // 1000, timezone.utc).astimezone(ZoneInfo(window.timezone))
    start = time.fromisoformat(window.start)
    end = time.fromisoformat(window.end)
    return start <= local.time() < end


def session(ref_time: int, config: SessionsConfig) -> SessionResult:
    try:
        active = tuple(name for name in SESSION_ORDER if _in_window(ref_time, getattr(config, name)))
    except (ZoneInfoNotFoundError, ValueError):
        # 시간대 데이터가 없다(Windows에서 tzdata 미설치 등). 추정하지 않고 사유를 남긴다.
        return SessionResult(None, (), TIMEZONE_DATA_UNAVAILABLE)
    if not active:
        return SessionResult(OFF_SESSION, (), None)
    if len(active) == 1:
        return SessionResult(active[0], active, None)
    return SessionResult("overlap_" + "_".join(active), active, None)
