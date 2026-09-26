"""적재 (PRD FR-1.7, FR-2.1, FR-2.4, FR-2.6).

- `kline_1m`, `premium_index_1m`: 마감된 봉만 받는다. 기본키가 충돌하면 기존 행을 유지한다.
- `metrics_5m`: 컬럼 단위로 병합한다. 기존 행의 NULL 칸만 채우고 NULL이 아닌 값은 바꾸지 않는다.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from typing import cast

from coindata.models import (
    ArchiveDay,
    ArchiveFileStatus,
    ArchiveOutcome,
    Dataset,
    Kline,
    METRICS_FIELDS,
    MetricsRow,
    PremiumKline,
    Row,
    RunMode,
    RunStatus,
    Source,
    SummaryRecord,
)
from coindata.store.db import transaction

_INSERT_KLINE = """
    INSERT INTO kline_1m (
        symbol, open_time, open, high, low, close, volume, quote_volume, trade_count,
        taker_buy_volume, taker_buy_quote_volume, source, ingested_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT (symbol, open_time) DO NOTHING
"""

_INSERT_PREMIUM = """
    INSERT INTO premium_index_1m (
        symbol, open_time, open, high, low, close, sample_count, source, ingested_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT (symbol, open_time) DO NOTHING
"""

_MERGE_METRICS = f"""
    INSERT INTO metrics_5m (symbol, ts, {", ".join(METRICS_FIELDS)}, source, ingested_at)
    VALUES (?, ?, {", ".join("?" for _ in METRICS_FIELDS)}, ?, ?)
    ON CONFLICT (symbol, ts) DO UPDATE SET
        {", ".join(f"{f} = COALESCE(metrics_5m.{f}, excluded.{f})" for f in METRICS_FIELDS)},
        ingested_at = excluded.ingested_at
    WHERE {" OR ".join(f"(metrics_5m.{f} IS NULL AND excluded.{f} IS NOT NULL)" for f in METRICS_FIELDS)}
"""

_UPSERT_ARCHIVE_FILE = """
    INSERT INTO archive_file (
        dataset, symbol, file_date, status, sha256, row_count, attempted_at, loaded_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT (dataset, symbol, file_date) DO UPDATE SET
        status = excluded.status,
        sha256 = excluded.sha256,
        row_count = excluded.row_count,
        attempted_at = excluded.attempted_at,
        loaded_at = excluded.loaded_at
"""


def _insert_rows(conn: sqlite3.Connection, dataset: Dataset, rows: Sequence[Row], source: Source, ingested_at: int) -> int:
    """호출자의 트랜잭션 안에서 행을 적재하고, 새로 들어가거나 칸이 채워진 행 수를 돌려준다."""
    before = conn.total_changes
    if dataset is Dataset.KLINE_1M:
        conn.executemany(
            _INSERT_KLINE,
            (
                (
                    k.symbol, k.open_time, k.open, k.high, k.low, k.close, k.volume, k.quote_volume,
                    k.trade_count, k.taker_buy_volume, k.taker_buy_quote_volume, source.value, ingested_at,
                )
                for k in cast(Sequence[Kline], rows)
            ),
        )
    elif dataset is Dataset.PREMIUM_INDEX_1M:
        conn.executemany(
            _INSERT_PREMIUM,
            (
                (p.symbol, p.open_time, p.open, p.high, p.low, p.close, p.sample_count, source.value, ingested_at)
                for p in cast(Sequence[PremiumKline], rows)
            ),
        )
    else:
        conn.executemany(
            _MERGE_METRICS,
            (
                (m.symbol, m.ts, *(getattr(m, f) for f in METRICS_FIELDS), source.value, ingested_at)
                for m in cast(Sequence[MetricsRow], rows)
            ),
        )
    return conn.total_changes - before


def store_rest_rows(conn: sqlite3.Connection, dataset: Dataset, rows: Sequence[Row], ingested_at: int) -> int:
    """REST로 받은 행을 한 트랜잭션으로 적재한다."""
    with transaction(conn):
        return _insert_rows(conn, dataset, rows, Source.REST, ingested_at)


def store_archive_day(conn: sqlite3.Connection, day: ArchiveDay, attempted_at: int, ingested_at: int) -> int:
    """아카이브 파일 하나의 결과를 기록한다.

    검증된 파일은 행 적재와 `archive_file`의 `loaded` 기록을 한 트랜잭션으로 수행한다(FR-2.6).
    커밋 전에 중단되면 둘 다 남지 않으므로 재실행 시 다시 적재된다.
    """
    with transaction(conn):
        changed = 0
        if day.outcome is ArchiveOutcome.VERIFIED:
            changed = _insert_rows(conn, day.dataset, day.rows, Source.ARCHIVE, ingested_at)
            status, row_count, loaded_at = ArchiveFileStatus.LOADED, len(day.rows), ingested_at
        elif day.outcome is ArchiveOutcome.NOT_PUBLISHED:
            status, row_count, loaded_at = ArchiveFileStatus.NOT_PUBLISHED, None, None
        else:
            status, row_count, loaded_at = ArchiveFileStatus.CHECKSUM_FAILED, None, None
        conn.execute(
            _UPSERT_ARCHIVE_FILE,
            (
                day.dataset.value, day.symbol, day.day.isoformat(), status.value,
                day.sha256 if status is ArchiveFileStatus.LOADED else None,
                row_count, attempted_at, loaded_at,
            ),
        )
    return changed


def start_run(conn: sqlite3.Connection, mode: RunMode, started_at: int) -> int:
    with transaction(conn):
        cursor = conn.execute("INSERT INTO ingest_run (mode, started_at) VALUES (?, ?)", (mode.value, started_at))
    return cast(int, cursor.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, status: RunStatus, finished_at: int, detail_json: str) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE ingest_run SET status = ?, finished_at = ?, detail = ? WHERE id = ?",
            (status.value, finished_at, detail_json, run_id),
        )


def insert_summary(conn: sqlite3.Connection, record: SummaryRecord) -> None:
    """요약 기록 (FR-4.5). 같은 요약 ID가 있으면 기본키 충돌로 실패한다."""
    with transaction(conn):
        conn.execute(
            'INSERT INTO summary_log (summary_id, created_at, "trigger", ref_time, ref_price, params_hash, state, file_path, '
            "params) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.summary_id, record.created_at, record.trigger.value, record.ref_time, record.ref_price,
                record.params_hash, record.state, record.file_path, record.params,
            ),
        )
