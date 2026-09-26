"""파생 지표: 미결제약정 4분면, 프리미엄 인덱스, metrics 최신값 (PRD 부록 A.5).

명목 미결제약정(`sum_open_interest_value`)은 방향성 판단에 쓰지 않는다(D-5). 4분면은 계약 수만 쓴다.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from coindata.compute.indicators import percentile_rank
from coindata.compute.series import (
    INSUFFICIENT_HISTORY,
    WINDOW_CONTAINS_ABSENT_BAR,
    align_up,
)
from coindata.models import MINUTE_MS, LatestMetric, MetricsRow, PremiumKline

BP = 10_000
SOURCE_GAP = "source_gap"
INSUFFICIENT_COVERAGE = "insufficient_coverage"
UP, FLAT, DOWN = "up", "flat", "down"


def coverage_percentile(value: float, window: Sequence[float | None], length: int, min_coverage: float) -> float | None:
    """결측을 뺀 표본으로 A.1.6 백분위. 표본 수 / `length`가 `min_coverage` 미만이면 None(A.5.2, A.5.4)."""
    sample = [x for x in window if x is not None]
    if len(sample) / length < min_coverage:
        return None
    return percentile_rank(value, sample)


# ---------------------------------------------------------------------------
# 4분면 (A.5.1)
# ---------------------------------------------------------------------------


def _change(now: float | None, before: float | None) -> float | None:
    if now is None or before is None or before == 0:
        return None
    return (now - before) / before


def changes_at(
    ts: int,
    period_ms: int,
    oi_by_ts: Mapping[int, float | None],
    close_by_open_time: Mapping[int, float],
) -> tuple[float | None, float | None]:
    """스냅샷 시각 `ts`의 (dOI, dPx). 가장 가까운 행으로 대체하지 않는다(R-3)."""
    d_oi = _change(oi_by_ts.get(ts), oi_by_ts.get(ts - period_ms))
    # Px_t는 close_time = ts − 1인 1분봉, 즉 open_time = ts − 1분인 봉의 종가다.
    d_px = _change(close_by_open_time.get(ts - MINUTE_MS), close_by_open_time.get(ts - period_ms - MINUTE_MS))
    return d_oi, d_px


def _axis(d: float, band: float) -> str:
    if abs(d) <= band:
        return FLAT
    return UP if d > 0 else DOWN


def band_value(sorted_sample: Sequence[float], q: float) -> float:
    """A.5.1: 오름차순 표본의 `floor(q/100 × (M−1))`번째 값(보간 없음)."""
    return sorted_sample[math.floor(q / 100 * (len(sorted_sample) - 1))]


class _SlidingSorted:
    """최근 `length`개 값 중 None이 아닌 것의 정렬 목록."""

    def __init__(self, length: int) -> None:
        self.length = length
        self.window: list[float | None] = []
        self.sorted: list[float] = []

    def push(self, value: float | None) -> None:
        self.window.append(value)
        if value is not None:
            bisect.insort(self.sorted, value)
        if len(self.window) > self.length:
            old = self.window.pop(0)
            if old is not None:
                del self.sorted[bisect.bisect_left(self.sorted, old)]


@dataclass(frozen=True, slots=True)
class QuadrantPoint:
    ts: int  # metrics 스냅샷 시각(5분 구간의 끝)
    d_oi: float | None
    d_px: float | None
    band_oi: float | None
    band_px: float | None
    raw: str | None
    null_reason: str | None


@dataclass(frozen=True, slots=True)
class QuadrantState:
    period: str
    ts: int
    point: QuadrantPoint  # 최신 스냅샷의 원시 상태
    confirmed: str | None
    confirmed_since: int | None
    duration_snapshots: int | None
    duration_capped: bool
    changes: tuple[tuple[int, str, str, QuadrantPoint], ...]  # (ts, from, to, 그 시각의 점)


def quadrant_series(
    period_ms: int,
    oi_by_ts: Mapping[int, float | None],
    close_by_open_time: Mapping[int, float],
    first_ts: int,
    last_ts: int,
    step: int,
    band_lookback: int,
    band_pct: float,
    min_coverage: float,
) -> list[QuadrantPoint]:
    """`first_ts`부터 `last_ts`까지 5분 격자의 원시 상태. 불감대 표본은 `first_ts` 이전 `band_lookback − 1`칸부터 모은다."""
    oi_window, px_window = _SlidingSorted(band_lookback), _SlidingSorted(band_lookback)
    points = []
    for ts in range(first_ts - (band_lookback - 1) * step, last_ts + 1, step):
        d_oi, d_px = changes_at(ts, period_ms, oi_by_ts, close_by_open_time)
        oi_window.push(abs(d_oi) if d_oi is not None else None)
        px_window.push(abs(d_px) if d_px is not None else None)
        if ts < first_ts:
            continue
        if d_oi is None or d_px is None:
            points.append(QuadrantPoint(ts, d_oi, d_px, None, None, None, SOURCE_GAP))
            continue
        covered = min(len(oi_window.sorted), len(px_window.sorted)) / band_lookback >= min_coverage
        if not covered:
            points.append(QuadrantPoint(ts, d_oi, d_px, None, None, None, INSUFFICIENT_COVERAGE))
            continue
        band_oi, band_px = band_value(oi_window.sorted, band_pct), band_value(px_window.sorted, band_pct)
        raw = f"oi_{_axis(d_oi, band_oi)}_price_{_axis(d_px, band_px)}"
        points.append(QuadrantPoint(ts, d_oi, d_px, band_oi, band_px, raw, None))
    return points


def confirm(period: str, points: Sequence[QuadrantPoint], confirm_snapshots: int) -> QuadrantState:
    """A.5.1 확정 상태: 같은 원시 상태가 `confirm_snapshots`회 연속하면 갱신한다. None은 연속을 끊는다."""
    confirmed: str | None = None
    since: int | None = None
    capped = False
    streak, streak_start = 0, 0
    changes: list[tuple[int, str, str, QuadrantPoint]] = []
    for i, point in enumerate(points):
        if point.raw is None:
            streak = 0
            continue
        if streak and points[i - 1].raw == point.raw:
            streak += 1
        else:
            streak, streak_start = 1, i
        if streak >= confirm_snapshots and point.raw != confirmed:
            if confirmed is not None:
                changes.append((point.ts, confirmed, point.raw, point))
            confirmed, since, capped = point.raw, point.ts, streak_start == 0
    last = points[-1]
    step = points[1].ts - points[0].ts if len(points) > 1 else 0
    duration = (last.ts - since) // step if since is not None and step else (0 if since is not None else None)
    return QuadrantState(period, last.ts, last, confirmed, since, duration, capped, tuple(changes))


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
    current_pct: float | None  # 현재값의 최근 1분 값 분포 위치 (A.5.2)
    current_pct_null_reason: str | None
    changes: tuple[PremiumChange, ...]
    smoothed: tuple[SmoothedBar, ...]  # 마감된 평활 봉, 시간순


def premium(
    rows: Sequence[PremiumKline],
    ref_time: int,
    windows: Sequence[tuple[str, int]],
    smoothing_tf_ms: int,
    pct_lookback: int,
    start_ms: int,
    current_pct_lookback: int,
    min_coverage: float,
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
    current_pct, pct_reason = None, SOURCE_GAP
    if current is not None:
        first = current.open_time - (current_pct_lookback - 1) * MINUTE_MS
        window = [close_by_time.get(t) for t in range(first, current.open_time + 1, MINUTE_MS)]
        current_pct = coverage_percentile(current.close * BP, window, current_pct_lookback, min_coverage)
        pct_reason = None if current_pct is not None else INSUFFICIENT_COVERAGE
    return PremiumResult(
        current.close * BP if current else None, current.open_time if current else None,
        current_pct, pct_reason, tuple(changes), smoothed,
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


# ---------------------------------------------------------------------------
# 비율 지표 백분위 (A.5.4)
# ---------------------------------------------------------------------------

RATIO_FIELDS: tuple[str, ...] = (
    "top_position_ratio", "top_account_ratio", "global_account_ratio", "taker_buy_sell_ratio",
)


@dataclass(frozen=True, slots=True)
class RatioValue:
    field: str
    value: float | None
    ts: int | None
    pct: float | None
    sample_n: int
    null_reason: str | None


def ratio_values(
    rows: Sequence[MetricsRow],
    latest: Sequence[LatestMetric],
    step: int,
    lookback: int,
    min_coverage: float,
) -> tuple[RatioValue, ...]:
    """컬럼별 최신값과, 그 `ts`까지 최근 `lookback`칸의 분포 위치."""
    by_field = {m.field: m for m in latest}
    result = []
    for name in RATIO_FIELDS:
        m = by_field.get(name)
        if m is None or m.value is None or m.ts is None:
            result.append(RatioValue(name, None, None, None, 0, SOURCE_GAP))
            continue
        first = m.ts - (lookback - 1) * step
        by_ts = {r.ts: getattr(r, name) for r in rows if first <= r.ts <= m.ts}
        window = [by_ts.get(ts) for ts in range(first, m.ts + 1, step)]
        sample_n = sum(1 for x in window if x is not None)
        pct = coverage_percentile(m.value, window, lookback, min_coverage)
        result.append(RatioValue(name, m.value, m.ts, pct, sample_n, None if pct is not None else INSUFFICIENT_COVERAGE))
    return tuple(result)
