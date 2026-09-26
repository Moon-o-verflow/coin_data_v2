"""`status` 명령의 출력 (PRD UF-4, FR-2.5, NFR-4.4)."""

from __future__ import annotations

import sqlite3
import unicodedata
from pathlib import Path

from coindata.ingest.timeutil import format_ms
from coindata.models import ALL_FIELDS, Dataset, OpenGap
from coindata.store import query


def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _left(text: str, width: int) -> str:
    return text + " " * max(0, width - _width(text))


def _right(text: str, width: int) -> str:
    return " " * max(0, width - _width(text)) + text


def _count(span_start: int, span_end: int, interval: int) -> int:
    return (span_end - span_start) // interval + 1


def _ms(value: int | None) -> str:
    return format_ms(value) if value is not None else "-"


def render_status(conn: sqlite3.Connection, symbol: str, db_path: Path) -> str:
    lines = [f"저장소: {db_path} ({symbol})", ""]
    open_gaps = query.open_gaps(conn, symbol)

    lines.append(
        _left("데이터셋", 18) + _right("행 수", 10) + "  " + _left("시작(UTC)", 18) + _left("끝(UTC)", 18)
        + _left("최종 적재(UTC)", 18) + _right("결측 행", 8) + _right("빈 칸", 8)
    )
    for status in query.dataset_statuses(conn, symbol):
        interval = status.dataset.interval_ms
        dataset_gaps = [g for g in open_gaps if g.dataset is status.dataset]
        missing_rows = sum(_count(g.range.start_ms, g.range.end_ms, interval) for g in dataset_gaps if g.field == ALL_FIELDS)
        empty_cells = sum(_count(g.range.start_ms, g.range.end_ms, interval) for g in dataset_gaps if g.field != ALL_FIELDS)
        lines.append(
            _left(status.dataset.value, 18) + _right(f"{status.row_count:,}", 10) + "  "
            + _left(_ms(status.first_ms), 18) + _left(_ms(status.last_ms), 18) + _left(_ms(status.last_ingested_at), 18)
            + _right(f"{missing_rows:,}", 8) + _right(f"{empty_cells:,}", 8)
        )

    counts = query.archive_status_counts(conn, symbol)
    lines += ["", "아카이브 파일"]
    if not counts:
        lines.append("  기록 없음")
    for dataset in Dataset:
        parts = [f"{c.status.value} {c.count}" for c in counts if c.dataset is dataset]
        if parts:
            lines.append(f"  {dataset.value}: {', '.join(parts)}")

    lines += ["", f"미해소 결측 ({len(open_gaps)}건)"]
    lines += [_gap_line(gap) for gap in open_gaps]

    run = query.last_run(conn)
    lines.append("")
    if run is None:
        lines.append("마지막 실행: 없음")
    else:
        finished = _ms(run.finished_at) if run.finished_at is not None else "진행 중이거나 비정상 종료"
        status_text = run.status.value if run.status is not None else "기록 없음"
        lines.append(f"마지막 실행: {run.mode.value}, {_ms(run.started_at)} ~ {finished}, {status_text}")
    return "\n".join(lines)


def _gap_line(gap: OpenGap) -> str:
    field = "(행 전체)" if gap.field == ALL_FIELDS else gap.field
    return (
        "  " + _left(gap.dataset.value, 18) + _left(field, 24)
        + f"{format_ms(gap.range.start_ms)} ~ {format_ms(gap.range.end_ms)}  {gap.reason.value}"
    )
