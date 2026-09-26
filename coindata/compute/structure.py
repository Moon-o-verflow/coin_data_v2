"""구조 상태, 돌파와 BOS/MSS, 되돌림 (PRD 부록 A.3.3 ~ A.3.5)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from coindata.compute.indicators import trailing_mean
from coindata.compute.series import BarSeries
from coindata.compute.zigzag import HIGH, LOW, Swing, Tentative

HHHL = "higher_highs_higher_lows"
LHLL = "lower_highs_lower_lows"
MIXED = "mixed"
INSUFFICIENT = "insufficient"

ABOVE = "above_swing_high"
BELOW = "below_swing_low"


HIGHER = "higher"
LOWER = "lower"
EQUAL = "equal"


@dataclass(frozen=True, slots=True)
class StructureState:
    state: str
    high_relation: str | None  # higher / lower / equal. 스윙이 모자라거나 ATR이 없으면 None
    low_relation: str | None


def relation(earlier: Swing, later: Swing, atr_values: Sequence[float | None], tol_atr: float) -> str | None:
    """A.3.3 관계 판정. 허용 오차는 나중 스윙의 확정 봉 직전 봉 ATR 기준이다."""
    c = later.confirmed_index
    atr = atr_values[c - 1] if c > 0 else None
    if atr is None:
        return None
    diff = later.price - earlier.price
    if abs(diff) < tol_atr * atr:
        return EQUAL
    return HIGHER if diff > 0 else LOWER


def structure_state(swings: Sequence[Swing], atr_values: Sequence[float | None], tol_atr: float) -> StructureState:
    """A.3.3. `swings`는 확정 순서대로다."""
    highs = [s for s in swings if s.type == HIGH]
    lows = [s for s in swings if s.type == LOW]
    high_rel = relation(highs[-2], highs[-1], atr_values, tol_atr) if len(highs) >= 2 else None
    low_rel = relation(lows[-2], lows[-1], atr_values, tol_atr) if len(lows) >= 2 else None
    if high_rel is None or low_rel is None:
        return StructureState(INSUFFICIENT, high_rel, low_rel)
    if high_rel == HIGHER and low_rel == HIGHER:
        return StructureState(HHHL, high_rel, low_rel)
    if high_rel == LOWER and low_rel == LOWER:
        return StructureState(LHLL, high_rel, low_rel)
    return StructureState(MIXED, high_rel, low_rel)


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
    equal_tol_atr: float,
) -> StructureResult:
    """봉을 순서대로 보며 구조 상태를 갱신하고 돌파를 판정한다.

    - 구조 상태는 그 봉에서 확정된 스윙까지 반영한 값이다(새 스윙이 확정될 때만 바뀐다).
    - 돌파 대상은 유형별로 최대 하나다. 가장 최근 확정 스윙이 확정 봉의 다음 봉부터 대상이 되고,
      돌파되면 그쪽 대상은 없음이 된다. 같은 유형의 새 스윙이 확정되면 그것으로 바뀐다(A.3.4).
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
    targets: dict[str, int | None] = {HIGH: None, LOW: None}
    state = INSUFFICIENT
    for t, bar in enumerate(bars):
        prev = bars[t - 1] if t > 0 else None
        if bar is not None and prev is not None:
            for side in (ABOVE, BELOW):
                kind = HIGH if side == ABOVE else LOW
                target = targets[kind]
                if target is None:
                    continue
                level = swings[target].price
                crossed = (prev.close <= level < bar.close) if side == ABOVE else (prev.close >= level > bar.close)
                if not crossed:
                    continue
                broken.add(target)
                targets[kind] = None
                mean_body = trailing_mean(bodies, t, displacement_lookback)
                mult = abs(bar.close - bar.open) / mean_body if mean_body else None
                prev_atr = atr_values[t - 1]
                beyond = abs(bar.close - level) / prev_atr if prev_atr else None
                displaced = mult is not None and mult >= displacement_mult
                current_state = structure_state(
                    [swings[i] for i in known + by_confirmation.get(t, [])], atr_values, equal_tol_atr
                ).state
                breaks.append(Break(t, side, _classify(side, current_state, displaced), swings[target], beyond, mult, current_state))
        for i in by_confirmation.get(t, []):
            targets[swings[i].type] = i
        known += by_confirmation.get(t, [])
        if t in by_confirmation:
            state = structure_state([swings[i] for i in known], atr_values, equal_tol_atr).state
        states.append(state)
    return StructureResult(tuple(states), tuple(breaks), frozenset(broken))


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


def retracement_tentative(swings: Sequence[Swing], tentative: Tentative | None, ref_price: float) -> float | None:
    """A.3.5 진행 파동 기준 깊이: 마지막 확정 스윙 `P_b`에서 잠정 후보 `C`까지의 파동."""
    if not swings or tentative is None:
        return None
    base, c = swings[-1].price, tentative.price
    if c == base:
        return None
    if tentative.dir == "up":
        return (c - ref_price) / (c - base)
    return (ref_price - c) / (base - c)
