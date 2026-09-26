"""체결 흐름 (PRD 부록 A.12, 계열 `flow`).

1분봉의 taker 매수 체결량으로 계산한다. 델타는 공격적으로 체결한 쪽의 방향만 나타내며, 미결제약정 증감의
롱·숏 구성은 나타내지 않는다. 1분봉 거래량을 전체 거래량으로 서술하지 않는다(D-7).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from coindata.compute.indicators import rolling_percentile
from coindata.compute.series import Bar, BarSeries, Measured, measure


def deltas(bars: Sequence[Bar | None]) -> list[float | None]:
    """봉별 `delta = taker_buy − taker_sell = 2 × taker_buy − volume`."""
    return [2 * b.taker_buy_volume - b.volume if b is not None else None for b in bars]


def imbalances(bars: Sequence[Bar | None]) -> list[float | None]:
    """봉별 `delta / volume`. volume이 0이면 None(A.1.4)."""
    return [(2 * b.taker_buy_volume - b.volume) / b.volume if b is not None and b.volume else None for b in bars]


def ema(values: Sequence[float | None], n: int) -> list[float | None]:
    """A.12 델타 EMA. 첫 값은 첫 `n`개의 산술평균, 이후 `α = 2/(n+1)`. None을 만나면 다시 시작한다."""
    out: list[float | None] = [None] * len(values)
    alpha = 2 / (n + 1)
    seed: list[float] = []
    value: float | None = None
    for i, x in enumerate(values):
        if x is None:
            seed, value = [], None
            continue
        if value is None:
            seed.append(x)
            if len(seed) == n:
                value = sum(seed) / n
                out[i] = value
        else:
            value = value + alpha * (x - value)
            out[i] = value
    return out


@dataclass(frozen=True, slots=True)
class FlowResult:
    bar_time: int | None  # 마지막 마감 봉
    taker_buy: float | None
    taker_sell: float | None
    delta: float | None
    imbalance: Measured
    imbalance_pct: Measured
    delta_ema: Measured


def flow(series: BarSeries, ema_n: int, pct_lookback: int) -> FlowResult:
    bars = series.bars
    last = len(bars) - 1
    delta_values = deltas(bars)
    imbalance_values = imbalances(bars)
    ema_values = ema(delta_values, ema_n)
    pct_values = rolling_percentile(imbalance_values, pct_lookback)
    bar = bars[last] if last >= 0 else None
    if bar is None:
        empty = measure(series, last, 1, None)
        return FlowResult(
            series.open_time(last) if last >= 0 else None, None, None, None, empty,
            measure(series, last, pct_lookback, None), measure(series, last, ema_n, None),
        )
    return FlowResult(
        bar_time=bar.open_time,
        taker_buy=bar.taker_buy_volume,
        taker_sell=bar.volume - bar.taker_buy_volume,
        delta=delta_values[last],
        imbalance=measure(series, last, 1, imbalance_values[last]),
        # 창에 부재 봉이 없는데 값이 없으면 volume 0 봉 때문이며, measure가 zero_denominator로 적는다.
        imbalance_pct=measure(series, last, pct_lookback, pct_values[last]),
        delta_ema=measure(series, last, ema_n, ema_values[last]),
    )

