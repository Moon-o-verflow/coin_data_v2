"""파생 지표: 미결제약정 4분면, 프리미엄 인덱스, metrics 최신값 (PRD 부록 A.5).

명목 미결제약정(`sum_open_interest_value`)은 방향성 판단에 쓰지 않는다(D-5). 4분면은 계약 수만 쓴다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from coindata.compute.indicators import percentile_rank
from coindata.compute.series import (
    INSUFFICIENT_HISTORY,
    WINDOW_CONTAINS_ABSENT_BAR,
    ZERO_DENOMINATOR,
    align_up,
)
from coindata.models import MINUTE_MS, PremiumKline

BP = 10_000
SOURCE_GAP = "source_gap"
INDETERMINATE = "indeterminate"

# ---------------------------------------------------------------------------
# 4분면 (A.5.1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QuadrantPoint:
    ts: int  # metrics 스냅샷 시각(5분 구간의 끝)
    period: str
    d_oi: float | None
    d_px: float | None
    quadrant: str | None
    null_reason: str | None


def _change(now: float | None, before: float | None) -> float | None:
    if now is None or before is None or before == 0:
        return None
    return (now - before) / before


def quadrant_at(
    ts: int,
    period: str,
    period_ms: int,
    oi_by_ts: Mapping[int, float | None],
    close_by_open_time: Mapping[int, float],
    oi_band: float,
    px_band: float,
) -> QuadrantPoint:
    """스냅샷 시각 `ts`에서 기간 `period`의 4분면. 가장 가까운 행으로 대체하지 않는다(R-3)."""
    oi_now, oi_before = oi_by_ts.get(ts), oi_by_ts.get(ts - period_ms)
    # Px_t는 close_time = ts − 1인 1분봉, 즉 open_time = ts − 1분인 봉의 종가다.
    px_now = close_by_open_time.get(ts - MINUTE_MS)
    px_before = close_by_open_time.get(ts - period_ms - MINUTE_MS)
    d_oi = _change(oi_now, oi_before)
    d_px = _change(px_now, px_before)
    if d_oi is None or d_px is None:
        missing = None in (oi_now, oi_before, px_now, px_before)
        return QuadrantPoint(ts, period, d_oi, d_px, None, SOURCE_GAP if missing else ZERO_DENOMINATOR)
    if abs(d_oi) < oi_band or abs(d_px) < px_band:
        quadrant = INDETERMINATE
    else:
        quadrant = f"oi_{'up' if d_oi > 0 else 'down'}_price_{'up' if d_px > 0 else 'down'}"
    return QuadrantPoint(ts, period, d_oi, d_px, quadrant, None)


# ---------------------------------------------------------------------------
# 프리미엄 인덱스 (A.5.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PremiumChange:
    window: str
    change_bp: float | None
    null_reason: str | None


@dataclass(frozen=True, slots=True)
class SmoothedBar:
    open_time: int
    value_bp: float | None  # 구간 내 1분 close 평균. 전부 없으면 None(부재 봉)
    missing_ratio: float
    pct: float | None
    null_reason: str | None


@dataclass(frozen=True, slots=True)
class PremiumResult:
    current_bp: float | None
    current_time: int | None  # 현재값 1분봉의 open_time
    changes: tuple[PremiumChange, ...]
    smoothed: tuple[SmoothedBar, ...]  # 마감된 평활 봉, 시간순


def premium(
    rows: Sequence[PremiumKline],
    ref_time: int,
    windows: Sequence[tuple[str, int]],
    smoothing_tf_ms: int,
    pct_lookback: int,
    start_ms: int,
) -> PremiumResult:
    """`rows`는 open_time 오름차순의 마감 프리미엄 1분봉이다. 평활 봉은 `start_ms` 이후 첫 경계부터 만든다."""
    closed = [r for r in rows if r.open_time < ref_time]
    close_by_time = {r.open_time: r.close * BP for r in closed}
    current = closed[-1] if closed else None
    changes = []
    for name, ms in windows:
        if current is None:
            changes.append(PremiumChange(name, None, SOURCE_GAP))
            continue
        before = close_by_time.get(current.open_time - ms)
        if before is None:
            changes.append(PremiumChange(name, None, SOURCE_GAP))
        else:
            changes.append(PremiumChange(name, current.close * BP - before, None))
    smoothed = _smooth(close_by_time, smoothing_tf_ms, start_ms, ref_time, pct_lookback)
    return PremiumResult(
        current.close * BP if current else None, current.open_time if current else None, tuple(changes), smoothed
    )


def _smooth(
    close_by_time: Mapping[int, float], tf_ms: int, start_ms: int, ref_time: int, lookback: int
) -> tuple[SmoothedBar, ...]:
    minutes = tf_ms // MINUTE_MS
    start = align_up(start_ms, tf_ms)
    count = max(0, (ref_time - start) // tf_ms)  # close_time < ref_time인 봉만
    values: list[float | None] = []
    missing: list[float] = []
    for i in range(count):
        open_time = start + i * tf_ms
        present = [close_by_time[t] for t in range(open_time, open_time + tf_ms, MINUTE_MS) if t in close_by_time]
        values.append(sum(present) / len(present) if present else None)
        missing.append((minutes - len(present)) / minutes)
    bars = []
    for i, value in enumerate(values):
        pct: float | None = None
        reason: str | None = None
        if i < lookback - 1:
            reason = INSUFFICIENT_HISTORY
        else:
            window = values[i - lookback + 1 : i + 1]
            if any(v is None for v in window):
                reason = WINDOW_CONTAINS_ABSENT_BAR
            else:
                pct = percentile_rank(value, window)  # type: ignore[arg-type]
        bars.append(SmoothedBar(start + i * tf_ms, value, missing[i], pct, reason))
    return tuple(bars)
