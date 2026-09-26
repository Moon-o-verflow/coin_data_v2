"""타임프레임 봉 합성 (PRD 부록 A.1.1 ~ A.1.3, A.1.8).

1분봉으로 상위 타임프레임 봉을 만든다. 봉 경계는 UTC 기준이며, 시작점(A.1.8) 이후 첫 경계부터 합성한다.
1분봉이 하나도 없는 구간은 부재 봉(`None`)이다. 진행 중인 봉은 계산에 넣지 않고 `forming`으로 따로 둔다.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from coindata.models import MINUTE_MS, Kline

_UNIT_MS = {"m": MINUTE_MS, "h": 60 * MINUTE_MS, "d": 1440 * MINUTE_MS}


def parse_tf(tf: str) -> int:
    """'15m', '1h', '1d' 같은 타임프레임 표기를 밀리초로 바꾼다."""
    if len(tf) < 2 or tf[-1] not in _UNIT_MS or not tf[:-1].isdigit() or int(tf[:-1]) < 1:
        raise ValueError(f"타임프레임 표기가 아니다: {tf!r}")
    return int(tf[:-1]) * _UNIT_MS[tf[-1]]


def align_up(value: int, step: int) -> int:
    return -(-value // step) * step


@dataclass(frozen=True, slots=True)
class Bar:
    open_time: int
    tf_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float
    high_time: int  # high를 처음 기록한 1분봉의 open_time (A.1.1)
    low_time: int
    missing_minutes: int

    @property
    def close_time(self) -> int:
        return self.open_time + self.tf_ms - 1

    @property
    def missing_ratio(self) -> float:
        return self.missing_minutes / (self.tf_ms // MINUTE_MS)


@dataclass(frozen=True, slots=True)
class BarSeries:
    """시작 경계부터 마지막 마감 봉까지 빈틈없는 봉 목록. 인덱스 i의 봉은 `start + i × tf_ms`에 시작한다."""

    tf: str
    tf_ms: int
    start: int
    bars: tuple[Bar | None, ...]
    forming: Bar | None  # 진행 중인 봉 (D-8). 계산에 쓰지 않는다

    def open_time(self, index: int) -> int:
        return self.start + index * self.tf_ms

    def index_of(self, open_time: int) -> int:
        return (open_time - self.start) // self.tf_ms

    def __len__(self) -> int:
        return len(self.bars)


class _Acc:
    __slots__ = ("open_time", "open", "high", "low", "close", "volume", "quote", "high_time", "low_time", "count")

    def __init__(self, k: Kline) -> None:
        self.open_time = k.open_time
        self.open, self.high, self.low, self.close = k.open, k.high, k.low, k.close
        self.volume, self.quote = k.volume, k.quote_volume
        self.high_time = self.low_time = k.open_time
        self.count = 1

    def add(self, k: Kline) -> None:
        if k.high > self.high:
            self.high, self.high_time = k.high, k.open_time
        if k.low < self.low:
            self.low, self.low_time = k.low, k.open_time
        self.close = k.close
        self.volume += k.volume
        self.quote += k.quote_volume
        self.count += 1

    def bar(self, open_time: int, tf_ms: int) -> Bar:
        minutes = tf_ms // MINUTE_MS
        return Bar(
            open_time, tf_ms, self.open, self.high, self.low, self.close, self.volume, self.quote,
            self.high_time, self.low_time, minutes - self.count,
        )


def synthesize(tf: str, klines: Sequence[Kline], anchor_ms: int, ref_time: int) -> BarSeries:
    """정렬된 마감 1분봉으로 `tf` 봉을 합성한다. `ref_time` 이전에 끝난 봉만 `bars`에 넣는다."""
    tf_ms = parse_tf(tf)
    start = align_up(anchor_ms, tf_ms)
    last_open = (ref_time - tf_ms) // tf_ms * tf_ms  # close_time < ref_time인 마지막 봉
    count = max(0, (last_open - start) // tf_ms + 1)
    accs: list[_Acc | None] = [None] * (count + 1)  # 마지막 칸은 진행 중인 봉
    for k in klines:
        if k.open_time < start or k.open_time >= ref_time:
            continue
        slot = (k.open_time - start) // tf_ms
        if slot > count:
            continue
        acc = accs[slot]
        if acc is None:
            accs[slot] = _Acc(k)
        else:
            acc.add(k)
    bars = tuple(acc.bar(start + i * tf_ms, tf_ms) if acc else None for i, acc in enumerate(accs[:count]))
    forming_acc = accs[count]
    forming = forming_acc.bar(start + count * tf_ms, tf_ms) if forming_acc else None
    return BarSeries(tf, tf_ms, start, bars, forming)


def window_gap_ratio(series: BarSeries, end_index: int, length: int) -> float | None:
    """`end_index`에서 끝나는 `length`개 봉의 결손 비율(분 단위, A.1.3). 부재 봉이 있거나 범위가 모자라면 None."""
    first = end_index - length + 1
    if first < 0 or end_index >= len(series.bars):
        return None
    window = series.bars[first : end_index + 1]
    if any(bar is None for bar in window):
        return None
    minutes = series.tf_ms // MINUTE_MS
    return sum(bar.missing_minutes for bar in window if bar is not None) / (length * minutes)


# 값이 null인 사유 (A.1.3, A.1.4, A.9.2)
INSUFFICIENT_HISTORY = "insufficient_history"
WINDOW_CONTAINS_ABSENT_BAR = "window_contains_absent_bar"
ZERO_DENOMINATOR = "zero_denominator"


@dataclass(frozen=True, slots=True)
class Measured:
    """지표값과 그 계산 창의 결손 비율(FR-3.0). 값이 없으면 `null_reason`에 사유가 있다."""

    value: float | None
    gap_ratio: float | None
    null_reason: str | None


def measure(series: BarSeries, end_index: int, length: int, value: float | None) -> Measured:
    """`end_index`에서 끝나는 `length`봉 창으로 계산한 값에 결손 비율과 null 사유를 붙인다."""
    if end_index < 0 or end_index - length + 1 < 0:
        return Measured(None, None, INSUFFICIENT_HISTORY)
    gap = window_gap_ratio(series, end_index, length)
    if value is not None:
        return Measured(value, gap, None)
    if gap is None:
        return Measured(None, None, WINDOW_CONTAINS_ABSENT_BAR)
    return Measured(None, gap, ZERO_DENOMINATOR)
