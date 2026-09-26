"""이벤트 구조와 판정 (PRD 부록 A.8). 이벤트는 사실과 측정값만 담는다(R-2).

측정값 필드 이름 끝의 `_`는 파이썬 예약어를 피하기 위한 것이다(`from_` → `from`). 직렬화할 때 뗀다.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from coindata.compute.indicators import trailing_mean
from coindata.compute.levels import Level, build_level, members_known_at
from coindata.compute.regime import SHOCK, ShockStart
from coindata.compute.series import BarSeries
from coindata.compute.structure import Break
from coindata.compute.zigzag import Swing

PRICE_STRUCTURE = "price_structure"
REGIME = "regime"
DERIVATIVES = "derivatives"
LEVEL = "level"


@dataclass(frozen=True, slots=True)
class SwingConfirmedMeasures:
    swing_type: str
    price: float
    extreme_bar_time: int
    lag_bars: int


@dataclass(frozen=True, slots=True)
class StructureBreakMeasures:
    side: str
    break_kind: str
    swing_price: float
    close_beyond_atr: float | None
    displacement_mult: float | None


@dataclass(frozen=True, slots=True)
class VolumeSpikeMeasures:
    volume_mult: float


@dataclass(frozen=True, slots=True)
class EfficiencyChangeMeasures:
    from_: str
    to: str
    er: float | None


@dataclass(frozen=True, slots=True)
class VolatilityChangeMeasures:
    from_: str
    to: str
    pct: float | None


@dataclass(frozen=True, slots=True)
class ShockStartMeasures:
    trigger: str
    wick_ratio: float | None
    range_atr: float | None


@dataclass(frozen=True, slots=True)
class LevelWickMeasures:
    level_id: str
    level_center: float
    penetration_atr: float
    source_count: int


@dataclass(frozen=True, slots=True)
class LevelCloseIntoMeasures:
    level_id: str
    level_center: float
    source_count: int


@dataclass(frozen=True, slots=True)
class LevelCloseThroughMeasures:
    level_id: str
    level_center: float
    close_beyond_atr: float
    source_count: int


@dataclass(frozen=True, slots=True)
class QuadrantChangeMeasures:
    period: str
    from_: str
    to: str
    d_oi: float | None
    d_px: float | None


@dataclass(frozen=True, slots=True)
class PremiumExtremeMeasures:
    side: str  # high / low
    value_bp: float
    pct: float


Measures = (
    SwingConfirmedMeasures
    | StructureBreakMeasures
    | VolumeSpikeMeasures
    | EfficiencyChangeMeasures
    | VolatilityChangeMeasures
    | ShockStartMeasures
    | LevelWickMeasures
    | LevelCloseIntoMeasures
    | LevelCloseThroughMeasures
    | QuadrantChangeMeasures
    | PremiumExtremeMeasures
)


@dataclass(frozen=True, slots=True)
class Event:
    type: str
    family: str
    tf: str
    bar_time: int  # 발생 봉의 open_time. 5m 기준 이벤트(quadrant_change)는 metrics 스냅샷 시각(ts)
    bars_ago: int
    measures: Measures


# ---------------------------------------------------------------------------
# 타임프레임 이벤트
# ---------------------------------------------------------------------------


def in_window(index: int, last: int, report_bars: int) -> bool:
    """A.8.2: 최근 `report_bars`개 마감 봉 안(현재 봉 = bars_ago 0)."""
    return 0 <= last - index < report_bars


def swing_events(series: BarSeries, swings: Sequence[Swing], last: int, report_bars: int) -> list[Event]:
    return [
        Event(
            "swing_confirmed", PRICE_STRUCTURE, series.tf, series.open_time(s.confirmed_index), last - s.confirmed_index,
            SwingConfirmedMeasures(s.type, s.price, series.open_time(s.bar_index), s.confirmed_index - s.bar_index),
        )
        for s in swings
        if in_window(s.confirmed_index, last, report_bars)
    ]


def break_events(series: BarSeries, breaks: Sequence[Break], last: int, report_bars: int) -> list[Event]:
    return [
        Event(
            "structure_break", PRICE_STRUCTURE, series.tf, series.open_time(b.bar_index), last - b.bar_index,
            StructureBreakMeasures(b.side, b.break_kind, b.swing.price, b.close_beyond_atr, b.displacement_mult),
        )
        for b in breaks
        if in_window(b.bar_index, last, report_bars)
    ]


def volume_spike_events(series: BarSeries, last: int, report_bars: int, lookback: int, mult: float) -> list[Event]:
    volumes = [b.volume if b is not None else None for b in series.bars]
    events = []
    for i in range(max(0, last - report_bars + 1), last + 1):
        volume = volumes[i]
        mean = trailing_mean(volumes, i, lookback)
        if volume is None or not mean:
            continue
        ratio = volume / mean
        if ratio >= mult:
            events.append(Event("volume_spike", PRICE_STRUCTURE, series.tf, series.open_time(i), last - i, VolumeSpikeMeasures(ratio)))
    return events


def state_change_events(
    series: BarSeries,
    states: Sequence[str | None],
    raw: Sequence[float | None],
    last: int,
    report_bars: int,
    kind: str,
) -> list[Event]:
    """`kind`는 efficiency / volatility. 이웃한 두 봉의 상태가 모두 있고 서로 다를 때만 발생한다.

    shock으로 바뀌는 효율성 변화는 `shock_start`와 같은 사실이므로 내지 않는다. shock 종료는 낸다(A.8.3).
    """
    events = []
    for i in range(max(1, last - report_bars + 1), last + 1):
        before, after = states[i - 1], states[i]
        if before is None or after is None or before == after:
            continue
        if kind == "efficiency" and after == SHOCK:
            continue
        if kind == "efficiency":
            measures: Measures = EfficiencyChangeMeasures(before, after, raw[i])
        else:
            measures = VolatilityChangeMeasures(before, after, raw[i])
        events.append(Event(f"{kind}_state_change", REGIME, series.tf, series.open_time(i), last - i, measures))
    return events


def shock_events(series: BarSeries, starts: Sequence[ShockStart], last: int, report_bars: int) -> list[Event]:
    return [
        Event(
            "shock_start", REGIME, series.tf, series.open_time(s.bar_index), last - s.bar_index,
            ShockStartMeasures(s.trigger, s.wick_ratio, s.range_atr),
        )
        for s in starts
        if in_window(s.bar_index, last, report_bars)
    ]


def level_events(
    series: BarSeries,
    levels: Sequence[Level],
    last: int,
    report_bars: int,
    atr: float,
    ref_price: float,
    zone_width: float,
) -> list[Event]:
    """A.8.3 레벨 이벤트. 기준 시각의 레벨 목록을 쓰되, 평가 봉마다 그때 알려진 구성원으로 zone을 다시 만든다."""
    events = []
    for i in range(max(1, last - report_bars + 1), last + 1):
        bar, prev = series.bars[i], series.bars[i - 1]
        if bar is None or prev is None:
            continue
        for level in levels:
            kept = members_known_at(level, bar.open_time, bar.close_time, i == last)
            if not kept:
                continue
            lv = build_level(kept, zone_width, atr, ref_price)
            measures = _level_measures(level.level_id, lv, prev.close, bar.close, bar.high, bar.low, atr)
            if measures is not None:
                name, m = measures
                events.append(Event(name, LEVEL, series.tf, bar.open_time, last - i, m))
    return events


def _side(price: float, lv: Level) -> str:
    if price < lv.zone_low:
        return "below"
    if price > lv.zone_high:
        return "above"
    return "inside"


def _level_measures(
    level_id: str, lv: Level, prev_close: float, close: float, high: float, low: float, atr: float
) -> tuple[str, Measures] | None:
    before, after = _side(prev_close, lv), _side(close, lv)
    if before != "inside" and after == "inside":
        return "level_close_into_zone", LevelCloseIntoMeasures(level_id, lv.center, lv.source_count)
    if {before, after} == {"below", "above"}:
        beyond = (close - lv.zone_high) if after == "above" else (lv.zone_low - close)
        return "level_close_through_zone", LevelCloseThroughMeasures(level_id, lv.center, beyond / atr, lv.source_count)
    if before == after == "below" and high >= lv.zone_low:
        return "level_wick_into_zone", LevelWickMeasures(level_id, lv.center, (high - lv.zone_low) / atr, lv.source_count)
    if before == after == "above" and low <= lv.zone_high:
        return "level_wick_into_zone", LevelWickMeasures(level_id, lv.center, (lv.zone_high - low) / atr, lv.source_count)
    return None
