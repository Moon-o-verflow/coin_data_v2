"""계획 붙여넣기 해석 (PRD FR-8.5).

판단 모델 답변에서 `plan/1` JSON 하나를 꺼내고, `plan_id`가 없는 계획에 번호를 붙인다.
검증 규칙(FR-7.2)은 여기서 판단하지 않는다. 결과 텍스트를 CLI와 같은 등록 함수에 넘긴다.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

_FENCE = re.compile(r"```[A-Za-z0-9_-]*[ \t]*\r?\n(.*?)```", re.S)
PLAN_ID_PREFIX = "p"


class PasteError(Exception):
    """붙여넣은 글에서 등록할 JSON을 얻을 수 없다."""


@dataclass(frozen=True, slots=True)
class Prepared:
    text: str  # 등록 함수에 넘길 JSON
    source_summary_id: str
    assigned: tuple[tuple[int, str], ...]  # (plans 안의 위치, 붙인 plan_id)


def extract_json(text: str) -> dict[str, Any]:
    """코드 울타리가 있으면 그 안, 없으면 글 전체를 JSON 객체로 읽는다. 울타리가 둘 이상이면 오류."""
    blocks = _FENCE.findall(text)
    if len(blocks) > 1:
        raise PasteError(f"코드 블록이 {len(blocks)}개다. 등록할 JSON 블록 하나만 붙여넣어라")
    body = blocks[0] if blocks else text
    if not body.strip():
        raise PasteError("붙여넣은 내용이 없다")
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise PasteError(f"JSON이 아니다: {exc}") from exc
    if not isinstance(data, dict):
        raise PasteError("JSON 객체여야 한다")
    return data


def _missing(value: Any) -> bool:
    return value is None or value == ""


def assign_plan_ids(data: dict[str, Any], in_use: set[str]) -> tuple[dict[str, Any], tuple[tuple[int, str], ...]]:
    """`plan_id`가 없는 계획에 `p1`, `p2`, …를 붙인다. 등록된 번호와 입력 안의 번호를 피한다."""
    plans = data.get("plans")
    if not isinstance(plans, list):
        return data, ()
    taken = set(in_use) | {p["plan_id"] for p in plans if isinstance(p, dict) and isinstance(p.get("plan_id"), str)}
    result = dict(data)
    result["plans"] = []
    assigned = []
    number = 1
    for index, plan in enumerate(plans):
        if isinstance(plan, dict) and _missing(plan.get("plan_id")):
            while f"{PLAN_ID_PREFIX}{number}" in taken:
                number += 1
            plan_id = f"{PLAN_ID_PREFIX}{number}"
            taken.add(plan_id)
            plan = {"plan_id": plan_id, **{k: v for k, v in plan.items() if k != "plan_id"}}
            assigned.append((index, plan_id))
        result["plans"].append(plan)
    return result, tuple(assigned)


def prepare(text: str, ids_in_use: Callable[[str], set[str]]) -> Prepared:
    """붙여넣은 글 → 등록 함수에 넘길 JSON. `source_summary_id`가 없으면 추정하지 않고 오류로 한다."""
    data = extract_json(text)
    source = data.get("source_summary_id")
    if not isinstance(source, str) or not source:
        raise PasteError("source_summary_id가 없다. 판단 모델 출력에 등록 기준 요약 ID가 있어야 한다")
    filled, assigned = assign_plan_ids(data, ids_in_use(source))
    return Prepared(json.dumps(filled, ensure_ascii=False, indent=2), source, assigned)
