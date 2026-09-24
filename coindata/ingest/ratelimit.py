"""요청 한도 제어 (PRD FR-1.5, CLAUDE.md A-2)."""

from __future__ import annotations

import logging
from collections import deque

from coindata.config import API_LIMITS
from coindata.ingest.http import Clock, HttpResponse, Sleeper
from coindata.models import MINUTE_MS

logger = logging.getLogger(__name__)

USED_WEIGHT_HEADER = "x-mbx-used-weight-1m"


def kline_request_weight(limit: int) -> int:
    """klines·premiumIndexKlines 요청의 추정 가중치. 실제 사용량은 응답 헤더로 추적한다."""
    for max_limit, weight in API_LIMITS.kline_weight_by_limit:
        if limit <= max_limit:
            return weight
    return API_LIMITS.kline_weight_by_limit[-1][1]


class WeightLimiter:
    """분당 요청 가중치를 추적해 설정 비율에 도달하면 다음 분까지 기다린다.

    사용량은 응답 헤더 `X-MBX-USED-WEIGHT-1M`을 기준으로 하고, 헤더를 받기 전에는 추정 가중치를 더한다.
    가중치 0인 요청은 기다리지 않는다.
    """

    def __init__(self, limit_per_minute: int, ratio: float, clock: Clock, sleeper: Sleeper) -> None:
        self._threshold = int(limit_per_minute * ratio)
        self._clock = clock
        self._sleeper = sleeper
        self._minute: int | None = None
        self._used = 0

    def acquire(self, weight: int) -> None:
        if weight <= 0:
            return
        now = self._clock.now_ms()
        self._roll(now)
        if self._used + weight > self._threshold:
            wait_ms = (now // MINUTE_MS + 1) * MINUTE_MS + API_LIMITS.weight_window_margin_ms - now
            logger.info("request weight %d/%d reached; waiting %.1f s", self._used, self._threshold, wait_ms / 1000)
            self._sleeper.sleep(wait_ms / 1000)
            self._roll(self._clock.now_ms())
        self._used += weight

    def observe(self, response: HttpResponse) -> None:
        value = response.headers.get(USED_WEIGHT_HEADER, "").strip()
        if value.isdigit():
            self._roll(self._clock.now_ms())
            self._used = int(value)

    def _roll(self, now_ms: int) -> None:
        minute = now_ms // MINUTE_MS
        if minute != self._minute:
            self._minute = minute
            self._used = 0


class RequestCountLimiter:
    """창 안의 요청 횟수를 세어 설정 비율에 도달하면 가장 오래된 요청이 창을 벗어날 때까지 기다린다.

    `/futures/data/*`의 "IP당 5분 1000회" 한도용이다. 가중치와 무관하게 요청마다 1회로 센다.
    """

    def __init__(self, max_requests: int, window_ms: int, ratio: float, clock: Clock, sleeper: Sleeper) -> None:
        self._allowed = max(1, int(max_requests * ratio))
        self._window_ms = window_ms
        self._clock = clock
        self._sleeper = sleeper
        self._times: deque[int] = deque()

    def acquire(self, weight: int) -> None:
        now = self._clock.now_ms()
        self._evict(now)
        if len(self._times) >= self._allowed:
            wait_ms = self._times[0] + self._window_ms - now
            logger.info("request count %d/%d in window reached; waiting %.1f s", len(self._times), self._allowed, wait_ms / 1000)
            self._sleeper.sleep(wait_ms / 1000)
            now = self._clock.now_ms()
            self._evict(now)
        self._times.append(now)

    def observe(self, response: HttpResponse) -> None:
        return None

    def _evict(self, now_ms: int) -> None:
        while self._times and self._times[0] <= now_ms - self._window_ms:
            self._times.popleft()
