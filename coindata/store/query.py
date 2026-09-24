"""조회 (PRD FR-2.5)."""

from __future__ import annotations

import sqlite3
from datetime import date

from coindata.models import (
    ArchiveFileStatus,
    ArchiveStatusCount,
    Dataset,
    DatasetStatus,
    GapReason,
    OpenGap,
    RunMode,
    RunRecord,
    RunStatus,
    TimeRange,
)

_TIME_COLUMN = {
    Dataset.KLINE_1M: "open_time",
    Dataset.PREMIUM_INDEX_1M: "open_time",
    Dataset.METRICS_5M: "ts",
}


def table_and_time_column(dataset: Dataset) -> tuple[str, str]:
    return dataset.value, _TIME_COLUMN[dataset]


def time_bounds(conn: sqlite3.Connection, dataset: Dataset, symbol: str) -> TimeRange | None:
    """저장된 행의 최초·최종 시각. 행이 없으면 None."""
    table, column = table_and_time_column(dataset)
    first, last = conn.execute(f"SELECT MIN({column}), MAX({column}) FROM {table} WHERE symbol = ?", (symbol,)).fetchone()
    if first is None:
        return None
    return TimeRange(first, last)


def archive_statuses(
    conn: sqlite3.Connection, dataset: Dataset, symbol: str, first_day: date, last_day: date
) -> dict[date, ArchiveFileStatus]:
    rows = conn.execute(
        "SELECT file_date, status FROM archive_file WHERE dataset = ? AND symbol = ? AND file_date BETWEEN ? AND ?",
        (dataset.value, symbol, first_day.isoformat(), last_day.isoformat()),
    )
    return {date.fromisoformat(file_date): ArchiveFileStatus(status) for file_date, status in rows}


def open_gaps(conn: sqlite3.Connection, symbol: str, dataset: Dataset | None = None) -> list[OpenGap]:
    sql = (
        "SELECT id, dataset, symbol, field, start_ms, end_ms, reason, detected_at FROM data_gap "
        "WHERE resolved_at IS NULL AND symbol = ?"
    )
    params: tuple[object, ...] = (symbol,)
    if dataset is not None:
        sql += " AND dataset = ?"
        params += (dataset.value,)
    sql += " ORDER BY dataset, field, start_ms"
    return [
        OpenGap(gid, Dataset(ds), sym, fld, TimeRange(start, end), GapReason(reason), detected)
        for gid, ds, sym, fld, start, end, reason, detected in conn.execute(sql, params)
    ]


def dataset_statuses(conn: sqlite3.Connection, symbol: str) -> list[DatasetStatus]:
    result = []
    for dataset in Dataset:
        table, column = table_and_time_column(dataset)
        count, first, last, ingested = conn.execute(
            f"SELECT COUNT(*), MIN({column}), MAX({column}), MAX(ingested_at) FROM {table} WHERE symbol = ?",
            (symbol,),
        ).fetchone()
        (gap_count,) = conn.execute(
            "SELECT COUNT(*) FROM data_gap WHERE resolved_at IS NULL AND symbol = ? AND dataset = ?",
            (symbol, dataset.value),
        ).fetchone()
        result.append(DatasetStatus(dataset, count, first, last, ingested, gap_count))
    return result


def archive_status_counts(conn: sqlite3.Connection, symbol: str) -> list[ArchiveStatusCount]:
    rows = conn.execute(
        "SELECT dataset, status, COUNT(*) FROM archive_file WHERE symbol = ? GROUP BY dataset, status ORDER BY dataset, status",
        (symbol,),
    )
    return [ArchiveStatusCount(Dataset(ds), ArchiveFileStatus(status), count) for ds, status, count in rows]


def last_run(conn: sqlite3.Connection) -> RunRecord | None:
    row = conn.execute(
        "SELECT id, mode, started_at, finished_at, status, detail FROM ingest_run ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    run_id, mode, started, finished, status, detail = row
    return RunRecord(run_id, RunMode(mode), started, finished, RunStatus(status) if status else None, detail)
