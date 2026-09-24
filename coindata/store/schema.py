"""저장소 스키마 (PRD 9.2).

구간 컬럼(`data_gap.start_ms`, `end_ms`)은 봉의 open_time 기준 양끝 포함이다.
"""

from __future__ import annotations

import sqlite3

from coindata.models import ArchiveFileStatus, Dataset, GapReason, RunMode, RunStatus, SummaryTrigger
from coindata.store.db import StoreError, transaction

SCHEMA_VERSION = 2  # 2: summary_log.trigger에 historical 추가


def _values(enum_type: type) -> str:
    return ", ".join(f"'{member.value}'" for member in enum_type)


_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS kline_1m (
        symbol TEXT NOT NULL,
        open_time INTEGER NOT NULL,
        open REAL NOT NULL,
        high REAL NOT NULL,
        low REAL NOT NULL,
        close REAL NOT NULL,
        volume REAL NOT NULL,
        quote_volume REAL NOT NULL,
        trade_count INTEGER NOT NULL,
        taker_buy_volume REAL NOT NULL,
        taker_buy_quote_volume REAL NOT NULL,
        source TEXT NOT NULL CHECK (source IN ('archive', 'rest')),
        ingested_at INTEGER NOT NULL,
        PRIMARY KEY (symbol, open_time)
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE IF NOT EXISTS premium_index_1m (
        symbol TEXT NOT NULL,
        open_time INTEGER NOT NULL,
        open REAL NOT NULL,
        high REAL NOT NULL,
        low REAL NOT NULL,
        close REAL NOT NULL,
        sample_count INTEGER,
        source TEXT NOT NULL CHECK (source IN ('archive', 'rest')),
        ingested_at INTEGER NOT NULL,
        PRIMARY KEY (symbol, open_time)
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE IF NOT EXISTS metrics_5m (
        symbol TEXT NOT NULL,
        ts INTEGER NOT NULL,
        sum_open_interest REAL,
        sum_open_interest_value REAL,
        top_position_ratio REAL,
        top_account_ratio REAL,
        global_account_ratio REAL,
        taker_buy_sell_ratio REAL,
        source TEXT NOT NULL CHECK (source IN ('archive', 'rest')),
        ingested_at INTEGER NOT NULL,
        PRIMARY KEY (symbol, ts)
    ) WITHOUT ROWID
    """,
    f"""
    CREATE TABLE IF NOT EXISTS archive_file (
        dataset TEXT NOT NULL CHECK (dataset IN ({_values(Dataset)})),
        symbol TEXT NOT NULL,
        file_date TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ({_values(ArchiveFileStatus)})),
        sha256 TEXT,
        row_count INTEGER,
        attempted_at INTEGER NOT NULL,
        loaded_at INTEGER,
        PRIMARY KEY (dataset, symbol, file_date)
    ) WITHOUT ROWID
    """,
    f"""
    CREATE TABLE IF NOT EXISTS data_gap (
        id INTEGER PRIMARY KEY,
        dataset TEXT NOT NULL CHECK (dataset IN ({_values(Dataset)})),
        symbol TEXT NOT NULL,
        field TEXT NOT NULL,
        start_ms INTEGER NOT NULL,
        end_ms INTEGER NOT NULL CHECK (end_ms >= start_ms),
        reason TEXT NOT NULL CHECK (reason IN ({_values(GapReason)})),
        detected_at INTEGER NOT NULL,
        resolved_at INTEGER
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_data_gap_open
        ON data_gap (dataset, symbol, field, start_ms)
        WHERE resolved_at IS NULL
    """,
    f"""
    CREATE TABLE IF NOT EXISTS ingest_run (
        id INTEGER PRIMARY KEY,
        mode TEXT NOT NULL CHECK (mode IN ({_values(RunMode)})),
        started_at INTEGER NOT NULL,
        finished_at INTEGER,
        status TEXT CHECK (status IS NULL OR status IN ({_values(RunStatus)})),
        detail TEXT
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS summary_log (
        summary_id TEXT PRIMARY KEY,
        created_at INTEGER NOT NULL,
        "trigger" TEXT NOT NULL CHECK ("trigger" IN ({_values(SummaryTrigger)})),
        ref_time INTEGER NOT NULL,
        ref_price REAL NOT NULL,
        params_hash TEXT NOT NULL,
        state TEXT NOT NULL,
        file_path TEXT NOT NULL
    )
    """,
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    """테이블이 없으면 만든다. 이미 있으면 아무것도 바꾸지 않는다."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise StoreError(f"저장소 스키마 버전({version})이 이 프로그램({SCHEMA_VERSION})보다 새롭다")
    with transaction(conn):
        if version == 1:
            _migrate_v1_summary_log(conn)
        for ddl in _DDL:
            conn.execute(ddl)
        if version == 1:
            conn.execute(
                'INSERT INTO summary_log SELECT summary_id, created_at, "trigger", ref_time, ref_price, params_hash, '
                "state, file_path FROM summary_log_v1"
            )
            conn.execute("DROP TABLE summary_log_v1")
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _migrate_v1_summary_log(conn: sqlite3.Connection) -> None:
    """버전 1의 summary_log는 trigger 제약이 'manual'뿐이다. 제약을 바꾸려면 테이블을 다시 만들어야 한다."""
    conn.execute("ALTER TABLE summary_log RENAME TO summary_log_v1")
