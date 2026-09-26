"""계획 평가 흐름 (PRD FR-7.4, FR-7.6). `sync`와 `summary`가 쓴다."""

from __future__ import annotations

import sqlite3

from coindata.compute.plans import evaluate_record
from coindata.config import Config
from coindata.models import MINUTE_MS, Dataset, PlanRecord
from coindata.report.summary import PlanView
from coindata.store import plans as plan_store
from coindata.store import query

HOUR_MS = 60 * MINUTE_MS


def evaluation_end(conn: sqlite3.Connection, config: Config) -> int | None:
    """현재 시점 평가의 끝: 마지막 저장 1분봉의 다음 분."""
    bounds = query.time_bounds(conn, Dataset.KLINE_1M, config.data.symbol)
    return bounds.end_ms + MINUTE_MS if bounds else None


def needs_evaluation(record: PlanRecord, until: int, config: Config) -> bool:
    """미종료 계획과 요약 보고 범위 안에서 종료된 계획을 다시 평가한다(늦게 채워진 결측 반영)."""
    ev = record.evaluation
    if ev is None or ev.state.is_open:
        return True
    end = ev.transitions[-1].time if ev.transitions else record.expires_at
    return end >= until - config.plans.report_hours * HOUR_MS


def refresh_plans(conn: sqlite3.Connection, config: Config, now_ms: int) -> int:
    """FR-7.4: 저장된 계획을 처음부터 다시 평가해 저장한다. 평가한 계획 수를 돌려준다."""
    until = evaluation_end(conn, config)
    if until is None:
        return 0
    count = 0
    for record in plan_store.list_plans(conn):
        if not needs_evaluation(record, until, config):
            continue
        evaluation, context = evaluate_record(conn, config, record, until)
        plan_store.save_evaluation(conn, record.spec.plan_key, evaluation, context, now_ms)
        count += 1
    return count


def _in_report(view: PlanView, ref_time: int, config: Config) -> bool:
    """FR-7.6: 미종료 계획과, 종료 시각이 기준 시각 전 report_hours 안인 계획."""
    ev = view.evaluation
    if ev.state.is_open:
        return True
    end = ev.transitions[-1].time if ev.transitions else view.record.expires_at
    return end >= ref_time - config.plans.report_hours * HOUR_MS


def live_views(conn: sqlite3.Connection, config: Config, ref_time: int) -> tuple[PlanView, ...]:
    """저장된 평가 결과로 만든 현재 시점 요약용 목록. 등록 기준 요약이 기준 시각 이전인 계획만."""
    views = [
        PlanView(r, r.evaluation, r.activation_context)
        for r in plan_store.list_plans(conn)
        if r.evaluation is not None and r.at_registration.source_ref_time < ref_time
    ]
    return tuple(v for v in views if _in_report(v, ref_time, config))


def historical_views(conn: sqlite3.Connection, config: Config, ref_time: int) -> tuple[PlanView, ...]:
    """FR-7.4: 과거 시점 요약은 그 기준 시각까지 메모리에서 평가하고 저장하지 않는다."""
    views = []
    for record in plan_store.list_plans(conn):
        if record.at_registration.source_ref_time >= ref_time:
            continue
        evaluation, context = evaluate_record(conn, config, record, ref_time)
        views.append(PlanView(record, evaluation, context))
    return tuple(v for v in views if _in_report(v, ref_time, config))
