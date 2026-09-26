"""ATR-ZigZag 스윙 (PRD 부록 A.3.2).

봉마다 고가·저가 이벤트를 1분봉으로 판정한 실제 발생 순서대로 처리한다. 두 극값이 같은 1분봉에서 나오면
동시 이벤트로 보고 확장만 한다. 임계값은 `k × ATR_{t−1}`이며, `ATR_{t−1}`이 없는 봉은 건너뛴다.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from coindata.compute.series import BarSeries

HIGH = "swing_high"
LOW = "swing_low"


@dataclass(frozen=True, slots=True)
class Swing:
    type: str  # swing_high / swing_low
    price: float
    bar_index: int  # 극점 봉
    extreme_time: int  # 극값이 나온 1분봉의 open_time
    confirmed_index: int  # 확정 봉
    known_time: int  # 확정 봉의 close_time + 1


@dataclass(frozen=True, slots=True)
class Tentative:
    dir: str  # up / down
    price: float
    bar_index: int


@dataclass(frozen=True, slots=True)
class ZigzagResult:
    swings: tuple[Swing, ...]
    tentative: Tentative | None


@dataclass(frozen=True, slots=True)
class _Event:
    kind: str  # "H" / "L"
    price: float
    time: int
    bar_index: int


class _Zigzag:
    def __init__(self, series: BarSeries) -> None:
        self.series = series
        self.events: list[_Event] = []
        self.dir = "none"
        self.hh: int | None = None  # 초기 단계의 최고 high 이벤트 인덱스
        self.ll: int | None = None
        self.cand: int | None = None
        self.swings: list[Swing] = []

    # -- 이벤트 처리 --------------------------------------------------------

    def bar(self, index: int, theta: float) -> None:
        b = self.series.bars[index]
        assert b is not None
        high = _Event("H", b.high, b.high_time, index)
        low = _Event("L", b.low, b.low_time, index)
        if b.high_time == b.low_time:
            self.events += [high, low]
            self._extend(len(self.events) - 2)
            self._extend(len(self.events) - 1)
            return
        for event in sorted((high, low), key=lambda e: e.time):
            self.events.append(event)
            self._process(len(self.events) - 1, theta, index)

    def _extend(self, i: int) -> None:
        """확장만 한다(동시 이벤트 규칙)."""
        e = self.events[i]
        if self.dir == "none":
            if e.kind == "H" and (self.hh is None or e.price > self.events[self.hh].price):
                self.hh = i
            if e.kind == "L" and (self.ll is None or e.price < self.events[self.ll].price):
                self.ll = i
        elif self.dir == "up" and e.kind == "H" and e.price > self.events[self.cand].price:  # type: ignore[index]
            self.cand = i
        elif self.dir == "down" and e.kind == "L" and e.price < self.events[self.cand].price:  # type: ignore[index]
            self.cand = i

    def _process(self, i: int, theta: float, bar_index: int) -> None:
        e = self.events[i]
        if self.dir == "none":
            self._extend(i)
            if e.kind == "H" and self.ll is not None:
                low = self.events[self.ll]
                if low.time < e.time and e.price - low.price >= theta:
                    self._confirm(self.ll, i, theta, bar_index)
            elif e.kind == "L" and self.hh is not None:
                high = self.events[self.hh]
                if high.time < e.time and high.price - e.price >= theta:
                    self._confirm(self.hh, i, theta, bar_index)
            return
        cand = self.events[self.cand]  # type: ignore[index]
        if self.dir == "up":
            if e.kind == "H":
                self._extend(i)
            elif cand.price - e.price >= theta:
                self._confirm(self.cand, i, theta, bar_index)  # type: ignore[arg-type]
        else:
            if e.kind == "L":
                self._extend(i)
            elif e.price - cand.price >= theta:
                self._confirm(self.cand, i, theta, bar_index)  # type: ignore[arg-type]

    def _confirm(self, extreme: int, current: int, theta: float, bar_index: int) -> None:
        """극점을 확정하고, 후보를 재설정한 뒤 재검사를 반복한다(A.3.2의 4).

        이벤트는 시간순으로 쌓이고 확정 극점의 시각은 매번 뒤로만 가므로 반복은 끝난다.
        """
        known_time = self.series.bars[bar_index].close_time + 1  # type: ignore[union-attr]
        while True:
            e = self.events[extreme]
            self.swings.append(Swing(HIGH if e.kind == "H" else LOW, e.price, e.bar_index, e.time, bar_index, known_time))
            self.dir = "down" if e.kind == "H" else "up"
            cand = self._most_extreme("L" if e.kind == "H" else "H", e.time, current)
            assert cand is not None, "확정 직후에는 반대쪽 극값이 항상 있다"
            self.cand = cand
            candidate = self.events[cand]
            x = self._most_extreme(e.kind, candidate.time, current)
            if x is None:
                return
            move = candidate.price - self.events[x].price if candidate.kind == "H" else self.events[x].price - candidate.price
            if move < theta:
                return
            extreme = cand

    def _most_extreme(self, kind: str, after_time: int, until: int) -> int | None:
        """`after_time`보다 뒤에 발생해 이벤트 `until`까지 처리된 `kind` 극값 중 가장 극단적인 것.

        같은 값이 여럿이면 가장 이른 것을 고른다. 이벤트 목록은 시간순이므로 뒤에서부터 보다가 멈춘다.
        """
        best: int | None = None
        for j in range(until, -1, -1):
            e = self.events[j]
            if e.time <= after_time:
                break
            if e.kind != kind:
                continue
            if best is None:
                best = j
                continue
            b = self.events[best]
            if (e.price > b.price if kind == "H" else e.price < b.price) or e.price == b.price:
                best = j  # 뒤에서부터 보므로 같은 값이면 더 이른 쪽으로 바꾼다
        return best


def zigzag(series: BarSeries, atr_values: Sequence[float | None], k: float) -> ZigzagResult:
    engine = _Zigzag(series)
    for i in range(1, len(series.bars)):
        prev_atr = atr_values[i - 1]
        if series.bars[i] is None or prev_atr is None:
            continue
        engine.bar(i, k * prev_atr)
    tentative = None
    if engine.dir != "none" and engine.cand is not None:
        c = engine.events[engine.cand]
        tentative = Tentative(engine.dir, c.price, c.bar_index)
    return ZigzagResult(tuple(engine.swings), tentative)
