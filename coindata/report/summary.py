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
from coindata.compute import stats as st
from coindata.compute.flow import FlowResult
from coindata.compute.reference import Divergence, ReferenceResult
from coindata.compute.session import SessionResult
from coindata.compute.levels import Level, TouchStats, distance_bp
from coindata.compute.series import Measured
from coindata.config import Config, ReportConfig
from coindata.models import (
    MINUTE_MS,
    Dataset,
    FundingInfo,
    Kline,
    ActivationContext,
    OpenGap,
    PlanEvaluation,
    PlanRecord,
    SummaryRecord,
    SummaryTrigger,
)

SUMMARY_SCHEMA_VERSION = "2"
NOT_AVAILABLE_AT_REF_TIME = "not_available_at_ref_time"
REST_FAILED = "rest_failed"
PREVIOUS_PARAMS_UNAVAILABLE = "previous_params_unavailable"
BP = 10_000
PERCENT = 100

UNAVAILABLE: tuple[tuple[str, str], ...] = (
    ("liquidation", "source_unavailable"),
    ("trade_size_distribution", "not_implemented"),
)

# 이벤트 측정값의 자릿수 종류. 여기에 없는 실수는 비율 자릿수를 쓴다.
_MEASURE_KIND = {"price": "price", "swing_price": "price", "level_center": "price", "value_bp": "bp", "pct": "pct"}
# 4분면 변화율은 값이 작아 백분율로 싣는다(FR-4.1 표기 규칙).
_MEASURE_PERCENT = {"d_oi": "d_oi_percent", "d_px": "d_px_percent"}


@dataclass(frozen=True, slots=True)
class DatasetLast:
    dataset: Dataset
    covered_until: int | None  # 봉은 마지막 봉의 close_time + 1, metrics는 마지막 ts


@dataclass(frozen=True, slots=True)
class PlanView:
    """요약의 `plans` 섹션에 실을 계획 하나와 그 평가 (FR-7.6)."""

    record: PlanRecord
    evaluation: PlanEvaluation
    context: ActivationContext | None


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
    full_params: bool  # --full-params
    plans: tuple[PlanView, ...]


@dataclass(frozen=True, slots=True)
class BuiltSummary:
    document: dict[str, Any]
    state_json: str
    params_hash: str
    params_json: str  # summary_log.params (FR-4.1 params_diff)


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
        self._digits = {
            "price": report.digits_price, "ratio": report.digits_ratio, "bp": report.digits_bp,
            "pct": report.digits_pct, "volume": report.digits_volume,
        }

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

    def volume(self, value: float | None) -> float | None:
        return self.num(value, "volume")

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
        "reference": dataclasses.asdict(config.reference),
        "sessions": dataclasses.asdict(config.sessions),
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
        "quadrant": {q.period: q.confirmed for q in analysis.derivatives.quadrants},
    }


def _flatten(node: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            out |= _flatten(value, f"{prefix}{key}.")
        return out
    return {prefix[:-1]: node}


def params_diff(previous: Mapping[str, Any], current: Mapping[str, Any]) -> list[dict[str, Any]]:
    """FR-4.1: 바뀐 파라미터 키의 이전 값과 새 값. 한쪽에만 있는 키는 없는 쪽을 null로 싣는다."""
    # 저장된 원문은 JSON이므로 튜플이 리스트로 돌아온다. 같은 표현으로 맞춰 비교한다.
    before, after = _flatten(json.loads(json.dumps(previous))), _flatten(json.loads(json.dumps(current)))
    return [
        {"key": key, "from": before.get(key), "to": after.get(key)}
        for key in sorted(set(before) | set(after))
        if before.get(key) != after.get(key)
    ]


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
        "derivatives": _derivatives(ctx, f),
        "flow": {"timeframes": [_flow(tf.tf, tf.flow, f) for tf in a.timeframes]},
        "reference": {"timeframes": [_reference(r, f) for r in a.reference]},
        "funding": _funding(ctx, historical, f),
        "levels": _levels(a, ctx.config, f),
        "events": [_event(e, f) for e in a.events],
        "plans": [_plan(v, f) for v in ctx.plans],
        "state": _state(ctx, state, hashed),
        "statistics": _statistics(a, f),
        "gaps": {
            "open": [_gap(g) for g in ctx.gaps],
            "acquisition_failures": list(ctx.failures),
        },
        "unavailable": [{"item": item, "reason": reason} for item, reason in UNAVAILABLE],
    }
    state_json = json.dumps(state, sort_keys=True, ensure_ascii=False)
    params_json = json.dumps(params, sort_keys=True, ensure_ascii=False)
    return BuiltSummary(document, state_json, hashed, params_json)


def serialize(document: Mapping[str, Any], compact: bool = False) -> str:
    """`compact`면 들여쓰기 없이 한 줄로 쓴다(`--compact`)."""
    if compact:
        return json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n"
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
        "session": _session(a.session),
        "historical": historical,
        "params_hash": hashed,
        **_params_fields(ctx, params, hashed),
    }


def _params_fields(ctx: SummaryContext, params: Mapping[str, Any], hashed: str) -> dict[str, Any]:
    """FR-4.1: 기본은 해시만. 직전 요약과 해시가 다르면 변경분, `--full-params`면 전체."""
    fields: dict[str, Any] = {}
    prev = ctx.previous
    if prev is not None and prev.params_hash != hashed:
        if prev.params is None:
            fields["params_diff"] = None
            fields["params_diff_null_reason"] = PREVIOUS_PARAMS_UNAVAILABLE
        else:
            fields["params_diff"] = params_diff(json.loads(prev.params), params)
    if ctx.full_params:
        fields["params"] = params
    return fields


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
            "distance_bp": f.bp(distance_bp(t.price, ref_price)),
            "bar_time": format_time(series.open_time(t.bar_index)),
            "distance_atr": f.ratio(tf.tentative_distance_atr),
        }
    brk = tf.last_break
    last_break = None if brk is None else {
        "side": brk.side,
        "break_kind": brk.break_kind,
        "swing_price": f.price(brk.swing.price),
        "bar_time": format_time(series.open_time(brk.bar_index)),
        "bars_ago": tf.last_index - brk.bar_index,
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
        "high_relation": tf.high_relation,
        "low_relation": tf.low_relation,
        "last_break": last_break,
        "swings": [
            {
                "type": s.type,
                "price": f.price(s.price),
                "distance_bp": f.bp(distance_bp(s.price, ref_price)),
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
            "basis": "ref_price",
        },
        "retracement_tentative": None if tf.retracement_tentative is None else {
            "depth": f.ratio(tf.retracement_tentative),
            "basis": "ref_price",
        },
        "candles": [_candle(row, f) for row in tf.candles],
    }


def _candle(row: Any, f: _Fmt) -> dict[str, Any]:
    c = row.candle
    if c is None:
        return {"bar_time": format_time(row.bar_time), "absent": True}
    return {
        "bar_time": format_time(row.bar_time),
        "absent": False,
        "missing_ratio": f.ratio(row.missing_ratio),
        "close_vs_open": c.close_vs_open,
        "upper_wick_ratio": f.ratio(c.upper_wick_ratio),
        "lower_wick_ratio": f.ratio(c.lower_wick_ratio),
        "body_ratio": f.ratio(c.body_ratio),
        "body_atr": f.ratio(c.body_atr),
        "range_atr": f.ratio(c.range_atr),
        "ratio_null_reason": row.ratio_null_reason,
        "atr_null_reason": row.atr_null_reason,
    }


def _regime(tf: TfAnalysis, f: _Fmt) -> dict[str, Any]:
    return {
        "tf": tf.tf,
        "efficiency_state": tf.efficiency_state,
        "efficiency_duration_bars": tf.efficiency_duration,
        "er": f.measured(tf.er, "ratio"),
        "er_direction": tf.er_direction,
        "volatility_state": tf.volatility_state,
        "volatility_duration_bars": tf.volatility_duration,
        "parkinson_bp": f.measured(tf.parkinson, "bp", BP),
        "parkinson_pct": f.measured(tf.parkinson_pct, "pct"),
        "shock_active": tf.shock_active,
    }


def _possibly_unpublished(ctx: SummaryContext, field: str, ts: int | None) -> bool:
    """FR-4.8: 과거 시점 요약에서 `ts > ref_time − lag`인 metrics 값. 현재 시점 요약은 항상 False."""
    if ctx.trigger is not SummaryTrigger.HISTORICAL or ts is None:
        return False
    lag = ctx.config.historical.publication_lag_minutes.get(field, 0) * MINUTE_MS
    return ts > ctx.analysis.ref_time - lag


def _derivatives(ctx: SummaryContext, f: _Fmt) -> dict[str, Any]:
    d = ctx.analysis.derivatives
    p = d.premium
    smoothed = p.smoothed[-1] if p.smoothed else None
    latest = {m.field: m for m in d.metric_values}
    oi = latest.get("sum_open_interest")
    oi_value, oi_ts = (oi.value, oi.ts) if oi is not None else (None, None)
    return {
        "premium_index": {
            "current_bp": f.bp(p.current_bp),
            "bar_time": format_time(p.current_time),
            "current_pct": f.pct(p.current_pct),
            "current_pct_null_reason": p.current_pct_null_reason,
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
            "contracts": f.price(oi_value),
            "ts": format_time(oi_ts),
            "possibly_unpublished_at_ref_time": _possibly_unpublished(ctx, "sum_open_interest", oi_ts),
            "quadrant_ts": format_time(d.metrics_ts),
            "quadrants": [_quadrant(q, f) for q in d.quadrants],
        },
        "ratios": [
            {
                "field": r.field,
                "value": f.ratio(r.value),
                "ts": format_time(r.ts),
                "pct": f.pct(r.pct),
                "sample_n": r.sample_n,
                "null_reason": r.null_reason,
                "possibly_unpublished_at_ref_time": _possibly_unpublished(ctx, r.field, r.ts),
            }
            for r in d.ratios
        ],
    }


def _quadrant(q: Any, f: _Fmt) -> dict[str, Any]:
    point = q.point

    def percent(x: float | None) -> float | None:
        return f.ratio(None if x is None else x * PERCENT)

    return {
        "period": q.period,
        "quadrant_confirmed": q.confirmed,
        "quadrant_raw": point.raw,
        "confirmed_since": format_time(q.confirmed_since),
        "duration_snapshots": q.duration_snapshots,
        "duration_capped": q.duration_capped,
        "d_oi_percent": percent(point.d_oi),
        "d_px_percent": percent(point.d_px),
        "band_oi_percent": percent(point.band_oi),
        "band_px_percent": percent(point.band_px),
        "null_reason": point.null_reason,
    }


def _flow(tf: str, fl: FlowResult, f: _Fmt) -> dict[str, Any]:
    """A.12. taker 체결량은 1분봉 기준이며 전체 거래량이 아니다(D-7)."""
    return {
        "tf": tf,
        "bar_time": format_time(fl.bar_time),
        "taker_buy": f.volume(fl.taker_buy),
        "taker_sell": f.volume(fl.taker_sell),
        "delta": f.volume(fl.delta),
        "imbalance": f.measured(fl.imbalance, "ratio"),
        "imbalance_pct": f.measured(fl.imbalance_pct, "pct"),
        "delta_ema": f.measured(fl.delta_ema, "volume"),
    }


def _session(se: SessionResult) -> dict[str, Any]:
    """FR-4.9. 시계 기준 표시이며 휴장일을 반영하지 않는다."""
    return {"label": se.label, "active": list(se.active), "null_reason": se.null_reason}


def _reference(r: ReferenceResult, f: _Fmt) -> dict[str, Any]:
    """A.13. 판단의 근거 수에 세지 않는 참조 지표다(FR-3.13)."""
    bb, macd = r.bollinger, r.macd
    return {
        "tf": r.tf,
        "ma": {
            "order": r.ma_order,
            "values": [
                {"period": m.period, **f.measured(m.value, "price"), "distance_bp": f.bp(m.distance_bp)} for m in r.ma
            ],
        },
        "rsi": f.measured(r.rsi, "pct"),
        "bollinger": {
            "upper": f.price(bb.upper),
            "lower": f.price(bb.lower),
            "percent_b": f.measured(bb.percent_b, "ratio"),
            "width": f.measured(bb.width, "ratio"),
            "width_pct": f.measured(bb.width_pct, "pct"),
        },
        "macd": {
            "histogram": f.measured(macd.histogram, "price"),
            "histogram_side": macd.histogram_side,
            "bars_since_side_change": macd.bars_since_side_change,
        },
        "rsi_divergence": {"highs": _divergence(r.divergence_highs), "lows": _divergence(r.divergence_lows)},
    }


def _divergence(d: Divergence | None) -> dict[str, Any] | None:
    return None if d is None else {"relation": d.relation, "known_time": format_time(d.known_time)}


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
    levels = [_level(lv, a.touches.get(lv.level_id), a.ref_price, f) for lv in a.levels.reported]
    return base | {"levels": levels, "null_reason": None}


def _level(lv: Level, touches: TouchStats | None, ref_price: float, f: _Fmt) -> dict[str, Any]:
    return {
        "level_id": lv.level_id,
        "center": f.price(lv.center),
        "center_distance_bp": f.bp(distance_bp(lv.center, ref_price)),
        "zone_low": f.price(lv.zone_low),
        "zone_low_distance_bp": f.bp(distance_bp(lv.zone_low, ref_price)),
        "zone_high": f.price(lv.zone_high),
        "zone_high_distance_bp": f.bp(distance_bp(lv.zone_high, ref_price)),
        "distance_atr": f.ratio(lv.distance_atr),
        "position": lv.position,
        "sources": list(lv.sources),
        "source_count": lv.source_count,
        "members": [
            {"source": m.source, "price": f.price(m.price), "broken": m.broken}
            for m in lv.members
        ],
        "touch_count": touches.touch_count if touches else None,
        "last_touch_bars_ago": touches.last_touch_bars_ago if touches else None,
        "touch_absent_bars": touches.absent_bars if touches else None,
        "touch_null_reason": touches.null_reason if touches else None,
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



def _condition(c: Any) -> dict[str, Any] | None:
    return None if c is None else {"kind": c.kind, "tf": c.tf, "price": c.price}


def _plan(view: PlanView, f: _Fmt) -> dict[str, Any]:
    """FR-7.6. 입력 원문, 상태, 전이 이력, 계산 필드. `side`는 입력을 되돌려 준 값이다(R-2)."""
    spec, reg, ev = view.record.spec, view.record.at_registration, view.evaluation
    since = ev.since_activation
    nearest = reg.nearest_opposing_level
    return {
        "plan_key": spec.plan_key,
        "input": {
            "side": spec.side,
            "activation": _condition(spec.activation),
            "invalidation": _condition(spec.invalidation),
            "objective": _condition(spec.objective),
            "co_conditions": [{"path": c.path, "equals": c.equals} for c in spec.co_conditions],
        },
        "state": ev.state.value,
        "expires_at": format_time(view.record.expires_at),
        "transitions": [
            {"state": t.state.value, "time": format_time(t.time), "price": f.price(t.price), "gap_before": t.gap_before}
            for t in ev.transitions
        ],
        "at_registration": {
            "source_ref_time": format_time(reg.source_ref_time),
            "source_ref_price": f.price(reg.source_ref_price),
            "risk_bp": f.bp(reg.risk_bp),
            "risk_atr": f.ratio(reg.risk_atr),
            "reward_bp": f.bp(reg.reward_bp),
            "reward_atr": f.ratio(reg.reward_atr),
            "activation_distance_bp": f.bp(reg.activation_distance_bp),
            "nearest_opposing_level": None if nearest is None else {
                "level_id": nearest.level_id,
                "boundary": f.price(nearest.boundary),
                "distance_bp": f.bp(nearest.distance_bp),
                "distance_atr": f.ratio(nearest.distance_atr),
                "activation_inside_zone": nearest.activation_inside_zone,
            },
            "registration_lag_minutes": reg.registration_lag_minutes,
            "params_changed_since_source": reg.params_changed_since_source,
        },
        "since_activation": None if since is None else {
            "activation_time": format_time(since.activation_time),
            "activation_price": f.price(since.activation_price),
            "mfe_bp": f.bp(since.mfe_bp),
            "mae_bp": f.bp(since.mae_bp),
            "mfe_atr": f.ratio(since.mfe_atr),
            "mae_atr": f.ratio(since.mae_atr),
            "excursion_null_reason": since.excursion_null_reason,
            "end_time": format_time(since.end_time),
            "end_reason": since.end_reason.value if since.end_reason else None,
            "end_price": f.price(since.end_price),
            "bars_to_end": since.bars_to_end,
        },
        "co_conditions_at_activation": None if view.context is None else [
            {"path": c.path, "equals": c.equals, "value": c.value, "met": c.met} for c in view.context.co_conditions
        ],
        "evaluation_gaps": [{"start": format_time(g.start_ms), "end": format_time(g.end_ms)} for g in ev.evaluation_gaps],
    }


S1_NOTE = "관측 빈도는 과거 표본의 기술이며 다음 사건의 발생 가능성을 뜻하지 않는다."
REQUIRED_TIMEFRAME_MISSING = "required_timeframe_missing"


def _statistics(a: Analysis, f: _Fmt) -> dict[str, Any]:
    """부록 B.1.7. 현재 상태에 해당하는 버킷만 싣는다. break_kind는 네 버킷 모두."""
    result = a.s1
    if result is None:
        return {"s1": None, "s1_null_reason": REQUIRED_TIMEFRAME_MISSING}
    by_tf = {tf.tf: tf for tf in a.timeframes}
    h1_now = by_tf[st.S1_CONTEXT_TF].efficiency_state or st.UNAVAILABLE
    m15_now = by_tf[st.S1_TF].volatility_state or st.UNAVAILABLE
    selections: list[tuple[str, str, list[st.Sample]]] = [
        ("all", "all", list(result.samples)),
        ("h1_efficiency_state", h1_now, [s for s in result.samples if s.h1_efficiency_state == h1_now]),
        ("m15_volatility_state", m15_now, [s for s in result.samples if s.m15_volatility_state == m15_now]),
    ]
    selections += [("break_kind", k, [s for s in result.samples if s.brk.break_kind == k]) for k in st.BREAK_KINDS]
    horizons = []
    for n in result.horizon_bars:
        buckets = []
        for axis, value, samples in selections:
            b = st.bucket(samples, n, result.min_n)
            buckets.append({
                "axis": axis,
                "value": value,
                "n": b.n,
                "held": b.held,
                "failed": b.failed,
                "held_ratio": f.ratio(b.held_ratio),
                "mfe_bp_median": f.bp(b.mfe_bp_median),
                "mae_bp_median": f.bp(b.mae_bp_median),
                "mfe_atr_median": f.ratio(b.mfe_atr_median),
                "mae_atr_median": f.ratio(b.mae_atr_median),
                "null_reason": b.null_reason,
            })
        horizons.append({"horizon_bars": n, "buckets": buckets})
    return {
        "s1": {
            "name": "structure_break_hold",
            "definition_version": result.definition_version,
            "note": S1_NOTE,
            "tf": st.S1_TF,
            "context_tf": st.S1_CONTEXT_TF,
            "horizon_bars": list(result.horizon_bars),
            "min_n": result.min_n,
            "period_start": format_time(result.period_start),
            "period_end": format_time(result.period_end),
            "samples": len(result.samples),
            "excluded_overlap": result.excluded_overlap,
            "excluded_gap": result.excluded_gap,
            "pending_outcome": result.pending_outcome,
            "horizons": horizons,
        }
    }
