"""Minimal Habitica API v3 client with request spacing and retries."""

import logging
import time
from collections.abc import Callable, Sequence
from types import TracebackType
from typing import Any, Self

import httpx

from hns.config import HabiticaCredentials

logger = logging.getLogger(__name__)

BASE_URL = "https://habitica.com/api/v3"
CLIENT_NAME = "HabiticaNotionSync"

# Habitica's API usage guidelines ask automated scripts to wait 30 s between calls.
MIN_REQUEST_INTERVAL = 30.0
MAX_ATTEMPTS = 4
BACKOFF_BASE = 2.0
MAX_RETRY_DELAY = 120.0
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

# Fetching the whole user document would also return the inbox, party, items, etc.
DEFAULT_USER_FIELDS = ("stats", "preferences", "lastCron")


class HabiticaError(Exception):
    """Raised when the Habitica API returns an error or cannot be reached."""


class HabiticaClient:
    def __init__(
        self,
        credentials: HabiticaCredentials,
        *,
        base_url: str = BASE_URL,
        min_interval: float = MIN_REQUEST_INTERVAL,
        max_attempts: int = MAX_ATTEMPTS,
        timeout: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._http = httpx.Client(
            base_url=base_url,
            headers={
                "x-api-user": credentials.user_id,
                "x-api-key": credentials.api_token.get_secret_value(),
                "x-client": f"{credentials.user_id}-{CLIENT_NAME}",
            },
            timeout=timeout,
        )
        self._min_interval = min_interval
        self._max_attempts = max_attempts
        self._sleep = sleep
        self._clock = clock
        self._last_request_at: float | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def get_user(self, fields: Sequence[str] = DEFAULT_USER_FIELDS) -> dict[str, Any]:
        """Return the authenticated user, limited to `fields` (all fields if empty)."""
        params = {"userFields": ",".join(fields)} if fields else None
        return self._get("/user", params=params)

    def get_tasks(self) -> list[dict[str, Any]]:
        """Return all active tasks (habits, dailys, todos, rewards) in a single request."""
        return self._get("/tasks/user")

    def _get(self, path: str, params: dict[str, str] | None = None) -> Any:
        for attempt in range(1, self._max_attempts + 1):
            is_last_attempt = attempt == self._max_attempts
            self._wait_for_slot()
            try:
                response = self._http.get(path, params=params)
            except httpx.TransportError as exc:
                if is_last_attempt:
                    raise HabiticaError(f"Could not reach Habitica ({path}): {exc}") from exc
                self._wait_before_retry(path, f"network error ({exc})", self._backoff(attempt))
                continue

            if response.status_code in RETRYABLE_STATUSES and not is_last_attempt:
                delay = self._retry_delay(response, attempt)
                self._wait_before_retry(path, f"HTTP {response.status_code}", delay)
                continue

            return _unwrap(path, response)

        raise AssertionError("unreachable: the last attempt always returns or raises")

    def _wait_for_slot(self) -> None:
        if self._last_request_at is not None:
            wait = self._min_interval - (self._clock() - self._last_request_at)
            if wait > 0:
                logger.info("Waiting %.0fs before the next Habitica request", wait)
                self._sleep(wait)
        self._last_request_at = self._clock()

    def _wait_before_retry(self, path: str, reason: str, delay: float) -> None:
        logger.warning("Habitica %s failed with %s; retrying in %.0fs", path, reason, delay)
        self._sleep(delay)

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after is not None:
            try:
                return min(max(float(retry_after), 0.0), MAX_RETRY_DELAY)
            except ValueError:
                pass
        return self._backoff(attempt)

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(BACKOFF_BASE**attempt, MAX_RETRY_DELAY)


def _unwrap(path: str, response: httpx.Response) -> Any:
    """Return the `data` field of a Habitica response, or raise a readable error."""
    try:
        body = response.json()
    except ValueError:
        body = None

    if response.is_success and isinstance(body, dict) and body.get("success") is True:
        return body.get("data")

    detail = ""
    if isinstance(body, dict):
        detail = ": ".join(str(body[key]) for key in ("error", "message") if body.get(key))
    message = f"Habitica {path} failed with HTTP {response.status_code}"
    if detail:
        message += f" ({detail})"
    if response.status_code == 401:
        message += ". Check HABITICA_USER_ID and HABITICA_API_TOKEN."
    raise HabiticaError(message)
