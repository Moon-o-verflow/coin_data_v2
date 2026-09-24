"""HTTP 전송, 재시도, 요청 기록 (PRD FR-1.6, NFR-4.1, NFR-9.2, CLAUDE.md A-3, A-4, C-7).

전송(`HttpTransport`), 시계(`Clock`), 대기(`Sleeper`)는 교체할 수 있어서 네트워크 없이 검증할 수 있다.
"""

from __future__ import annotations

import http.client
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]  # 키는 소문자
    body: bytes


class TransportError(Exception):
    """네트워크 수준의 실패(연결, 시간 초과 등). 재시도 대상이다."""


class RequestFailedError(Exception):
    """재시도를 모두 소진했거나 재시도하지 않는 응답을 받았다."""

    def __init__(self, label: str, params: str, reason: str) -> None:
        super().__init__(f"{label} [{params}]: {reason}")
        self.label = label
        self.params = params
        self.reason = reason


class HttpTransport(Protocol):
    def get(self, url: str, timeout_seconds: float) -> HttpResponse: ...


class Clock(Protocol):
    def now_ms(self) -> int: ...


class Sleeper(Protocol):
    def sleep(self, seconds: float) -> None: ...


class UrllibTransport:
    """표준 라이브러리 urllib 기반 전송. HTTP 오류 상태도 예외가 아니라 응답으로 돌려준다."""

    def __init__(self, user_agent: str = "coindata/1") -> None:
        self._headers = {"User-Agent": user_agent}

    def get(self, url: str, timeout_seconds: float) -> HttpResponse:
        request = urllib.request.Request(url, headers=self._headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                return HttpResponse(response.status, _lower_headers(response.headers.items()), response.read())
        except urllib.error.HTTPError as exc:
            headers = _lower_headers(exc.headers.items()) if exc.headers is not None else {}
            return HttpResponse(exc.code, headers, exc.read())
        except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
            raise TransportError(str(exc)) from exc


def _lower_headers(items: Sequence[tuple[str, str]]) -> dict[str, str]:
    return {key.lower(): value for key, value in items}


class SystemClock:
    def now_ms(self) -> int:
        return time.time_ns() // 1_000_000


class SystemSleeper:
    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class RequestLimiter(Protocol):
    def acquire(self, weight: int) -> None: ...

    def observe(self, response: HttpResponse) -> None: ...


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_retries: int
    backoff_initial_seconds: float
    backoff_max_seconds: float

    def backoff_seconds(self, retry_number: int) -> float:
        """지수 백오프. `retry_number`는 1부터 센다."""
        return min(self.backoff_initial_seconds * 2 ** (retry_number - 1), self.backoff_max_seconds)


class RequestExecutor:
    """GET 요청을 보내고 실패를 분류해 재시도한다.

    - 네트워크 오류와 5xx: 지수 백오프로 재시도한다.
    - 429: `Retry-After`만큼 기다린 뒤 재시도한다. 헤더가 없으면 백오프를 쓴다.
    - 그 밖의 응답(2xx, 404 등 4xx): 재시도하지 않고 그대로 돌려준다. 해석은 호출자가 한다.
    """

    def __init__(self, transport: HttpTransport, retry: RetryPolicy, sleeper: Sleeper, timeout_seconds: float) -> None:
        self._transport = transport
        self._retry = retry
        self._sleeper = sleeper
        self._timeout = timeout_seconds

    def get(
        self,
        url: str,
        label: str,
        params: str,
        limiters: Sequence[RequestLimiter] = (),
        weight: int = 0,
    ) -> HttpResponse:
        retries = 0
        while True:
            for limiter in limiters:
                limiter.acquire(weight)
            started = time.monotonic()
            wait_seconds: float | None = None
            try:
                response = self._transport.get(url, self._timeout)
            except TransportError as exc:
                elapsed_ms = int((time.monotonic() - started) * 1000)
                logger.warning("GET %s [%s] -> network error after %d ms: %s", label, params, elapsed_ms, exc)
                reason = f"network error: {exc}"
            else:
                elapsed_ms = int((time.monotonic() - started) * 1000)
                logger.info("GET %s [%s] -> %d (%d ms)", label, params, response.status, elapsed_ms)
                for limiter in limiters:
                    limiter.observe(response)
                if response.status != 429 and response.status < 500:
                    return response
                reason = f"HTTP {response.status}"
                if response.status == 429:
                    wait_seconds = _retry_after_seconds(response)
            retries += 1
            if retries > self._retry.max_retries:
                raise RequestFailedError(label, params, f"{reason} (재시도 {self._retry.max_retries}회 소진)")
            delay = wait_seconds if wait_seconds is not None else self._retry.backoff_seconds(retries)
            logger.info("retrying %s in %.1f s (retry %d/%d)", label, delay, retries, self._retry.max_retries)
            self._sleeper.sleep(delay)


def _retry_after_seconds(response: HttpResponse) -> float | None:
    value = response.headers.get("retry-after", "").strip()
    return float(value) if value.isdigit() else None
