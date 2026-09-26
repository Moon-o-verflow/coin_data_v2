"""변동성·효율성·캔들 지표 (PRD 부록 A.1.6, A.2, A.3.1, A.4.1).

모든 함수는 봉 목록과 같은 길이의 결과 목록을 돌려준다. 값을 만들 수 없는 자리는 `None`이다.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from coindata.compute.series import Bar


def percentile_rank(value: float, sample: Sequence[float]) -> float:
    """A.1.6: `pct = 100 × (B + 0.5 × E) / M`. `sample`은 `value`를 포함한다."""
    below = sum(1 for x in sample if x < value)
    equal = sum(1 for x in sample if x == value)
    return 100.0 * (below + 0.5 * equal) / len(sample)


def atr(bars: Sequence[Bar | None], n: int) -> list[float | None]:
    """A.2.1 Wilder ATR. 부재 봉을 만나면 재귀를 끊고 다음 봉부터 첫 값을 다시 만든다."""
    out: list[float | None] = [None] * len(bars)
    prev_close: float | None = None
    seed: list[float] = []
    value: float | None = None
    for i, bar in enumerate(bars):
        if bar is None:
            prev_close, seed, value = None, [], None
            continue
        tr = bar.high - bar.low
        if prev_close is not None:
            tr = max(tr, abs(bar.high - prev_close), abs(bar.low - prev_close))
        prev_close = bar.close
        if value is None:
            seed.append(tr)
            if len(seed) == n:
                value = sum(seed) / n
                out[i] = value
        else:
            value = (value * (n - 1) + tr) / n
            out[i] = value
    return out


def parkinson(bars: Sequence[Bar | None], n: int) -> list[float | None]:
    """A.2.2 Parkinson 변동성(봉당, 연율화하지 않음). 창에 부재 봉이 있으면 None."""
    logs = [math.log(b.high / b.low) ** 2 if b is not None else None for b in bars]
    out: list[float | None] = [None] * len(bars)
    denominator = 4 * math.log(2) * n
    for i in range(n - 1, len(bars)):
        window = logs[i - n + 1 : i + 1]
        if all(x is not None for x in window):
            out[i] = math.sqrt(sum(window) / denominator)  # type: ignore[arg-type]
    return out


def rolling_percentile(values: Sequence[float | None], m: int) -> list[float | None]:
    """최근 `m`개 값(현재 포함)에 대한 A.1.6 백분위. 창에 None이 있거나 값이 모자라면 None."""
    out: list[float | None] = [None] * len(values)
    for i in range(m - 1, len(values)):
        current = values[i]
        window = values[i - m + 1 : i + 1]
        if current is not None and all(x is not None for x in window):
            out[i] = percentile_rank(current, window)  # type: ignore[arg-type]
    return out


def efficiency_ratio(bars: Sequence[Bar | None], n: int) -> list[float | None]:
    """A.4.1 Kaufman ER. `n + 1`개 종가가 모두 있어야 한다. 분모가 0이면 None."""
    out: list[float | None] = [None] * len(bars)
    for i in range(n, len(bars)):
        window = bars[i - n : i + 1]
        if any(b is None for b in window):
            continue
        closes = [b.close for b in window if b is not None]
        path = sum(abs(closes[j] - closes[j - 1]) for j in range(1, len(closes)))
        if path > 0:
            out[i] = abs(closes[-1] - closes[0]) / path
    return out


@dataclass(frozen=True, slots=True)
class Candle:
    upper_wick_ratio: float | None
    lower_wick_ratio: float | None
    body_ratio: float | None
    body_atr: float | None
    range_atr: float | None
    close_vs_open: str  # above / below / equal


def close_vs_open(bar: Bar) -> str:
    if bar.close > bar.open:
        return "above"
    if bar.close < bar.open:
        return "below"
    return "equal"


def candle(bar: Bar, prev_atr: float | None) -> Candle:
    """A.3.1. 범위가 0이면 비율은 None, 직전 봉 ATR이 없거나 0이면 ATR 배수는 None(A.1.4)."""
    span = bar.high - bar.low
    body = abs(bar.close - bar.open)
    if span > 0:
        upper = (bar.high - max(bar.open, bar.close)) / span
        lower = (min(bar.open, bar.close) - bar.low) / span
        body_ratio: float | None = body / span
    else:
        upper = lower = body_ratio = None
    direction = close_vs_open(bar)
    if prev_atr:
        return Candle(upper, lower, body_ratio, body / prev_atr, span / prev_atr, direction)
    return Candle(upper, lower, body_ratio, None, None, direction)


def er_direction(bars: Sequence[Bar | None], index: int, n: int, er_value: float | None) -> str | None:
    """A.4.1 `er_direction`: `C_t − C_{t−n}`의 부호. ER이 없으면 None."""
    if er_value is None:
        return None
    now, before = bars[index], bars[index - n]
    assert now is not None and before is not None  # ER이 있으면 창의 봉이 모두 있다
    change = now.close - before.close
    return "up" if change > 0 else ("down" if change < 0 else "flat")


def trailing_mean(values: Sequence[float | None], end: int, length: int) -> float | None:
    """`end` 직전 `length`개(end 미포함)의 평균. 하나라도 없으면 None."""
    if end - length < 0:
        return None
    window = values[end - length : end]
    if any(x is None for x in window):
        return None
    return sum(window) / length  # type: ignore[arg-type]
