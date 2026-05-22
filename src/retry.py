"""Retry decorator for transient HTTP errors."""

from __future__ import annotations

import logging

import httpx
from lago_python_client.exceptions import LagoApiError
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)


def _is_transient(exc: BaseException) -> bool:
    """Determine if an exception is transient and worth retrying."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 500, 502, 503, 504)
    if isinstance(exc, (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout)):
        return True
    if isinstance(exc, LagoApiError):
        return exc.status_code in (429, 500, 502, 503, 504)
    return False


with_retry = retry(
    retry=retry_if_exception(_is_transient),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    before_sleep=lambda retry_state: logger.warning(
        "Retrying after %s (attempt %d)", retry_state.outcome.exception(), retry_state.attempt_number
    ),
    reraise=True,
)
