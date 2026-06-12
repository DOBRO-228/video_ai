from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Callable, TypeVar

from openai import APIConnectionError, APITimeoutError, InternalServerError, RateLimitError

from style_kb.clients.provider_diagnostics import response_status_from_error

_T = TypeVar("_T")

RETRYABLE_OPENAI_EXCEPTIONS: tuple[type[BaseException], ...] = (
    APIConnectionError,
    APITimeoutError,
    RateLimitError,
    InternalServerError,
)
RETRYABLE_STATUS_CODES = frozenset({408, 409, 425, 429})


@dataclass(slots=True)
class RetryPolicy:
    max_attempts: int = 5
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 30.0
    jitter: float = 0.25
    retry_after_cap_seconds: float = 120.0

    def delay_for_attempt(self, attempt: int) -> float:
        delay = min(self.base_delay_seconds * (2 ** (attempt - 1)), self.max_delay_seconds)
        offset = delay * self.jitter * (2 * random.random() - 1)
        return max(0.0, delay + offset)

    def delay_for_retry_after(self, retry_after_seconds: float) -> float:
        delay = max(0.0, min(retry_after_seconds, self.retry_after_cap_seconds))
        return min(self.retry_after_cap_seconds, delay + delay * self.jitter * random.random())


OnRetry = Callable[[int, float, BaseException], None]


def call_with_retry(
    func: Callable[[], _T],
    *,
    policy: RetryPolicy,
    on_retry: OnRetry | None = None,
) -> _T:
    last_error: BaseException | None = None
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return func()
        except Exception as error:
            if not is_retryable_error(error):
                raise
            last_error = error
            if attempt >= policy.max_attempts:
                break
            server_hint = _retry_after_seconds(error)
            if server_hint is not None:
                delay = policy.delay_for_retry_after(server_hint)
            else:
                delay = policy.delay_for_attempt(attempt)
            if on_retry is not None:
                on_retry(attempt, delay, error)
            time.sleep(delay)
    assert last_error is not None
    raise last_error


def is_retryable_error(error: BaseException) -> bool:
    if isinstance(error, RETRYABLE_OPENAI_EXCEPTIONS):
        return True
    status_code = response_status_from_error(error)
    if status_code is None:
        return False
    return status_code in RETRYABLE_STATUS_CODES or status_code >= 500


def _retry_after_seconds(error: BaseException) -> float | None:
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    retry_after_ms = _header_value(headers, "retry-after-ms")
    if retry_after_ms is not None:
        try:
            return max(0.0, float(retry_after_ms) / 1000.0)
        except (TypeError, ValueError):
            pass
    retry_after = _header_value(headers, "retry-after")
    if retry_after is not None:
        try:
            return max(0.0, float(retry_after))
        except (TypeError, ValueError):
            return None
    return None


def _header_value(headers: object, name: str) -> str | None:
    getter = getattr(headers, "get", None)
    if callable(getter):
        value = getter(name) or getter(name.lower()) or getter(name.upper())
        if value is not None:
            return str(value)
    return None
