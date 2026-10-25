"""The shared HTTP client, and backoff for retries."""

import httpx

from aaata_gtfs.config import ERROR_BACKOFF_S, MAX_ERROR_BACKOFF_S, REQUEST_HEADERS, REQUEST_TIMEOUT_S


def make_client(transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    """The one `AsyncClient` the collector shares. Every request carries `REQUEST_HEADERS`.

    `transport` is for tests (an `httpx.MockTransport`).
    """
    return httpx.AsyncClient(
        headers=REQUEST_HEADERS,
        timeout=REQUEST_TIMEOUT_S,
        follow_redirects=True,
        transport=transport,
    )


async def get(client: httpx.AsyncClient, url: str, headers: dict[str, str] | None = None) -> httpx.Response:
    """GET `url`, raising `httpx.HTTPStatusError` for a 4xx/5xx response (but not 304).

    `REQUEST_HEADERS` are sent per request too, so a caller's own client identifies us.
    """
    response = await client.get(url, headers={**REQUEST_HEADERS, **(headers or {})})
    if response.status_code != 304:
        response.raise_for_status()
    return response


async def get_bytes(client: httpx.AsyncClient, url: str) -> bytes:
    """The body of a GET of `url`."""
    return (await get(client, url)).content


def backoff_delay(consecutive_errors: int) -> float:
    """Wait after the n-th consecutive error: 5, 10, 20, ... s, capped at 120 s."""
    if consecutive_errors <= 0:
        return 0.0
    return min(ERROR_BACKOFF_S * 2 ** (consecutive_errors - 1), MAX_ERROR_BACKOFF_S)
