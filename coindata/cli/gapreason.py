"""결측 원인 판정 (PRD 12.2, FR-2.2).

store가 찾은 빠진 구간을, 이번 실행의 수집 결과(요청 시도와 성공 여부)와 `archive_file` 상태로 분류한다.
구간은 UTC 일자 경계와 요청 구간 경계에서 나눈 뒤 조각마다 판정하고, 원인이 같은 인접 조각은 합친다.

판정 순서:
1. 이번 실행에서 이 조각을 포함한 요청이 실패했다 → `rest_failed`
2. 그 날의 아카이브 파일이 적재되었다 → `source_gap` (파일 안에서 행이나 값이 비어 있다)
3. 이번 실행에서 이 조각을 포함한 요청이 성공했다(응답에 데이터가 없다) → 아카이브 공개 예상 시점 전이면
   `awaiting_archive`, 지났으면 `source_gap`
4. 그 날의 아카이브가 체크섬 검증에 실패했다 → `checksum_failed`
5. 아카이브 공개 예상 시점이 지나지 않았다 → 결측이 아니다(기록하지 않는다)
6. metrics이고 REST 보관 기간이 지났다 → `retention_expired`
7. 그 밖 → `archive_missing`
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from coindata.config import API_LIMITS
from coindata.ingest.archive_parse import archive_day_of, archive_day_range
from coindata.ingest.timeutil import day_start_ms
from coindata.models import (
    ALL_FIELDS,
    DAY_MS,
    ArchiveFileStatus,
    Dataset,
    GapRange,
    GapReason,
    TimeRange,
)


@dataclass(frozen=True, slots=True)
class Attempt:
    """이번 실행에서 시도한 수집. `range`는 데이터셋 격자에 맞춘 양끝 포함 구간이다."""

    dataset: Dataset
    fields: tuple[str, ...]
    range: TimeRange
    ok: bool


@dataclass(frozen=True, slots=True)
class ClassifyContext:
    now_ms: int
    publish_delay_days: int
    archive_status: Mapping[date, ArchiveFileStatus]
    attempts: Sequence[Attempt]


def align_range(dataset: Dataset, start_ms: int, end_ms: int) -> TimeRange | None:
    """구간을 데이터셋 격자에 맞춘다(시작은 올림, 끝은 내림). 비면 None."""
    interval = dataset.interval_ms
    start = -(-start_ms // interval) * interval
    end = end_ms // interval * interval
    return TimeRange(start, end) if start <= end else None


def classify_missing(
    dataset: Dataset, symbol: str, field: str, missing: TimeRange, ctx: ClassifyContext
) -> list[GapRange]:
    interval = dataset.interval_ms
    relevant = [a for a in ctx.attempts if a.dataset is dataset and _covers_field(a, field)]
    first_day = archive_day_of(dataset, missing.start_ms)
    days = archive_day_of(dataset, missing.end_ms).toordinal() - first_day.toordinal()
    cuts = {archive_day_range(dataset, first_day + timedelta(days=i)).start_ms for i in range(1, days + 1)}
    for attempt in relevant:
        cuts.add(attempt.range.start_ms)
        cuts.add(attempt.range.end_ms + interval)
    boundaries = sorted(cut for cut in cuts if missing.start_ms < cut <= missing.end_ms)

    result: list[GapRange] = []
    start = missing.start_ms
    for cut in [*boundaries, missing.end_ms + interval]:
        piece = TimeRange(start, cut - interval)
        start = cut
        reason = _reason(dataset, piece, relevant, ctx)
        if reason is None:
            continue
        previous = result[-1] if result else None
        if previous is not None and previous.reason is reason and previous.range.end_ms + interval == piece.start_ms:
            result[-1] = GapRange(dataset, symbol, field, TimeRange(previous.range.start_ms, piece.end_ms), reason)
        else:
            result.append(GapRange(dataset, symbol, field, piece, reason))
    return result


def _covers_field(attempt: Attempt, field: str) -> bool:
    return field == ALL_FIELDS or ALL_FIELDS in attempt.fields or field in attempt.fields


def _contains(outer: TimeRange, inner: TimeRange) -> bool:
    return outer.start_ms <= inner.start_ms and inner.end_ms <= outer.end_ms


def _reason(dataset: Dataset, piece: TimeRange, attempts: Sequence[Attempt], ctx: ClassifyContext) -> GapReason | None:
    covering = [a for a in attempts if _contains(a.range, piece)]
    if any(not a.ok for a in covering):
        return GapReason.REST_FAILED
    day = archive_day_of(dataset, piece.start_ms)
    status = ctx.archive_status.get(day)
    publish_due_ms = day_start_ms(day) + DAY_MS * (1 + ctx.publish_delay_days)
    if status is ArchiveFileStatus.LOADED:
        return GapReason.SOURCE_GAP
    if covering:
        return GapReason.AWAITING_ARCHIVE if ctx.now_ms < publish_due_ms else GapReason.SOURCE_GAP
    if status is ArchiveFileStatus.CHECKSUM_FAILED:
        return GapReason.CHECKSUM_FAILED
    if ctx.now_ms < publish_due_ms:
        return None
    if dataset is Dataset.METRICS_5M and piece.end_ms < ctx.now_ms - API_LIMITS.futures_data_retention_ms:
        return GapReason.RETENTION_EXPIRED
    return GapReason.ARCHIVE_MISSING
