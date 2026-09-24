"""계산 조립: 저장소 조회 → TF별 지표·구조·레짐 → 파생 → 레벨 → 이벤트.

`compute`는 데이터 출처를 모른다(3.3). 저장소의 조회 인터페이스로 마감 봉만 읽는다.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone

from coindata.compute import derivatives as deriv
from coindata.compute import events as ev
from coindata.compute.indicators import Candle, atr, candle, efficiency_ratio, parkinson, rolling_percentile
from coindata.compute.levels import (
    LevelsResult,
    WindowStats,
    candidates,
    levels,
    window_stats,
)
from coindata.compute.regime import SHOCK, duration, efficiency_state, shock, volatility_state
from coindata.compute.series import BarSeries, Measured, measure, parse_tf, synthesize
from coindata.compute.structure import Retracement, analyze_structure, retracement
from coindata.compute.zigzag import ZigzagResult, zigzag
from coindata.config import Config
from coindata.models import MINUTE_MS, Dataset, Kline, LatestMetric, MetricsRow, PremiumKline
from coindata.store import query

METRICS_STEP_MS = Dataset.METRICS_5M.interval_ms


class ComputeError(Exception):
    """계산에 필요한 최소 데이터(마감 1분봉)가 없다."""


@dataclass(frozen=True, slots=True)
class ComputeInput:
    symbol: str
    anchor_ms: int  # A.1.8
    ref_time: int  # A.1.2
    klines: tuple[Kline, ...]  # [anchor, ref_time) 마감 1분봉
    premium: tuple[PremiumKline, ...]
    premium_start: int
    metrics: tuple[MetricsRow, ...]  # 4분면 계산 구간, ts <= ref_time
    latest_metrics: tuple[LatestMetric, ...]  # 컬럼별 최신값, ts <= ref_time

    @property
    def ref_price(self) -> float:
        return self.klines[-1].close


@dataclass(frozen=True, slots=True)
class CandleRow:
    bar_time: int
    candle: Candle | None  # 부재 봉이면 None
    missing_ratio: float


@dataclass(frozen=True, slots=True)
class TfAnalysis:
    tf: str
    series: BarSeries
    last_index: int  # 마지막 마감 봉. 봉이 없으면 −1
    atr: Measured
    parkinson: Measured
    parkinson_pct: Measured
    er: Measured
    efficiency_state: str | None  # shock 활성 시 "shock"
    efficiency_duration: int | None
    volatility_state: str | None
    volatility_duration: int | None
    shock_active: bool | None  # shock 판정 대상 TF가 아니면 None
    zigzag: ZigzagResult
    broken: frozenset[int]
    structure_state: str
    retracement: Retracement | None
    tentative_distance_atr: float | None
    candles: tuple[CandleRow, ...]  # 최근 K개 마감 봉, 시간순
    events: tuple[ev.Event, ...]


@dataclass(frozen=True, slots=True)
class DerivativesResult:
    premium: deriv.PremiumResult
    metrics_ts: int | None  # ts <= ref_time인 가장 최근 metrics 행
    quadrants: tuple[deriv.QuadrantPoint, ...]  # 기간별 현재 4분면
    metric_values: tuple[LatestMetric, ...]


@dataclass(frozen=True, slots=True)
class Analysis:
    symbol: str
    anchor_ms: int
    ref_time: int
    ref_price: float
    timeframes: tuple[TfAnalysis, ...]  # indicators.timeframes 순서
    window: WindowStats
    levels: LevelsResult
    derivatives: DerivativesResult
    events: tuple[ev.Event, ...]  # 전체 이벤트, bar_time 순


# ---------------------------------------------------------------------------
# 조회
# ---------------------------------------------------------------------------


def resolve_anchor(conn: sqlite3.Connection, config: Config) -> int | None:
    """A.1.8. 설정값이 있으면 그 날짜 00:00 UTC, 없으면 저장소의 첫 1분봉 시각."""
    if config.compute.anchor_time:
        day = date.fromisoformat(config.compute.anchor_time)
        return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp()) * 1000
    bounds = query.time_bounds(conn, Dataset.KLINE_1M, config.data.symbol)
    return bounds.start_ms if bounds else None


def load_input(conn: sqlite3.Connection, config: Config) -> ComputeInput:
    symbol = config.data.symbol
    bounds = query.time_bounds(conn, Dataset.KLINE_1M, symbol)
    anchor = resolve_anchor(conn, config)
    if bounds is None or anchor is None:
        raise ComputeError("저장된 1분봉이 없다")
    ref_time = bounds.end_ms + MINUTE_MS
    klines = query.klines_between(conn, symbol, anchor, ref_time)
    if not klines:
        raise ComputeError("시작점 이후 저장된 1분봉이 없다")
    premium_start = premium_load_start(config, ref_time)
    premium = query.premium_between(conn, symbol, premium_start, ref_time)
    metrics = query.metrics_between(conn, symbol, metrics_load_start(config, ref_time), ref_time)
    latest = query.latest_metrics(conn, symbol, ref_time)
    return ComputeInput(
        symbol, anchor, ref_time, tuple(klines), tuple(premium), premium_start, tuple(metrics), tuple(latest)
    )


def premium_load_start(config: Config, ref_time: int) -> int:
    """평활 백분위와 보고 기간, 변화량 창을 채우는 가장 이른 시각(평활 TF 경계)."""
    p = config.derivatives.premium
    tf_ms = parse_tf(p.smoothing_tf)
    bars = p.pct_lookback + config.events.report_bars[p.smoothing_tf] + 1
    start = (ref_time // tf_ms - bars) * tf_ms
    longest = max((parse_tf(w) for w in p.windows), default=0)
    return min(start, (ref_time - longest - MINUTE_MS) // tf_ms * tf_ms)


def metrics_load_start(config: Config, ref_time: int) -> int:
    longest = max((parse_tf(p) for p in config.derivatives.quadrant.periods), default=0)
    report = config.events.quadrant_change.report_minutes * MINUTE_MS
    return ref_time - (report + longest + METRICS_STEP_MS)


# ---------------------------------------------------------------------------
# 계산
# ---------------------------------------------------------------------------


def analyze(inp: ComputeInput, config: Config) -> Analysis:
    ref_price = inp.ref_price
    wanted = list(config.indicators.timeframes)
    # 모든 경로 의존 계산(ATR, ZigZag, 구조·broken, shock, 레짐 지속)은 고정 시작점부터의 전체 봉으로 한다(A.1.8).
    per_tf = {tf: _analyze_tf(synthesize(tf, inp.klines, inp.anchor_ms, inp.ref_time), config, ref_price) for tf in wanted}

    stats = window_stats(inp.klines, inp.ref_time, config.levels.vwap_window_minutes, config.levels.range_window_minutes)
    lv = config.levels
    members = candidates(
        [(tf, per_tf[tf].zigzag.swings, per_tf[tf].broken) for tf in lv.swing_timeframes], stats, lv.swing_count
    )
    level_result = levels(members, per_tf[lv.normalize_tf].atr.value, ref_price, lv.merge_dist, lv.zone_width, lv.report_each_side)

    events: list[ev.Event] = []
    results = []
    for tf in wanted:
        a = per_tf[tf]
        tf_events = list(a.events)
        if tf in config.events.level.timeframes and level_result.atr is not None:
            tf_events += ev.level_events(
                a.series, level_result.reported, a.last_index, config.events.report_bars[tf],
                level_result.atr, ref_price, lv.zone_width,
            )
        tf_events.sort(key=lambda e: (e.bar_time, e.type))
        a = replace(a, events=tuple(tf_events))
        results.append(a)
        events += tf_events

    derivatives, deriv_events = _derivatives(inp, config)
    events += deriv_events
    events.sort(key=lambda e: (e.bar_time, e.tf, e.type))
    return Analysis(
        inp.symbol, inp.anchor_ms, inp.ref_time, ref_price, tuple(results), stats, level_result, derivatives, tuple(events)
    )


def _analyze_tf(series: BarSeries, config: Config, ref_price: float) -> TfAnalysis:
    ind, reg = config.indicators, config.regime
    bars = series.bars
    last = len(bars) - 1
    atr_values = atr(bars, ind.atr.n)
    park = parkinson(bars, ind.parkinson.n)
    pct = rolling_percentile(park, ind.parkinson.pct_lookback)
    er = efficiency_ratio(bars, reg.er.n)
    candles = [
        candle(b, atr_values[i - 1] if i > 0 else None) if b is not None else None for i, b in enumerate(bars)
    ]
    zz = zigzag(series, atr_values, ind.zigzag.k)
    st = analyze_structure(
        series, zz.swings, atr_values, ind.structure.displacement_mult, ind.structure.displacement_lookback
    )
    is_shock_tf = series.tf in reg.shock.timeframes
    sh = shock(candles, st.breaks, reg) if is_shock_tf else None
    eff = [
        SHOCK if sh is not None and sh.active[i] else efficiency_state(x, reg.er.trend, reg.er.range)
        for i, x in enumerate(er)
    ]
    vol = [volatility_state(p, reg.vol.high, reg.vol.low) for p in pct]

    atr_now = atr_values[last] if last >= 0 else None
    tentative = None
    if zz.tentative is not None and atr_now:
        tentative = (ref_price - zz.tentative.price) / atr_now

    k = ind.candle.report_bars
    candle_rows = tuple(
        CandleRow(series.open_time(i), candles[i], bars[i].missing_ratio if bars[i] is not None else 1.0)  # type: ignore[union-attr]
        for i in range(max(0, last - k + 1), last + 1)
    )

    report_bars = config.events.report_bars.get(series.tf, 0)
    tf_events: list[ev.Event] = []
    tf_events += ev.swing_events(series, zz.swings, last, report_bars)
    tf_events += ev.break_events(series, st.breaks, last, report_bars)
    vs = config.events.volume_spike
    if series.tf in vs.timeframes:
        tf_events += ev.volume_spike_events(series, last, report_bars, vs.lookback, vs.mult)
    tf_events += ev.state_change_events(series, eff, er, last, report_bars, "efficiency")
    tf_events += ev.state_change_events(series, vol, pct, last, report_bars, "volatility")
    if sh is not None:
        tf_events += ev.shock_events(series, sh.starts, last, report_bars)

    p_n = ind.parkinson.n
    return TfAnalysis(
        tf=series.tf,
        series=series,
        last_index=last,
        atr=measure(series, last, ind.atr.n, atr_now),
        parkinson=measure(series, last, p_n, park[last] if last >= 0 else None),
        parkinson_pct=measure(series, last, p_n + ind.parkinson.pct_lookback - 1, pct[last] if last >= 0 else None),
        er=measure(series, last, reg.er.n + 1, er[last] if last >= 0 else None),
        efficiency_state=eff[last] if last >= 0 else None,
        efficiency_duration=duration(eff, last),
        volatility_state=vol[last] if last >= 0 else None,
        volatility_duration=duration(vol, last),
        shock_active=(sh.active[last] if last >= 0 else False) if sh is not None else None,
        zigzag=zz,
        broken=st.broken,
        structure_state=st.states[last] if last >= 0 else "insufficient",
        retracement=retracement(zz.swings, ref_price, last) if last >= 0 else None,
        tentative_distance_atr=tentative,
        candles=candle_rows,
        events=tuple(tf_events),
    )


def _derivatives(inp: ComputeInput, config: Config) -> tuple[DerivativesResult, list[ev.Event]]:
    d = config.derivatives
    windows = [(w, parse_tf(w)) for w in d.premium.windows]
    smoothing_ms = parse_tf(d.premium.smoothing_tf)
    prem = deriv.premium(inp.premium, inp.ref_time, windows, smoothing_ms, d.premium.pct_lookback, inp.premium_start)

    events: list[ev.Event] = []
    smoothed = prem.smoothed
    last = len(smoothed) - 1
    high, low = config.events.premium_extreme.high, config.events.premium_extreme.low
    report_bars = config.events.report_bars[d.premium.smoothing_tf]

    def extreme(p: float) -> str | None:
        return "high" if p >= high else ("low" if p <= low else None)

    for i in range(max(1, last - report_bars + 1), last + 1):
        now, before = smoothed[i], smoothed[i - 1]
        if now.pct is None or before.pct is None or now.value_bp is None:
            continue
        side = extreme(now.pct)
        if side is not None and extreme(before.pct) is None:
            events.append(
                ev.Event(
                    "premium_extreme", ev.DERIVATIVES, d.premium.smoothing_tf, now.open_time, last - i,
                    ev.PremiumExtremeMeasures(side, now.value_bp, now.pct),
                )
            )

    rows = [r for r in inp.metrics if r.ts <= inp.ref_time]
    oi_by_ts = {r.ts: r.sum_open_interest for r in rows}
    close_by_open = {k.open_time: k.close for k in inp.klines}
    q = d.quadrant
    periods = [(p, parse_tf(p)) for p in q.periods]
    latest_ts = rows[-1].ts if rows else None
    current: list[deriv.QuadrantPoint] = []
    if latest_ts is not None:
        current = [deriv.quadrant_at(latest_ts, p, ms, oi_by_ts, close_by_open, q.oi_band, q.px_band) for p, ms in periods]
        report_ms = config.events.quadrant_change.report_minutes * MINUTE_MS
        for r in rows:
            if inp.ref_time - r.ts >= report_ms:
                continue
            for p, ms in periods:
                now = deriv.quadrant_at(r.ts, p, ms, oi_by_ts, close_by_open, q.oi_band, q.px_band)
                before = deriv.quadrant_at(r.ts - METRICS_STEP_MS, p, ms, oi_by_ts, close_by_open, q.oi_band, q.px_band)
                if now.quadrant is None or before.quadrant is None or now.quadrant == before.quadrant:
                    continue
                events.append(
                    ev.Event(
                        "quadrant_change", ev.DERIVATIVES, "5m", r.ts, (latest_ts - r.ts) // METRICS_STEP_MS,
                        ev.QuadrantChangeMeasures(p, before.quadrant, now.quadrant, now.d_oi, now.d_px),
                    )
                )
    result = DerivativesResult(prem, latest_ts, tuple(current), inp.latest_metrics)
    return result, events
