"""구조 상태, 돌파와 BOS/MSS, 되돌림 (PRD 부록 A.3.3 ~ A.3.5)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from coindata.compute.indicators import trailing_mean
from coindata.compute.series import BarSeries
from coindata.compute.zigzag import HIGH, LOW, Swing

HHHL = "higher_highs_higher_lows"
LHLL = "lower_highs_lower_lows"
MIXED = "mixed"
INSUFFICIENT = "insufficient"

ABOVE = "above_swing_high"
BELOW = "below_swing_low"


def structure_state(swings: Sequence[Swing]) -> str:
    """A.3.3. `swings`는 확정 순서대로다."""
    highs = [s.price for s in swings if s.type == HIGH]
    lows = [s.price for s in swings if s.type == LOW]
    if len(highs) < 2 or len(lows) < 2:
        return INSUFFICIENT
    if highs[-1] > highs[-2] and lows[-1] > lows[-2]:
        return HHHL
    if highs[-1] < highs[-2] and lows[-1] < lows[-2]:
        return LHLL
    return MIXED


@dataclass(frozen=True, slots=True)
class Break:
    bar_index: int
    side: str  # above_swing_high / below_swing_low
    break_kind: str  # BOS / MSS / break_no_displacement / break_unclassified
    swing: Swing
    close_beyond_atr: float | None
    displacement_mult: float | None
    structure_state: str


@dataclass(frozen=True, slots=True)
class StructureResult:
    states: tuple[str, ...]  # 봉마다 그 봉 마감 시점의 구조 상태
    breaks: tuple[Break, ...]
    broken: frozenset[int]  # 돌파된 스윙의 swings 인덱스


def _classify(side: str, state: str, displaced: bool) -> str:
    """A.3.4 분류표."""
    if state in (MIXED, INSUFFICIENT):
        return "break_unclassified"
    same_direction = (side == ABOVE and state == HHHL) or (side == BELOW and state == LHLL)
    if same_direction:
        return "BOS"
    return "MSS" if displaced else "break_no_displacement"


def analyze_structure(
    series: BarSeries,
    swings: Sequence[Swing],
    atr_values: Sequence[float | None],
    displacement_mult: float,
    displacement_lookback: int,
) -> StructureResult:
    """봉을 순서대로 보며 구조 상태를 갱신하고 돌파를 판정한다.

    - 구조 상태는 그 봉에서 확정된 스윙까지 반영한 값이다(새 스윙이 확정될 때만 바뀐다).
    - 돌파 대상은 확정된 봉의 다음 봉부터, 아직 돌파되지 않은 가장 최근 스윙 고점·저점이다.
    """
    bars = series.bars
    bodies = [abs(b.close - b.open) if b is not None else None for b in bars]
    by_confirmation: dict[int, list[int]] = {}
    for i, swing in enumerate(swings):
        by_confirmation.setdefault(swing.confirmed_index, []).append(i)

    states: list[str] = []
    breaks: list[Break] = []
    broken: set[int] = set()
    known: list[int] = []  # 이미 확정된 스윙(확정 봉 이전)
    state = INSUFFICIENT
    for t, bar in enumerate(bars):
        prev = bars[t - 1] if t > 0 else None
        if bar is not None and prev is not None:
            for side in (ABOVE, BELOW):
                target = _latest_unbroken(swings, known, broken, HIGH if side == ABOVE else LOW)
                if target is None:
                    continue
                level = swings[target].price
                crossed = (prev.close <= level < bar.close) if side == ABOVE else (prev.close >= level > bar.close)
                if not crossed:
                    continue
                broken.add(target)
                mean_body = trailing_mean(bodies, t, displacement_lookback)
                mult = abs(bar.close - bar.open) / mean_body if mean_body else None
                prev_atr = atr_values[t - 1]
                beyond = abs(bar.close - level) / prev_atr if prev_atr else None
                displaced = mult is not None and mult >= displacement_mult
                current_state = structure_state([swings[i] for i in known + by_confirmation.get(t, [])])
                breaks.append(Break(t, side, _classify(side, current_state, displaced), swings[target], beyond, mult, current_state))
        known += by_confirmation.get(t, [])
        if t in by_confirmation:
            state = structure_state([swings[i] for i in known])
        states.append(state)
    return StructureResult(tuple(states), tuple(breaks), frozenset(broken))


def _latest_unbroken(swings: Sequence[Swing], known: Sequence[int], broken: set[int], kind: str) -> int | None:
    for i in reversed(known):
        if swings[i].type == kind and i not in broken:
            return i
    return None


@dataclass(frozen=True, slots=True)
class Retracement:
    depth: float | None
    time_ratio: float | None


def retracement(swings: Sequence[Swing], ref_price: float, current_index: int) -> Retracement | None:
    """A.3.5. 가장 최근 확정 스윙 2개(서로 반대 유형)로 계산한다. 스윙이 모자라면 None."""
    if len(swings) < 2:
        return None
    a, b = swings[-2], swings[-1]
    span = b.price - a.price if b.type == HIGH else a.price - b.price
    depth = ((b.price - ref_price) if b.type == HIGH else (ref_price - b.price)) / span if span else None
    bars_between = b.bar_index - a.bar_index
    time_ratio = (current_index - b.bar_index) / bars_between if bars_between else None
    return Retracement(depth, time_ratio)
