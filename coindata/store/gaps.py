"""결측 탐지와 기록 (PRD FR-2.2, FR-2.3).

탐지는 저장된 행만 보고 빠진 구간을 찾는다. 결측의 원인(reason)은 판정하지 않는다.
원인은 호출자(cli)가 수집 결과로 판정해 `reconcile_gaps`에 넘긴다.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from coindata.models import (
    ALL_FIELDS,
    METRICS_FIELDS,
    Dataset,
    GapRange,
    GapReason,
    ReconcileStats,
    TimeRange,
)
from coindata.store.db import transaction
from coindata.store.query import table_and_time_column


def _align_up(value: int, interval: int) -> int:
    return -(-value // interval) * interval


def _align_down(value: int, interval: int) -> int:
    return value // interval * interval


def missing_row_ranges(conn: sqlite3.Connection, dataset: Dataset, symbol: str, window: TimeRange) -> list[TimeRange]:
    """`window` 안에서 행이 없는 시각 구간을 찾는다. 시각은 데이터셋 간격의 격자로 정렬한다."""
    interval = dataset.interval_ms
    start = _align_up(window.start_ms, interval)
    end = _align_down(window.end_ms, interval)
    if start > end:
        return []
    table, column = table_and_time_column(dataset)
    rows = conn.execute(
        f"SELECT {column} FROM {table} WHERE symbol = ? AND {column} BETWEEN ? AND ? ORDER BY {column}",
        (symbol, start, end),
    )
    ranges: list[TimeRange] = []
    expected = start
    for (ts,) in rows:
        if ts % interval != 0 or ts < expected:
            continue
        if ts > expected:
            ranges.append(TimeRange(expected, ts - interval))
        expected = ts + interval
    if expected <= end:
        ranges.append(TimeRange(expected, end))
    return ranges


def null_field_ranges(conn: sqlite3.Connection, symbol: str, field: str, window: TimeRange) -> list[TimeRange]:
    """`metrics_5m`에서 행은 있으나 `field`가 NULL인 연속 구간을 찾는다."""
    if field not in METRICS_FIELDS:
        raise ValueError(f"metrics 컬럼이 아니다: {field}")
    interval = Dataset.METRICS_5M.interval_ms
    rows = conn.execute(
        f"SELECT ts FROM metrics_5m WHERE symbol = ? AND ts BETWEEN ? AND ? AND {field} IS NULL ORDER BY ts",
        (symbol, window.start_ms, window.end_ms),
    )
    ranges: list[TimeRange] = []
    for (ts,) in rows:
        if ranges and ts == ranges[-1].end_ms + interval:
            ranges[-1] = TimeRange(ranges[-1].start_ms, ts)
        else:
            ranges.append(TimeRange(ts, ts))
    return ranges


def reconcile_gaps(
    conn: sqlite3.Connection,
    dataset: Dataset,
    symbol: str,
    field: str,
    window: TimeRange,
    current: Sequence[GapRange],
    now_ms: int,
) -> ReconcileStats:
    """`window` 안의 미해소 결측 기록을 현재 탐지 결과(`current`)와 맞춘다.

    - 구간과 원인이 그대로인 기록은 유지한다(같은 결측을 중복 기록하지 않는다).
    - 사라지거나 바뀐 기록은 해소 처리한다. 일부만 해소된 경우 남은 구간은 `current`에 새 행으로 들어간다.
    - `window` 밖에 걸친 기록은 건드리지 않는다.
    """
    if field != ALL_FIELDS and field not in METRICS_FIELDS:
        raise ValueError(f"알 수 없는 결측 컬럼: {field}")
    wanted: set[tuple[int, int, GapReason]] = set()
    for gap in current:
        if gap.dataset is not dataset or gap.symbol != symbol or gap.field != field:
            raise ValueError("다른 데이터셋·종목·컬럼의 결측이 섞였다")
        if gap.range.start_ms < window.start_ms or gap.range.end_ms > window.end_ms:
            raise ValueError("결측 구간이 탐지 범위를 벗어났다")
        wanted.add((gap.range.start_ms, gap.range.end_ms, gap.reason))

    inserted: list[GapRange] = []
    resolved = kept = 0
    with transaction(conn):
        existing = conn.execute(
            "SELECT id, start_ms, end_ms, reason FROM data_gap WHERE resolved_at IS NULL "
            "AND dataset = ? AND symbol = ? AND field = ? AND start_ms >= ? AND end_ms <= ?",
            (dataset.value, symbol, field, window.start_ms, window.end_ms),
        ).fetchall()
        for gap_id, start, end, reason in existing:
            key = (start, end, GapReason(reason))
            if key in wanted:
                wanted.remove(key)
                kept += 1
            else:
                conn.execute("UPDATE data_gap SET resolved_at = ? WHERE id = ?", (now_ms, gap_id))
                resolved += 1
        for start, end, reason in sorted(wanted, key=lambda item: item[0]):
            conn.execute(
                "INSERT INTO data_gap (dataset, symbol, field, start_ms, end_ms, reason, detected_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (dataset.value, symbol, field, start, end, reason.value, now_ms),
            )
            inserted.append(GapRange(dataset, symbol, field, TimeRange(start, end), reason))
    return ReconcileStats(inserted=tuple(inserted), resolved=resolved, kept=kept)
