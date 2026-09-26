"""조건 레지스트리 저장 (PRD 9.2 `plan`, `plan_state_log`, FR-7.4).

계획과 평가 결과는 JSON 문자열로 저장하고, 읽을 때 models의 데이터 구조로 되돌린다.
`plan_state_log`는 파생 테이블이며 평가마다 계획별로 교체한다.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from typing import Any

from coindata.models import (
    ActivationContext,
    AtRegistration,
    CoCondition,
    CoConditionResult,
    Condition,
    NearestLevel,
    PlanEvaluation,
    PlanRecord,
    PlanSpec,
    PlanState,
    SinceActivation,
    TimeRange,
    Transition,
)
from coindata.store.db import transaction

_COLUMNS = (
    "plan_key, spec, registered_at, expires_at, cancelled_at, at_registration, activation_context, "
    "evaluation, evaluated_at"
)


def _dump(value: Any) -> str:
    def default(o: Any) -> Any:
        if isinstance(o, PlanState):
            return o.value
        raise TypeError(f"직렬화할 수 없는 값: {o!r}")

    return json.dumps(dataclasses.asdict(value), ensure_ascii=False, sort_keys=True, default=default)


# ---------------------------------------------------------------------------
# JSON → 데이터 구조
# ---------------------------------------------------------------------------


def _condition(d: dict[str, Any] | None) -> Condition | None:
    return None if d is None else Condition(d["kind"], d["tf"], d["price"])


def spec_from_json(text: str) -> PlanSpec:
    d = json.loads(text)
    activation, invalidation = _condition(d["activation"]), _condition(d["invalidation"])
    assert activation is not None and invalidation is not None
    return PlanSpec(
        d["plan_key"], d["source_summary_id"], d["plan_id"], d["side"], activation, invalidation,
        _condition(d["objective"]), tuple(CoCondition(c["path"], c["equals"]) for c in d["co_conditions"]),
        d["expires_at"],
    )


def _at_registration(text: str) -> AtRegistration:
    d = json.loads(text)
    level = d["nearest_opposing_level"]
    d["nearest_opposing_level"] = None if level is None else NearestLevel(**level)
    return AtRegistration(**d)


def _activation_context(text: str | None) -> ActivationContext | None:
    if text is None:
        return None
    d = json.loads(text)
    return ActivationContext(d["activation_time"], d["atr"], tuple(CoConditionResult(**c) for c in d["co_conditions"]))


def _evaluation(text: str | None) -> PlanEvaluation | None:
    if text is None:
        return None
    d = json.loads(text)
    since = d["since_activation"]
    if since is not None:
        since["end_reason"] = PlanState(since["end_reason"]) if since["end_reason"] else None
        since = SinceActivation(**since)
    return PlanEvaluation(
        PlanState(d["state"]),
        tuple(Transition(PlanState(t["state"]), t["time"], t["price"], t["gap_before"]) for t in d["transitions"]),
        tuple(TimeRange(g["start_ms"], g["end_ms"]) for g in d["evaluation_gaps"]),
        since,
        d["evaluated_until"],
    )


def _record(row: tuple[Any, ...]) -> PlanRecord:
    key, spec, registered, expires, cancelled, at_reg, context, evaluation, evaluated_at = row
    return PlanRecord(
        spec_from_json(spec), spec, registered, expires, cancelled, _at_registration(at_reg),
        _activation_context(context), _evaluation(evaluation), evaluated_at,
    )


# ---------------------------------------------------------------------------
# 쓰기·읽기
# ---------------------------------------------------------------------------


def spec_to_json(spec: PlanSpec) -> str:
    return _dump(spec)


def insert_plan(conn: sqlite3.Connection, spec: PlanSpec, registered_at: int, expires_at: int, at_registration: AtRegistration) -> None:
    """기본키(plan_key)가 겹치면 sqlite3.IntegrityError."""
    with transaction(conn):
        conn.execute(
            "INSERT INTO plan (plan_key, source_summary_id, spec, registered_at, expires_at, at_registration, state) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                spec.plan_key, spec.source_summary_id, spec_to_json(spec), registered_at, expires_at,
                _dump(at_registration), PlanState.PENDING.value,
            ),
        )


def plan_exists(conn: sqlite3.Connection, plan_key: str) -> bool:
    return conn.execute("SELECT 1 FROM plan WHERE plan_key = ?", (plan_key,)).fetchone() is not None


def get_plan(conn: sqlite3.Connection, plan_key: str) -> PlanRecord | None:
    row = conn.execute(f"SELECT {_COLUMNS} FROM plan WHERE plan_key = ?", (plan_key,)).fetchone()
    return _record(row) if row else None


def list_plans(conn: sqlite3.Connection) -> list[PlanRecord]:
    rows = conn.execute(f"SELECT {_COLUMNS} FROM plan ORDER BY registered_at, plan_key")
    return [_record(row) for row in rows]


def cancel_plan(conn: sqlite3.Connection, plan_key: str, cancelled_at: int) -> None:
    with transaction(conn):
        conn.execute("UPDATE plan SET cancelled_at = ? WHERE plan_key = ?", (cancelled_at, plan_key))


def save_evaluation(
    conn: sqlite3.Connection,
    plan_key: str,
    evaluation: PlanEvaluation,
    context: ActivationContext | None,
    evaluated_at: int,
) -> None:
    """평가 결과를 저장하고 `plan_state_log`를 교체한다(FR-7.4)."""
    with transaction(conn):
        conn.execute(
            "UPDATE plan SET state = ?, evaluation = ?, activation_context = ?, evaluated_at = ? WHERE plan_key = ?",
            (
                evaluation.state.value, _dump(evaluation), _dump(context) if context is not None else None,
                evaluated_at, plan_key,
            ),
        )
        conn.execute("DELETE FROM plan_state_log WHERE plan_key = ?", (plan_key,))
        conn.executemany(
            "INSERT INTO plan_state_log (plan_key, seq, state, time, price, gap_before, evaluated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (plan_key, seq, t.state.value, t.time, t.price, int(t.gap_before), evaluated_at)
                for seq, t in enumerate(evaluation.transitions)
            ],
        )
