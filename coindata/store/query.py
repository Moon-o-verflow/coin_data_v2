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
    Kline,
    LatestMetric,
    METRICS_FIELDS,
    MetricsRow,
    OpenGap,
    PremiumKline,
    RunMode,
    RunRecord,
    RunStatus,
    SummaryRecord,
    SummaryTrigger,
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


# ---------------------------------------------------------------------------
# 계산용 조회 (compute가 쓰는 인터페이스). 구간은 [start_ms, end_ms) 반열림이며 시각 오름차순이다.
# ---------------------------------------------------------------------------


def klines_between(conn: sqlite3.Connection, symbol: str, start_ms: int, end_ms: int) -> list[Kline]:
    rows = conn.execute(
        "SELECT open_time, open, high, low, close, volume, quote_volume, trade_count, taker_buy_volume, "
        "taker_buy_quote_volume FROM kline_1m WHERE symbol = ? AND open_time >= ? AND open_time < ? ORDER BY open_time",
        (symbol, start_ms, end_ms),
    )
    return [Kline(symbol, *row) for row in rows]


def premium_between(conn: sqlite3.Connection, symbol: str, start_ms: int, end_ms: int) -> list[PremiumKline]:
    rows = conn.execute(
        "SELECT open_time, open, high, low, close, sample_count FROM premium_index_1m "
        "WHERE symbol = ? AND open_time >= ? AND open_time < ? ORDER BY open_time",
        (symbol, start_ms, end_ms),
    )
    return [PremiumKline(symbol, *row) for row in rows]


def metrics_between(conn: sqlite3.Connection, symbol: str, start_ms: int, end_ms: int) -> list[MetricsRow]:
    """`ts`가 [start_ms, end_ms]인 행. metrics의 ts는 구간 끝 시각이므로 끝을 포함한다."""
    rows = conn.execute(
        f"SELECT ts, {', '.join(METRICS_FIELDS)} FROM metrics_5m WHERE symbol = ? AND ts >= ? AND ts <= ? ORDER BY ts",
        (symbol, start_ms, end_ms),
    )
    return [MetricsRow(symbol, *row) for row in rows]


def latest_metrics(conn: sqlite3.Connection, symbol: str, until_ms: int) -> list[LatestMetric]:
    """컬럼마다 `ts <= until_ms`이고 값이 NULL이 아닌 가장 최근 행의 값."""
    result = []
    for name in METRICS_FIELDS:
        row = conn.execute(
            f"SELECT {name}, ts FROM metrics_5m WHERE symbol = ? AND ts <= ? AND {name} IS NOT NULL ORDER BY ts DESC LIMIT 1",
            (symbol, until_ms),
        ).fetchone()
        result.append(LatestMetric(name, row[0], row[1]) if row else LatestMetric(name, None, None))
    return result


def gaps_overlapping(conn: sqlite3.Connection, symbol: str, start_ms: int, end_ms: int) -> list[OpenGap]:
    """[start_ms, end_ms]와 겹치는 미해소 결측."""
    return [g for g in open_gaps(conn, symbol) if g.range.start_ms <= end_ms and g.range.end_ms >= start_ms]


def summary_exists(conn: sqlite3.Connection, summary_id: str) -> bool:
    return conn.execute("SELECT 1 FROM summary_log WHERE summary_id = ?", (summary_id,)).fetchone() is not None


def previous_summary(conn: sqlite3.Connection, trigger: SummaryTrigger, ref_time: int) -> SummaryRecord | None:
    """FR-4.4, FR-4.8의 직전 요약.

    - manual: manual 기록 중 가장 최근에 만든 것.
    - historical: historical 기록 중 기준 시각이 `ref_time`보다 앞선 것 가운데 기준 시각이 가장 늦은 것
      (같으면 생성 시각이 늦은 것).
    """
    columns = 'summary_id, created_at, "trigger", ref_time, ref_price, params_hash, state, file_path, params'
    if trigger is SummaryTrigger.MANUAL:
        row = conn.execute(
            f'SELECT {columns} FROM summary_log WHERE "trigger" = ? ORDER BY created_at DESC, summary_id DESC LIMIT 1',
            (trigger.value,),
        ).fetchone()
    else:
        row = conn.execute(
            f'SELECT {columns} FROM summary_log WHERE "trigger" = ? AND ref_time < ? '
            "ORDER BY ref_time DESC, created_at DESC, summary_id DESC LIMIT 1",
            (trigger.value, ref_time),
        ).fetchone()
    if row is None:
        return None
    sid, created, trig, ref, price, params_hash, state, path, params = row
    return SummaryRecord(sid, created, SummaryTrigger(trig), ref, price, params_hash, state, path, params)


def get_summary(conn: sqlite3.Connection, summary_id: str) -> SummaryRecord | None:
    row = conn.execute(
        'SELECT summary_id, created_at, "trigger", ref_time, ref_price, params_hash, state, file_path, params '
        "FROM summary_log WHERE summary_id = ?",
        (summary_id,),
    ).fetchone()
    if row is None:
        return None
    sid, created, trig, ref, price, params_hash, state, path, params = row
    return SummaryRecord(sid, created, SummaryTrigger(trig), ref, price, params_hash, state, path, params)
