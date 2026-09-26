"""과거 발생 빈도 통계 (PRD 부록 B, CLAUDE.md R-1 예외).

예측이 아니라 과거 표본의 기술이다. 관측 빈도는 다음 사건의 발생 가능성을 뜻하지 않는다.
결과 정의는 부록 B에 확정되어 있으며, 바꾸려면 정의 버전을 올린다.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass

from coindata.compute.series import BarSeries
from coindata.compute.structure import ABOVE, Break
from coindata.config import S1_CURRENT_VERSION

S1_VERSION = S1_CURRENT_VERSION
S1_TF = "15m"  # 부록 B.1.1: 정의의 일부이며 설정값이 아니다
S1_CONTEXT_TF = "1h"
BREAK_KINDS = ("BOS", "MSS", "break_no_displacement", "break_unclassified")
UNAVAILABLE = "unavailable"
INSUFFICIENT_SAMPLE = "insufficient_sample"
BP = 10_000


@dataclass(frozen=True, slots=True)
class Outcome:
    """표본 하나의 판정 기간 N 결과."""

    held: bool
    mfe_bp: float
    mae_bp: float
    mfe_atr: float | None
    mae_atr: float | None


@dataclass(frozen=True, slots=True)
class Sample:
    brk: Break
    h1_efficiency_state: str
    m15_volatility_state: str
    outcomes: dict[int, Outcome]  # 판정 기간 → 결과


@dataclass(frozen=True, slots=True)
class Bucket:
    n: int
    held: int
    failed: int
    held_ratio: float | None
    mfe_bp_median: float | None
    mae_bp_median: float | None
    mfe_atr_median: float | None
    mae_atr_median: float | None
    null_reason: str | None


@dataclass(frozen=True, slots=True)
class S1Result:
    definition_version: str
    horizon_bars: tuple[int, ...]
    min_n: int
    period_start: int
    period_end: int | None
    samples: tuple[Sample, ...]
    excluded_overlap: int
    excluded_gap: int
    pending_outcome: int


def select_samples(
    series: BarSeries,
    breaks: Sequence[Break],
    atr_values: Sequence[float | None],
    volatility_states: Sequence[str | None],
    h1: BarSeries,
    h1_efficiency_states: Sequence[str | None],
    horizons: Sequence[int],
) -> tuple[list[Sample], int, int, int]:
    """부록 B.1.4 순서(중복 → 미확정 → 결측)로 표본을 고르고 결과를 판정한다."""
    longest = max(horizons)
    last = len(series.bars) - 1
    last_kept: dict[str, int] = {}
    samples: list[Sample] = []
    overlap = gap = pending = 0
    for brk in sorted(breaks, key=lambda b: b.bar_index):
        t = brk.bar_index
        prev = last_kept.get(brk.side)
        if prev is not None and t <= prev + longest:
            overlap += 1
            continue
        if t + longest > last:
            pending += 1
            continue
        if any(b is None for b in series.bars[t + 1 : t + longest + 1]):
            gap += 1
            continue
        last_kept[brk.side] = t
        outcomes = {n: _outcome(series, brk, atr_values[t - 1] if t > 0 else None, n) for n in horizons}
        samples.append(
            Sample(
                brk,
                _h1_state(series, t, h1, h1_efficiency_states),
                volatility_states[t] or UNAVAILABLE,
                outcomes,
            )
        )
    return samples, overlap, gap, pending


def _h1_state(series: BarSeries, t: int, h1: BarSeries, states: Sequence[str | None]) -> str:
    """부록 B.1.5: close_time ≤ 돌파 봉 close_time인 마지막 1h 봉의 상태. 같은 시각 마감이면 그 봉."""
    end = series.open_time(t) + series.tf_ms  # 돌파 봉 close_time + 1
    j = (end - h1.start) // h1.tf_ms - 1
    if j < 0 or j >= len(states):
        return UNAVAILABLE
    return states[j] or UNAVAILABLE


def _outcome(series: BarSeries, brk: Break, atr: float | None, n: int) -> Outcome:
    t, x = brk.bar_index, brk.swing.price
    bar = series.bars[t]
    assert bar is not None
    base = bar.close
    window = [b for b in series.bars[t + 1 : t + n + 1] if b is not None]
    above = brk.side == ABOVE
    failed = any((b.close <= x) if above else (b.close >= x) for b in window)
    if above:
        favorable = max(0.0, max(b.high for b in window) - base)
        adverse = max(0.0, base - min(b.low for b in window))
    else:
        favorable = max(0.0, base - min(b.low for b in window))
        adverse = max(0.0, max(b.high for b in window) - base)
    return Outcome(
        held=not failed,
        mfe_bp=favorable / base * BP,
        mae_bp=adverse / base * BP,
        mfe_atr=favorable / atr if atr else None,
        mae_atr=adverse / atr if atr else None,
    )


def bucket(samples: Sequence[Sample], horizon: int, min_n: int) -> Bucket:
    """부록 B.1.6. n < min_n이면 건수만 싣는다."""
    outcomes = [s.outcomes[horizon] for s in samples]
    n = len(outcomes)
    held = sum(1 for o in outcomes if o.held)
    if n < min_n:
        return Bucket(n, held, n - held, None, None, None, None, None, INSUFFICIENT_SAMPLE)

    def median(values: list[float]) -> float | None:
        return statistics.median(values) if values else None

    return Bucket(
        n, held, n - held, held / n,
        median([o.mfe_bp for o in outcomes]),
        median([o.mae_bp for o in outcomes]),
        median([o.mfe_atr for o in outcomes if o.mfe_atr is not None]),
        median([o.mae_atr for o in outcomes if o.mae_atr is not None]),
        None,
    )


def s1(
    series: BarSeries,
    breaks: Sequence[Break],
    atr_values: Sequence[float | None],
    volatility_states: Sequence[str | None],
    h1: BarSeries,
    h1_efficiency_states: Sequence[str | None],
    horizons: Sequence[int],
    min_n: int,
) -> S1Result:
    samples, overlap, gap, pending = select_samples(
        series, breaks, atr_values, volatility_states, h1, h1_efficiency_states, horizons
    )
    last_judged = len(series.bars) - 1 - max(horizons)
    return S1Result(
        S1_VERSION,
        tuple(horizons),
        min_n,
        series.start,
        series.open_time(last_judged) if last_judged >= 0 else None,
        tuple(samples),
        overlap,
        gap,
        pending,
    )
