"""참조 지표 (PRD 부록 A.13, 계열 `reference`).

사용자의 차트 리딩을 검증하기 위한 종가 파생 지표다. 새로운 근거가 아니며 판단의 근거 수에 세지 않는다.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from coindata.compute.flow import ema
from coindata.compute.indicators import rolling_percentile
from coindata.compute.levels import distance_bp
from coindata.compute.series import BarSeries, Measured, measure
from coindata.compute.structure import EQUAL, HIGHER, LOWER, relation
from coindata.compute.zigzag import HIGH, LOW, Swing
from coindata.config import ReferenceConfig

FAST_ABOVE_SLOW = "fast_above_slow"
FAST_BELOW_SLOW = "fast_below_slow"
MIXED = "mixed"


def closes(series: BarSeries) -> list[float | None]:
    return [b.close if b is not None else None for b in series.bars]


def sma(values: Sequence[float | None], n: int) -> list[float | None]:
    """창에 None이 있으면 None."""
    out: list[float | None] = [None] * len(values)
    for i in range(n - 1, len(values)):
        window = values[i - n + 1 : i + 1]
        if all(v is not None for v in window):
            out[i] = sum(window) / n  # type: ignore[arg-type]
    return out


def rsi(values: Sequence[float | None], n: int) -> list[float | None]:
    """A.13.2 Wilder RSI. 부재(None) 뒤에는 다시 시작한다."""
    out: list[float | None] = [None] * len(values)
    prev: float | None = None
    gains: list[float] = []
    losses: list[float] = []
    avg_gain = avg_loss = None
    for i, value in enumerate(values):
        if value is None:
            prev, gains, losses, avg_gain, avg_loss = None, [], [], None, None
            continue
        if prev is not None:
            change = value - prev
            gain, loss = max(change, 0.0), max(-change, 0.0)
            if avg_gain is None:
                gains.append(gain)
                losses.append(loss)
                if len(gains) == n:
                    avg_gain, avg_loss = sum(gains) / n, sum(losses) / n
            else:
                avg_gain = (avg_gain * (n - 1) + gain) / n
                avg_loss = (avg_loss * (n - 1) + loss) / n  # type: ignore[operator]
            if avg_gain is not None and avg_loss is not None:
                out[i] = _rsi_value(avg_gain, avg_loss)
        prev = value
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float | None:
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else None
    return 100 - 100 / (1 + avg_gain / avg_loss)


@dataclass(frozen=True, slots=True)
class MaValue:
    period: int
    value: Measured
    distance_bp: float | None


@dataclass(frozen=True, slots=True)
class Bollinger:
    upper: float | None
    lower: float | None
    percent_b: Measured
    width: Measured
    width_pct: Measured


@dataclass(frozen=True, slots=True)
class Macd:
    histogram: Measured
    histogram_side: str | None
    bars_since_side_change: int | None


@dataclass(frozen=True, slots=True)
class Divergence:
    relation: str  # 예: price_higher_rsi_lower
    known_time: int  # 나중 스윙


@dataclass(frozen=True, slots=True)
class ReferenceResult:
    tf: str
    ma: tuple[MaValue, ...]
    ma_order: str | None
    rsi: Measured
    bollinger: Bollinger
    macd: Macd
    divergence_highs: Divergence | None
    divergence_lows: Divergence | None


def ma_order(values: Sequence[float | None], atr: float | None, tol_atr: float) -> str | None:
    """A.13.1: 기간 오름차순 인접 쌍의 차가 허용 오차 미만이면 같은 값."""
    if atr is None or any(v is None for v in values):
        return None
    tol = tol_atr * atr
    pairs = list(zip(values, values[1:]))
    if all(fast - slow >= tol and fast > slow for fast, slow in pairs):  # type: ignore[operator]
        return FAST_ABOVE_SLOW
    if all(slow - fast >= tol and slow > fast for fast, slow in pairs):  # type: ignore[operator]
        return FAST_BELOW_SLOW
    return MIXED


def _side(h: float) -> str:
    return "above_zero" if h > 0 else ("below_zero" if h < 0 else "zero")


def bars_since_side_change(hist: Sequence[float | None], last: int) -> int | None:
    """A.13.4: 히스토그램 부호가 직전 봉과 달라진 가장 최근 봉까지의 봉 수. 없거나 None으로 끊기면 None."""
    for i in range(last, 0, -1):
        now, before = hist[i], hist[i - 1]
        if now is None or before is None:
            return None
        if _side(now) != _side(before):
            return last - i
    return None


def _rsi_relation(earlier: float | None, later: float | None, tol: float) -> str | None:
    if earlier is None or later is None:
        return None
    diff = later - earlier
    if abs(diff) < tol:
        return EQUAL
    return HIGHER if diff > 0 else LOWER


def divergence(
    swings: Sequence[Swing], kind: str, rsi_values: Sequence[float | None], atr_values: Sequence[float | None],
    swing_tol_atr: float, rsi_tol: float,
) -> Divergence | None:
    """A.13.5: 같은 유형의 최근 확정 스윙 두 개의 가격 관계와 극점 봉 RSI 관계. 비교할 수 없으면 None."""
    same = [s for s in swings if s.type == kind]
    if len(same) < 2:
        return None
    earlier, later = same[-2], same[-1]
    price = relation(earlier, later, atr_values, swing_tol_atr)
    rsi_rel = _rsi_relation(rsi_values[earlier.bar_index], rsi_values[later.bar_index], rsi_tol)
    if price is None or rsi_rel is None:
        return None
    return Divergence(f"price_{price}_rsi_{rsi_rel}", later.known_time)


def reference(
    series: BarSeries,
    atr_values: Sequence[float | None],
    swings: Sequence[Swing],
    ref_price: float,
    config: ReferenceConfig,
    swing_tol_atr: float,
) -> ReferenceResult:
    last = len(series.bars) - 1
    c = closes(series)

    def at_last(values: Sequence[float | None]) -> float | None:
        return values[last] if last >= 0 else None

    # MA
    ma_values = []
    ma_last: list[float | None] = []
    for period in config.ma.periods:
        values = sma(c, period) if config.ma.kind == "sma" else ema(c, period)
        v = at_last(values)
        ma_last.append(v)
        ma_values.append(MaValue(period, measure(series, last, period, v), distance_bp(v, ref_price) if v is not None else None))
    prev_atr = atr_values[last - 1] if last >= 1 else None
    order = ma_order(ma_last, prev_atr, config.ma.equal_tol_atr)

    # RSI
    rsi_values = rsi(c, config.rsi.n)
    rsi_now = measure(series, last, config.rsi.n + 1, at_last(rsi_values))

    # 볼린저 밴드
    n, k = config.bb.n, config.bb.k
    mids = sma(c, n)
    widths: list[float | None] = [None] * len(c)
    upper = lower = None
    pb: float | None = None
    for i in range(n - 1, len(c)):
        mid = mids[i]
        if mid is None:
            continue
        window = c[i - n + 1 : i + 1]
        sd = math.sqrt(sum((x - mid) ** 2 for x in window) / n)  # type: ignore[operator]
        widths[i] = 2 * k * sd / mid if mid else None
        if i == last:
            upper, lower = mid + k * sd, mid - k * sd
            pb = (c[i] - lower) / (upper - lower) if upper != lower else None  # type: ignore[operator]
    width_pct = rolling_percentile(widths, config.bb.width_lookback)
    bb = Bollinger(
        upper, lower,
        measure(series, last, n, pb),  # 상단 = 하단이면 zero_denominator
        measure(series, last, n, at_last(widths)),
        measure(series, last, n + config.bb.width_lookback - 1, at_last(width_pct)),
    )

    # MACD
    m = config.macd
    fast, slow = ema(c, m.fast), ema(c, m.slow)
    line = [f - s if f is not None and s is not None else None for f, s in zip(fast, slow)]
    signal = ema(line, m.signal)
    hist = [x - y if x is not None and y is not None else None for x, y in zip(line, signal)]
    h_now = at_last(hist)
    macd = Macd(
        measure(series, last, m.slow + m.signal - 1, h_now),
        _side(h_now) if h_now is not None else None,
        bars_since_side_change(hist, last) if last >= 1 else None,
    )

    return ReferenceResult(
        series.tf, tuple(ma_values), order, rsi_now, bb, macd,
        divergence(swings, HIGH, rsi_values, atr_values, swing_tol_atr, config.rsi.equal_tol),
        divergence(swings, LOW, rsi_values, atr_values, swing_tol_atr, config.rsi.equal_tol),
    )

