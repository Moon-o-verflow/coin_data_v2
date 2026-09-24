"""SQLite 연결과 트랜잭션."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class StoreError(Exception):
    """저장소를 열 수 없거나 저장소 상태가 예상과 다르다."""


def open_db(path: Path, busy_timeout_ms: int) -> sqlite3.Connection:
    """저장소를 연다. 파일이 없으면 만든다.

    트랜잭션은 `transaction()`으로 명시적으로 시작한다(자동 트랜잭션을 쓰지 않는다).
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, isolation_level=None)
        conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        conn.execute("PRAGMA journal_mode = WAL")
    except (sqlite3.Error, OSError) as exc:
        raise StoreError(f"저장소를 열 수 없다: {path}: {exc}") from exc
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """쓰기 트랜잭션. 블록이 예외로 끝나면 롤백한다."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
