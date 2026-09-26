"""UTC 시각 변환. 모든 변환은 정수 연산으로 하며 부동소수점을 거치지 않는다(CLAUDE.md D-1)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from coindata.models import DAY_MS

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_ONE_MS = timedelta(milliseconds=1)


def datetime_to_ms(value: datetime) -> int:
    if value.tzinfo is None:
        raise ValueError("시간대가 없는 datetime은 변환하지 않는다")
    return (value - _EPOCH) // _ONE_MS


def parse_utc_text_to_ms(text: str) -> int:
    """`YYYY-MM-DD HH:MM:SS` 형식의 UTC 문자열을 밀리초 정수로 바꾼다."""
    return datetime_to_ms(datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC))


def day_start_ms(day: date) -> int:
    return datetime_to_ms(datetime(day.year, day.month, day.day, tzinfo=UTC))


def ms_to_day(ms: int) -> date:
    return date(1970, 1, 1) + timedelta(days=ms // DAY_MS)


def format_ms(ms: int) -> str:
    """표시용 UTC 문자열 `YYYY-MM-DD HH:MM`."""
    return (_EPOCH + ms * _ONE_MS).strftime("%Y-%m-%d %H:%M")
