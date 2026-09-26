"""조건 레지스트리 명령과 평가 흐름 (PRD 10.7, FR-7.1 ~ FR-7.7).

입력 JSON의 해석과 검증은 명령 해석의 일부로 여기서 한다. 평가는 compute, 저장은 store가 맡는다.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from dataclasses import dataclass
from typing import Any

from coindata.cli.summary import SummaryError, parse_at
from coindata.compute.engine import ComputeError, analyze, load_input
from coindata.compute.plans import registration, valid_state_path
from coindata.config import Config
from coindata.models import (
    MINUTE_MS,
    CoCondition,
    Condition,
    AtRegistration,
    PlanSpec,
    PlanState,
)
from coindata.report.summary import params_hash, parameters
from coindata.store import plans as plan_store
from coindata.store import query

logger = logging.getLogger(__name__)

SCHEMA = "plan/1"
SIDES = ("long", "short")
KINDS = ("close_above", "close_below", "touch_above", "touch_below")
HOUR_MS = 60 * MINUTE_MS


class PlanInputError(Exception):
    """입력 전체를 처리할 수 없다(형식 오류, 등록 기준 요약 없음 등)."""


@dataclass(frozen=True, slots=True)
class PlanAddOutcome:
    plan_key: str
    registered: bool  # 검증만 했으면(dry_run) 등록 가능 여부
    errors: tuple[str, ...]
    at_registration: AtRegistration | None = None  # 오류가 없을 때의 등록 시 계산값 (FR-7.5)


# ---------------------------------------------------------------------------
# 입력 해석 (FR-7.1, FR-7.2)
# ---------------------------------------------------------------------------


def _condition(raw: Any, field: str, timeframes: tuple[str, ...], errors: list[str]) -> Condition | None:
    if not isinstance(raw, dict):
        errors.append(f"{field}: 객체여야 한다")
        return None
    unknown = sorted(set(raw) - {"kind", "tf", "price"})
    if unknown:
        errors.append(f"{field}: 알 수 없는 필드 {', '.join(unknown)}")
    kind, tf, price = raw.get("kind"), raw.get("tf"), raw.get("price")
    ok = True
    if kind not in KINDS:
        errors.append(f"{field}.kind: {', '.join(KINDS)} 중 하나여야 한다")
        ok = False
    if isinstance(kind, str) and kind.startswith("close_"):
        if tf not in timeframes:
            errors.append(f"{field}.tf: close 조건에는 계산 대상 TF({', '.join(timeframes)}) 중 하나가 필요하다")
            ok = False
    elif tf is not None:
        errors.append(f"{field}.tf: touch 조건에는 tf를 쓰지 않는다")
        ok = False
    if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
        errors.append(f"{field}.price: 양수여야 한다")
        ok = False
    return Condition(kind, tf, float(price)) if ok else None


def parse_plan(
    raw: Any, index: int, source_summary_id: str, config: Config
) -> tuple[str, PlanSpec | None, list[str]]:
    """계획 하나를 해석한다. (plan_key 또는 표시용 이름, 계획, 오류)."""
    prefix = f"plans[{index}]"
    errors: list[str] = []
    if not isinstance(raw, dict):
        return prefix, None, [f"{prefix}: 객체여야 한다"]
    allowed = {"plan_id", "side", "activation", "invalidation", "objective", "co_conditions", "expires_at"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        errors.append(f"{prefix}: 알 수 없는 필드 {', '.join(unknown)}")
    plan_id = raw.get("plan_id")
    if not isinstance(plan_id, str) or not plan_id or "/" in plan_id:
        errors.append(f"{prefix}.plan_id: '/'가 없는 비어 있지 않은 문자열이어야 한다")
        plan_id = None
    label = f"{source_summary_id}/{plan_id}" if plan_id else prefix
    side = raw.get("side")
    if side not in SIDES:
        errors.append(f"{prefix}.side: long 또는 short여야 한다")
    timeframes = config.indicators.timeframes
    activation = _condition(raw.get("activation"), f"{prefix}.activation", timeframes, errors)
    invalidation = _condition(raw.get("invalidation"), f"{prefix}.invalidation", timeframes, errors)
    objective = None
    if raw.get("objective") is not None:
        objective = _condition(raw["objective"], f"{prefix}.objective", timeframes, errors)
    co_conditions: list[CoCondition] = []
    raw_co = raw.get("co_conditions") or []
    if not isinstance(raw_co, list):
        errors.append(f"{prefix}.co_conditions: 배열이어야 한다")
        raw_co = []
    for j, co in enumerate(raw_co):
        field = f"{prefix}.co_conditions[{j}]"
        path = co.get("path") if isinstance(co, dict) else None
        equals = co.get("equals") if isinstance(co, dict) else None
        if not isinstance(path, str) or not valid_state_path(path, timeframes, config.derivatives.quadrant.periods):
            errors.append(
                f"{field}.path: timeframes.<tf>.efficiency_state|volatility_state|structure_state 또는 quadrant.<period>여야 한다"
            )
        elif not isinstance(equals, str):
            errors.append(f"{field}.equals: 문자열이어야 한다")
        else:
            co_conditions.append(CoCondition(path, equals))
    expires_at = None
    if raw.get("expires_at") is not None:
        try:
            expires_at = parse_at(str(raw["expires_at"]))
        except SummaryError:
            errors.append(f"{prefix}.expires_at: UTC 시각(예: 2026-09-25T18:00Z)이어야 한다")
    if errors or activation is None or invalidation is None or plan_id is None:
        return label, None, errors
    spec = PlanSpec(
        label, source_summary_id, plan_id, side, activation, invalidation, objective, tuple(co_conditions), expires_at
    )
    return label, spec, []


def check_against_source(spec: PlanSpec, ref_time: int, ref_price: float) -> list[str]:
    """가격 순서, 만료 시각, 이미 충족된 touch 조건 (FR-7.2)."""
    errors: list[str] = []
    a, i, o = spec.activation.price, spec.invalidation.price, spec.objective.price if spec.objective else None
    if spec.side == "long":
        if not (i < a and (o is None or a < o)):
            errors.append(f"{spec.plan_key}: long은 invalidation < activation < objective여야 한다")
    elif not (i > a and (o is None or a > o)):
        errors.append(f"{spec.plan_key}: short는 invalidation > activation > objective여야 한다")
    for name, c in (("activation", spec.activation), ("objective", spec.objective)):
        if c is None or c.is_close:
            continue
        met = ref_price >= c.price if c.is_above else ref_price <= c.price
        if met:
            errors.append(f"{spec.plan_key}.{name}: 등록 기준 요약의 ref_price({ref_price})에서 이미 충족된 touch 조건이다")
    if spec.expires_at is not None and spec.expires_at <= ref_time:
        errors.append(f"{spec.plan_key}.expires_at: 등록 기준 요약의 ref_time 이후여야 한다")
    return errors


# ---------------------------------------------------------------------------
# 명령
# ---------------------------------------------------------------------------


def add_plans(
    conn: sqlite3.Connection, config: Config, text: str, now_ms: int, dry_run: bool = False
) -> list[PlanAddOutcome]:
    """FR-7.2 검증 후 등록한다. `dry_run`이면 저장하지 않고 같은 검증과 계산만 한다(FR-8.5)."""
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise PlanInputError(f"JSON이 아니다: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise PlanInputError(f'schema가 "{SCHEMA}"여야 한다')
    unknown = sorted(set(data) - {"schema", "source_summary_id", "plans"})
    if unknown:
        raise PlanInputError(f"알 수 없는 필드: {', '.join(unknown)}")
    source_id = data.get("source_summary_id")
    source = query.get_summary(conn, source_id) if isinstance(source_id, str) else None
    if source is None:
        raise PlanInputError(f"source_summary_id가 summary_log에 없다: {source_id!r}")
    if not isinstance(data.get("plans"), list) or not data["plans"]:
        raise PlanInputError("plans는 비어 있지 않은 배열이어야 한다")

    try:
        analysis = analyze(load_input(conn, config, source.ref_time), config)
    except ComputeError as exc:
        raise PlanInputError(f"등록 기준 요약 시점을 다시 계산할 수 없다: {exc}") from exc
    current_hash = params_hash(parameters(config, analysis.anchor_ms))
    default_expiry = source.ref_time + config.plans.default_ttl_hours * HOUR_MS

    outcomes = []
    seen: set[str] = set()
    for index, raw in enumerate(data["plans"]):
        label, spec, errors = parse_plan(raw, index, source.summary_id, config)
        if spec is not None:
            errors = check_against_source(spec, source.ref_time, analysis.ref_price)
            if not errors and (spec.plan_key in seen or plan_store.plan_exists(conn, spec.plan_key)):
                errors = [f"{spec.plan_key}: 같은 plan_key가 이미 있다"]
        if spec is None or errors:
            outcomes.append(PlanAddOutcome(label, False, tuple(errors)))
            continue
        seen.add(spec.plan_key)
        at_reg = registration(spec, analysis, now_ms, source.params_hash != current_hash)
        if not dry_run:
            plan_store.insert_plan(conn, spec, now_ms, spec.expires_at or default_expiry, at_reg)
        outcomes.append(PlanAddOutcome(spec.plan_key, True, (), at_reg))
    return outcomes


def used_plan_ids(conn: sqlite3.Connection, source_summary_id: str) -> set[str]:
    """그 요약을 기준으로 이미 등록된 `plan_id` (GUI의 자동 부여가 피할 번호, FR-8.5)."""
    return {r.spec.plan_id for r in plan_store.list_plans(conn) if r.spec.source_summary_id == source_summary_id}


def cancel(conn: sqlite3.Connection, plan_key: str, now_ms: int) -> str | None:
    """pending 계획만 취소한다(FR-7.3). 문제가 있으면 사유를 돌려준다."""
    record = plan_store.get_plan(conn, plan_key)
    if record is None:
        return f"계획이 없다: {plan_key}"
    if record.cancelled_at is not None:
        return f"이미 취소되었다: {plan_key}"
    state = record.evaluation.state if record.evaluation else PlanState.PENDING
    if state is not PlanState.PENDING:
        return f"pending 계획만 취소할 수 있다(현재 {state.value}): {plan_key}"
    plan_store.cancel_plan(conn, plan_key, now_ms)
    return None
