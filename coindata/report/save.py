"""요약 파일 저장과 `summary_log` 기록 (FR-4.5)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from coindata.models import SummaryRecord, SummaryTrigger
from coindata.report.summary import BuiltSummary, serialize
from coindata.store import query, writer


class SummaryExistsError(Exception):
    """같은 요약 ID의 파일이나 기록이 이미 있다."""


def save_summary(
    conn: sqlite3.Connection,
    output_dir: Path,
    built: BuiltSummary,
    summary_id: str,
    created_at: int,
    trigger: SummaryTrigger,
    ref_time: int,
    ref_price: float,
) -> Path:
    """파일을 먼저 쓰고 기록을 남긴다. 기록에 실패하면 쓴 파일을 지운다."""
    path = output_dir / f"{summary_id}.json"
    if path.exists() or query.summary_exists(conn, summary_id):
        raise SummaryExistsError(f"같은 요약 ID가 이미 있다: {summary_id}")
    output_dir.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(serialize(built.document), encoding="utf-8")
    temp.replace(path)
    record = SummaryRecord(summary_id, created_at, trigger, ref_time, ref_price, built.params_hash, built.state_json, str(path))
    try:
        writer.insert_summary(conn, record)
    except sqlite3.Error:
        path.unlink(missing_ok=True)
        raise
    return path
