"""작업 스레드 (PRD FR-8.4).

오래 걸리는 명령을 한 번에 하나씩 작업 스레드에서 실행하고, 결과·진행 메시지를 큐에 넣는다.
화면 갱신은 주 스레드가 `drain`으로 큐를 비우며 한다. tkinter에 의존하지 않는다.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Done:
    name: str
    value: Any
    error: BaseException | None
    on_done: Callable[[Any], None]
    on_error: Callable[[BaseException], None]


@dataclass(frozen=True, slots=True)
class Message:
    text: str


class Worker:
    def __init__(self) -> None:
        self.events: queue.Queue[Done | Message] = queue.Queue()
        self._busy = threading.Event()

    @property
    def busy(self) -> bool:
        return self._busy.is_set()

    def message(self, text: str) -> None:
        """어느 스레드에서든 부를 수 있다."""
        self.events.put(Message(text))

    def submit(
        self,
        name: str,
        job: Callable[[], Any],
        on_done: Callable[[Any], None],
        on_error: Callable[[BaseException], None],
    ) -> bool:
        """작업을 시작한다. 이미 실행 중이면 시작하지 않고 False."""
        if self._busy.is_set():
            return False
        self._busy.set()
        threading.Thread(target=self._run, args=(name, job, on_done, on_error), name=f"coindata-{name}", daemon=True).start()
        return True

    def _run(
        self, name: str, job: Callable[[], Any], on_done: Callable[[Any], None], on_error: Callable[[BaseException], None]
    ) -> None:
        try:
            value = job()
        except Exception as exc:
            # 작업 실패를 화면에 알리기 위한 최상위 처리다. 원인은 로그에 남긴다(CLAUDE.md C-5).
            logger.debug("job %s failed", name, exc_info=True)
            self.events.put(Done(name, None, exc, on_done, on_error))
        else:
            self.events.put(Done(name, value, None, on_done, on_error))
        finally:
            self._busy.clear()

    def drain(self, on_message: Callable[[str], None]) -> None:
        """주 스레드에서 부른다. 쌓인 메시지와 완료 콜백을 처리한다."""
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                return
            if isinstance(event, Message):
                on_message(event.text)
            elif event.error is not None:
                event.on_error(event.error)
            else:
                event.on_done(event.value)


class QueueLogHandler(logging.Handler):
    """로그를 작업 큐의 메시지로 보낸다(C-6). 화면 하단 칸에 표시된다."""

    def __init__(self, worker: Worker) -> None:
        super().__init__()
        self._worker = worker

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._worker.message(self.format(record))
        except Exception:  # logging.Handler 규약: 처리 실패는 handleError로 넘긴다
            self.handleError(record)
