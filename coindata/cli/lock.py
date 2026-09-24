"""프로세스 잠금 (PRD NFR-3.5). 저장소에 쓰는 명령은 동시에 하나만 실행한다."""

from __future__ import annotations

import os
from pathlib import Path
from types import TracebackType
from typing import IO


class LockError(Exception):
    """다른 프로세스가 잠금을 가지고 있다."""


class ProcessLock:
    """잠금 파일에 OS 수준의 배타 잠금을 건다. 프로세스가 끝나면 OS가 잠금을 풀어준다."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: IO[bytes] | None = None

    def __enter__(self) -> ProcessLock:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise LockError(f"다른 명령이 저장소를 사용 중이다 (잠금 파일: {self._path})") from exc
        self._handle = handle
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._handle is None:
            return
        if os.name == "nt":
            import msvcrt

            self._handle.seek(0)
            msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None
