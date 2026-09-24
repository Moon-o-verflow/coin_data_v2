"""레짐: 효율성 축, 변동성 축, shock (PRD 부록 A.4). 두 축을 하나의 레이블로 합성하지 않는다(R-7)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from coindata.compute.indicators import Candle
from coindata.compute.structure import ABOVE, BELOW, Break
from coindata.config import RegimeConfig


def efficiency_state(er: float | None, trend: float, range_: float) -> str | None:
    if er is None:
        return None
    if er >= trend:
        return "trend"
    if er <= range_:
        return "range"
    return "transition"


def volatility_state(pct: float | None, high: float, low: float) -> str | None:
    if pct is None:
        return None
    if pct >= high:
        return "expansion"
    if pct <= low:
        return "compression"
    return "normal"


@dataclass(frozen=True, slots=True)
class ShockStart:
    bar_index: int
    trigger: str  # wick / rapid_reversal / both
    wick_ratio: float | None
    range_atr: float | None


@dataclass(frozen=True, slots=True)
class ShockResult:
    active: tuple[bool, ...]
    starts: tuple[ShockStart, ...]


def shock(candles: Sequence[Candle | None], breaks: Sequence[Break], config: RegimeConfig) -> ShockResult:
    """A.4.4. 발동 봉을 포함해 `duration_bars`개 봉 동안 활성이며, 활성 중 재발동하면 연장한다."""
    settings = config.shock
    above: dict[int, bool] = {b.bar_index: True for b in breaks if b.side == ABOVE}
    below: dict[int, bool] = {b.bar_index: True for b in breaks if b.side == BELOW}
    active = [False] * len(candles)
    starts: list[ShockStart] = []
    active_until = -1
    for t, c in enumerate(candles):
        wick = None
        wick_hit = False
        if c is not None and c.upper_wick_ratio is not None and c.lower_wick_ratio is not None:
            wick = max(c.upper_wick_ratio, c.lower_wick_ratio)
            wick_hit = wick >= settings.wick_th and c.range_atr is not None and c.range_atr >= settings.range_atr_th
        reversal_hit = _rapid_reversal(t, above, below, settings.gap_bars)
        if wick_hit or reversal_hit:
            if t > active_until:
                trigger = "both" if wick_hit and reversal_hit else ("wick" if wick_hit else "rapid_reversal")
                starts.append(ShockStart(t, trigger, wick, c.range_atr if c is not None else None))
            active_until = t + settings.duration_bars - 1
        active[t] = t <= active_until
    return ShockResult(tuple(active), tuple(starts))


def _rapid_reversal(t: int, above: dict[int, bool], below: dict[int, bool], gap: int) -> bool:
    """봉 t에서 한쪽 돌파가 났고, `gap`봉 이내 이전에 반대쪽 돌파가 있었다(늦은 쪽이 봉 t)."""
    if t in above and any(t - g in below for g in range(0, gap + 1)):
        return True
    if t in below and any(t - g in above for g in range(0, gap + 1)):
        return True
    return False


def duration(states: Sequence[str | None], end: int) -> int | None:
    """A.4.3. `end`까지 같은 상태가 연속된 봉 수(현재 봉 포함). None을 만나면 멈춘다."""
    current = states[end] if 0 <= end < len(states) else None
    if current is None:
        return None
    count = 0
    for i in range(end, -1, -1):
        if states[i] != current:
            break
        count += 1
    return count
