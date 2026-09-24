"""요약 JSON 조립 (PRD FR-4.1 ~ FR-4.8).

지표를 계산하지 않는다. `compute`의 결과를 섹션별로 옮기고, 직렬화 단계에서만 반올림한다(A.1.5).
방향을 판정하거나 확률을 만들지 않는다(R-1, R-2). 값이 없으면 `null`과 사유를 싣는다(FR-4.2).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from coindata.compute import events as ev
from coindata.compute.engine import Analysis, TfAnalysis
from coindata.compute.levels import Level
from coindata.compute.series import Measured
from coindata.config import Config, ReportConfig
from coindata.models import (
    MINUTE_MS,
    Dataset,
    FundingInfo,
    Kline,
    LatestMetric,
    OpenGap,
    SummaryRecord,
    SummaryTrigger,
)

SUMMARY_SCHEMA_VERSION = "1"
NOT_AVAILABLE_AT_REF_TIME = "not_available_at_ref_time"
REST_FAILED = "rest_failed"
ABSENT_BAR = "absent_bar"
BP = 10_000
PERCENT = 100

UNAVAILABLE: tuple[tuple[str, str], ...] = (
    ("liquidation", "source_unavailable"),
    ("trade_based_indicators", "not_implemented"),
    ("statistics", "not_implemented"),
)
RATIO_FIELDS = ("top_position_ratio", "top_account_ratio", "global_account_ratio", "taker_buy_sell_ratio")

# 이벤트 측정값의 자릿수 종류. 여기에 없는 실수는 비율 자릿수를 쓴다.
_MEASURE_KIND = {"price": "price", "swing_price": "price", "level_center": "price", "value_bp": "bp", "pct": "pct"}
# 4분면 변화율은 값이 작아 백분율로 싣는다(FR-4.1 표기 규칙).
_MEASURE_PERCENT = {"d_oi": "d_oi_percent", "d_px": "d_px_percent"}


@dataclass(frozen=True, slots=True)
class DatasetLast:
    dataset: Dataset
    covered_until: int | None  # 봉은 마지막 봉의 close_time + 1, metrics는 마지막 ts


@dataclass(frozen=True, slots=True)
class SummaryContext:
    """요약 한 건을 만드는 데 필요한 입력. cli가 모은다."""

    summary_id: str
    created_at: int
    trigger: SummaryTrigger
    requested_time: int | None  # 과거 시점 요약의 지정 시각(분 내림)
    analysis: Analysis
    config: Config
    run_time: int | None  # 현재 시점 요약만. 서버 시각 추정값 또는 로컬 시각
    run_time_source: str | None  # server / local
    dataset_last: tuple[DatasetLast, ...]
    current_bar: Kline | None
    funding: FundingInfo | None
    failures: tuple[str, ...]  # 이번 실행의 취득 실패
    gaps: tuple[OpenGap, ...]
    previous: SummaryRecord | None


@dataclass(frozen=True, slots=True)
class BuiltSummary:
    document: dict[str, Any]
    state_json: str
    params_hash: str


# ---------------------------------------------------------------------------
# 표기
# ---------------------------------------------------------------------------


def format_time(ms: int | None) -> str | None:
    """요약 JSON 안의 시각 표기 `YYYY-MM-DDTHH:MMZ` (FR-4.1)."""
    if ms is None:
        return None
    return datetime.fromtimestamp(ms // 1000, timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def summary_id_of(ms: int) -> str:
    """FR-4.5: 생성 시각의 `YYYYMMDDTHHMMSSZ`."""
    return datetime.fromtimestamp(ms // 1000, timezone.utc).strftime("%Y%m%dT%H%M%SZ")


class _Fmt:
    def __init__(self, report: ReportConfig) -> None:
        self._digits = {"price": report.digits_price, "ratio": report.digits_ratio, "bp": report.digits_bp, "pct": report.digits_pct}

    def num(self, value: float | None, kind: str) -> float | None:
        return None if value is None else round(value, self._digits[kind])

    def price(self, value: float | None) -> float | None:
        return self.num(value, "price")

    def ratio(self, value: float | None) -> float | None:
        return self.num(value, "ratio")

    def bp(self, value: float | None) -> float | None:
        return self.num(value, "bp")

    def pct(self, value: float | None) -> float | None:
        return self.num(value, "pct")

    def measured(self, m: Measured, kind: str, scale: float = 1.0) -> dict[str, Any]:
        value = None if m.value is None else m.value * scale
        return {"value": self.num(value, kind), "gap_ratio": self.ratio(m.gap_ratio), "null_reason": m.null_reason}


# ---------------------------------------------------------------------------
# 파라미터와 상태
# ---------------------------------------------------------------------------


def parameters(config: Config, anchor_ms: int) -> dict[str, Any]:
    """FR-6.4: 계산 결과에 영향을 주는 설정과 실제로 쓴 시작점(A.1.8)."""
    return {
        "indicators": dataclasses.asdict(config.indicators),
        "regime": dataclasses.asdict(config.regime),
        "derivatives": dataclasses.asdict(config.derivatives),
        "levels": dataclasses.asdict(config.levels),
        "events": dataclasses.asdict(config.events),
        "anchor_time": format_time(anchor_ms),
    }


def params_hash(params: Mapping[str, Any]) -> str:
    canonical = json.dumps(params, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def current_state(analysis: Analysis) -> dict[str, Any]:
    """FR-4.4 비교 대상: TF별 세 상태와 기간별 4분면."""
    return {
        "timeframes": {
            tf.tf: {
                "efficiency_state": tf.efficiency_state,
                "volatility_state": tf.volatility_state,
                "structure_state": tf.structure_state,
            }
            for tf in analysis.timeframes
        },
        "quadrant": {q.period: q.quadrant for q in analysis.derivatives.quadrants},
    }


def state_changes(previous: Mapping[str, Any], current: Mapping[str, Any]) -> list[dict[str, Any]]:
    """양쪽에 모두 있는 항목만 비교한다. 설정이 바뀌어 한쪽에만 있는 항목은 비교하지 않는다."""
    changes = []
    prev_tfs, cur_tfs = previous.get("timeframes", {}), current["timeframes"]
    for tf, states in cur_tfs.items():
        for key, value in states.items():
            if tf in prev_tfs and key in prev_tfs[tf] and prev_tfs[tf][key] != value:
                changes.append({"item": key, "tf": tf, "from": prev_tfs[tf][key], "to": value})
    prev_q = previous.get("quadrant", {})
    for period, value in current["quadrant"].items():
        if period in prev_q and prev_q[period] != value:
            changes.append({"item": "quadrant", "period": period, "from": prev_q[period], "to": value})
    return changes


# ---------------------------------------------------------------------------
# 조립
# ---------------------------------------------------------------------------


def build_summary(ctx: SummaryContext) -> BuiltSummary:
    a = ctx.analysis
    f = _Fmt(ctx.config.report)
    params = parameters(ctx.config, a.anchor_ms)
    hashed = params_hash(params)
    state = current_state(a)
    historical = ctx.trigger is SummaryTrigger.HISTORICAL
    document = {
        "meta": _meta(ctx, f, params, hashed),
        "data_freshness": _freshness(ctx),
        "price_structure": {"timeframes": [_price_structure(tf, a.ref_price, ctx.config, f) for tf in a.timeframes]},
        "regime": {"timeframes": [_regime(tf, f) for tf in a.timeframes]},
        "derivatives": _derivatives(a, f),
        "funding": _funding(ctx, historical, f),
        "levels": _levels(a, ctx.config, f),
        "events": [_event(e, f) for e in a.events],
        "state": _state(ctx, state, hashed),
        "statistics": {"status": "not_implemented"},
        "gaps": {
            "open": [_gap(g) for g in ctx.gaps],
            "acquisition_failures": list(ctx.failures),
        },
        "unavailable": [{"item": item, "reason": reason} for item, reason in UNAVAILABLE],
    }
    state_json = json.dumps(state, sort_keys=True, ensure_ascii=False)
    return BuiltSummary(document, state_json, hashed)


def serialize(document: Mapping[str, Any]) -> str:
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


def _meta(ctx: SummaryContext, f: _Fmt, params: Mapping[str, Any], hashed: str) -> dict[str, Any]:
    a = ctx.analysis
    if ctx.trigger is SummaryTrigger.HISTORICAL:
        current: dict[str, Any] = {"price": None, "bar_time": None, "is_closed": False, "null_reason": NOT_AVAILABLE_AT_REF_TIME}
        historical: dict[str, Any] | None = {
            "requested_time": format_time(ctx.requested_time),
            "publication_delay_reflected": False,
            "note": "저장소의 현재 내용으로 재현한다. 실시간으로는 몇 분 늦게 공개되는 값(예: metrics taker 비율)이 이미 채워져 있을 수 있다.",
        }
    else:
        bar = ctx.current_bar
        current = {
            "price": f.price(bar.close) if bar else None,
            "bar_time": format_time(bar.open_time) if bar else None,
            "is_closed": False,
            "null_reason": None if bar else REST_FAILED,
        }
        historical = None
    return {
        "summary_id": ctx.summary_id,
        "created_at": format_time(ctx.created_at),
        "trigger": ctx.trigger.value,
        "symbol": a.symbol,
        "ref_time": format_time(a.ref_time),
        "ref_price": f.price(a.ref_price),
        "current_price": current,
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "anchor_time": format_time(a.anchor_ms),
        "historical": historical,
        "params_hash": hashed,
        "params": params,
    }


def _freshness(ctx: SummaryContext) -> dict[str, Any]:
    """FR-4.3: 실행 시각 기준. 과거 시점 요약은 판정하지 않는다(FR-4.8)."""
    ref_time = ctx.analysis.ref_time
    if ctx.trigger is SummaryTrigger.HISTORICAL or ctx.run_time is None:
        return {"judged": False, "reason": "historical_summary", "ref_time": format_time(ref_time)}
    report = ctx.config.report
    stale_limit = {
        Dataset.KLINE_1M: report.stale_minutes_bars,
        Dataset.PREMIUM_INDEX_1M: report.stale_minutes_bars,
        Dataset.METRICS_5M: report.stale_minutes_metrics,
    }
    datasets = []
    for item in ctx.dataset_last:
        age = None if item.covered_until is None else (ctx.run_time - item.covered_until) // MINUTE_MS
        datasets.append(
            {
                "dataset": item.dataset.value,
                "covered_until": format_time(item.covered_until),
                "age_minutes": age,
                "stale": True if age is None else age >= stale_limit[item.dataset],
                "stale_after_minutes": stale_limit[item.dataset],
            }
        )
    return {
        "judged": True,
        "run_time": format_time(ctx.run_time),
        "run_time_source": ctx.run_time_source,
        "ref_time": format_time(ref_time),
        "ref_time_lag_minutes": (ctx.run_time - ref_time) // MINUTE_MS,
        "datasets": datasets,
    }


def _price_structure(tf: TfAnalysis, ref_price: float, config: Config, f: _Fmt) -> dict[str, Any]:
    series = tf.series
    swings = tf.zigzag.swings
    first = max(0, len(swings) - config.report.swings_per_tf)
    tentative = None
    if tf.zigzag.tentative is not None:
        t = tf.zigzag.tentative
        tentative = {
            "dir": t.dir,
            "price": f.price(t.price),
            "bar_time": format_time(series.open_time(t.bar_index)),
            "distance_atr": f.ratio(tf.tentative_distance_atr),
        }
    forming = series.forming
    last_bar = series.bars[tf.last_index] if tf.last_index >= 0 else None
    return {
        "tf": tf.tf,
        "last_closed_bar_time": format_time(series.open_time(tf.last_index)) if tf.last_index >= 0 else None,
        "last_closed_bar_missing_ratio": f.ratio(last_bar.missing_ratio) if last_bar is not None else None,
        "forming_bar": None if forming is None else {
            "bar_time": format_time(forming.open_time),
            "open": f.price(forming.open), "high": f.price(forming.high),
            "low": f.price(forming.low), "close": f.price(forming.close),
            "is_closed": False,
        },
        "atr": f.measured(tf.atr, "price"),
        "structure_state": tf.structure_state,
        "swings": [
            {
                "type": s.type,
                "price": f.price(s.price),
                "bar_time": format_time(series.open_time(s.bar_index)),
                "extreme_time": format_time(s.extreme_time),
                "confirmed_time": format_time(series.open_time(s.confirmed_index)),
                "known_time": format_time(s.known_time),
                "broken": i in tf.broken,
            }
            for i, s in enumerate(swings)
            if i >= first
        ],
        "tentative_wave": tentative,
        "retracement": None if tf.retracement is None else {
            "depth": f.ratio(tf.retracement.depth),
            "time_ratio": f.ratio(tf.retracement.time_ratio),
        },
        "candles": [_candle(row, f) for row in tf.candles],
    }


def _candle(row: Any, f: _Fmt) -> dict[str, Any]:
    c = row.candle
    if c is None:
        return {"bar_time": format_time(row.bar_time), "missing_ratio": f.ratio(row.missing_ratio), "null_reason": ABSENT_BAR}
    return {
        "bar_time": format_time(row.bar_time),
        "missing_ratio": f.ratio(row.missing_ratio),
        "upper_wick_ratio": f.ratio(c.upper_wick_ratio),
        "lower_wick_ratio": f.ratio(c.lower_wick_ratio),
        "body_ratio": f.ratio(c.body_ratio),
        "body_atr": f.ratio(c.body_atr),
        "range_atr": f.ratio(c.range_atr),
        "null_reason": None,
    }


def _regime(tf: TfAnalysis, f: _Fmt) -> dict[str, Any]:
    return {
        "tf": tf.tf,
        "efficiency_state": tf.efficiency_state,
        "efficiency_duration_bars": tf.efficiency_duration,
        "er": f.measured(tf.er, "ratio"),
        "volatility_state": tf.volatility_state,
        "volatility_duration_bars": tf.volatility_duration,
        "parkinson_bp": f.measured(tf.parkinson, "bp", BP),
        "parkinson_pct": f.measured(tf.parkinson_pct, "pct"),
        "shock_active": tf.shock_active,
    }


def _derivatives(a: Analysis, f: _Fmt) -> dict[str, Any]:
    d = a.derivatives
    p = d.premium
    smoothed = p.smoothed[-1] if p.smoothed else None
    latest = {m.field: m for m in d.metric_values}
    oi = latest.get("sum_open_interest", LatestMetric("sum_open_interest", None, None))
    return {
        "premium_index": {
            "current_bp": f.bp(p.current_bp),
            "bar_time": format_time(p.current_time),
            "changes": [{"window": c.window, "change_bp": f.bp(c.change_bp), "null_reason": c.null_reason} for c in p.changes],
            "smoothed": None if smoothed is None else {
                "bar_time": format_time(smoothed.open_time),
                "value_bp": f.bp(smoothed.value_bp),
                "missing_ratio": f.ratio(smoothed.missing_ratio),
                "pct": f.pct(smoothed.pct),
                "null_reason": smoothed.null_reason,
            },
        },
        "open_interest": {
            "contracts": f.price(oi.value),
            "ts": format_time(oi.ts),
            "quadrant_ts": format_time(d.metrics_ts),
            "quadrants": [
                {
                    "period": q.period,
                    "quadrant": q.quadrant,
                    "d_oi_percent": f.ratio(None if q.d_oi is None else q.d_oi * PERCENT),
                    "d_px_percent": f.ratio(None if q.d_px is None else q.d_px * PERCENT),
                    "null_reason": q.null_reason,
                }
                for q in d.quadrants
            ],
        },
        "ratios": [
            {"field": name, "value": f.ratio(latest[name].value if name in latest else None),
             "ts": format_time(latest[name].ts if name in latest else None)}
            for name in RATIO_FIELDS
        ],
    }


def _funding(ctx: SummaryContext, historical: bool, f: _Fmt) -> dict[str, Any]:
    """A.5.3. 비용 정보이며 계열 분류 대상이 아니다(FR-3.13)."""
    info = None if historical else ctx.funding
    if info is None:
        reason = NOT_AVAILABLE_AT_REF_TIME if historical else REST_FAILED
        return {"funding_rate_bp": None, "next_funding_time": None, "minutes_to_next_funding": None, "null_reason": reason}
    return {
        "funding_rate_bp": f.bp(info.last_funding_rate * BP),
        "next_funding_time": format_time(info.next_funding_time),
        "minutes_to_next_funding": (info.next_funding_time - ctx.analysis.ref_time) // MINUTE_MS,
        "null_reason": None,
    }


def _levels(a: Analysis, config: Config, f: _Fmt) -> dict[str, Any]:
    w = a.window
    base = {
        "normalize_tf": config.levels.normalize_tf,
        "atr": f.price(a.levels.atr),
        "vwap_24h": {"value": f.price(w.vwap), "gap_ratio": f.ratio(w.vwap_gap_ratio)},
        "high_24h": {"value": f.price(w.high), "time": format_time(w.high_time), "gap_ratio": f.ratio(w.range_gap_ratio)},
        "low_24h": {"value": f.price(w.low), "time": format_time(w.low_time), "gap_ratio": f.ratio(w.range_gap_ratio)},
    }
    if a.levels.atr is None:
        return base | {"levels": None, "null_reason": "normalize_atr_unavailable"}
    return base | {"levels": [_level(lv, f) for lv in a.levels.reported], "null_reason": None}


def _level(lv: Level, f: _Fmt) -> dict[str, Any]:
    return {
        "center": f.price(lv.center),
        "zone_low": f.price(lv.zone_low),
        "zone_high": f.price(lv.zone_high),
        "distance_atr": f.ratio(lv.distance_atr),
        "position": lv.position,
        "sources": list(lv.sources),
        "source_count": lv.source_count,
        "members": [
            {"source": m.source, "price": f.price(m.price), "broken": m.broken}
            for m in lv.members
        ],
    }


def _event(e: ev.Event, f: _Fmt) -> dict[str, Any]:
    measures: dict[str, Any] = {}
    for field in dataclasses.fields(e.measures):
        value = getattr(e.measures, field.name)
        name = field.name.rstrip("_")
        if name in _MEASURE_PERCENT:
            measures[_MEASURE_PERCENT[name]] = f.ratio(None if value is None else value * PERCENT)
        elif name.endswith("_time") and isinstance(value, int):
            measures[name] = format_time(value)
        elif isinstance(value, float):
            measures[name] = f.num(value, _MEASURE_KIND.get(name, "ratio"))
        else:
            measures[name] = value
    return {
        "type": e.type,
        "family": e.family,
        "tf": e.tf,
        "bar_time": format_time(e.bar_time),
        "bars_ago": e.bars_ago,
        "measures": measures,
    }


def _state(ctx: SummaryContext, state: Mapping[str, Any], hashed: str) -> dict[str, Any]:
    prev = ctx.previous
    if prev is None:
        return {"previous": None, "params_changed": None, "changes": [], "current": state}
    return {
        "previous": {
            "summary_id": prev.summary_id,
            "created_at": format_time(prev.created_at),
            "ref_time": format_time(prev.ref_time),
        },
        "params_changed": prev.params_hash != hashed,
        "changes": state_changes(json.loads(prev.state), state),
        "current": state,
    }


def _gap(g: OpenGap) -> dict[str, Any]:
    return {
        "dataset": g.dataset.value,
        "field": g.field,
        "start": format_time(g.range.start_ms),
        "end": format_time(g.range.end_ms),
        "reason": g.reason.value,
    }

