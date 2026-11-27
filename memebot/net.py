"""HTTP with retries. Every network call in the bot goes through request_json().

Retries: connection resets, remote disconnects, timeouts, HTTP 429 and 5xx, with exponential
backoff plus jitter, honoring Retry-After (capped). Other 4xx errors are returned immediately.
bot.py crashed on ConnectionResetError/RemoteDisconnected; here nothing network-related escapes
as an unhandled exception: callers get FetchError.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Any

import httpx

log = logging.getLogger(__name__)

UA = "memebot/1.0 (+paper-first Solana bot)"


class FetchError(Exception):
    def __init__(self, msg: str, status: int | None = None, body: Any = None):
        super().__init__(msg)
        self.status = status
        self.body = body


def make_client(timeout: float = 15.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(timeout), headers={"User-Agent": UA},
                             follow_redirects=True, http2=False)


def _retry_after(resp: httpx.Response) -> float | None:
    v = resp.headers.get("Retry-After")
    if not v:
        return None
    try:
        return max(0.0, float(v))
    except ValueError:
        return None


async def request_json(client: httpx.AsyncClient, method: str, url: str, *, label: str,
                       params: dict | None = None, json_body: Any = None, headers: dict | None = None,
                       retries: int = 3, base_delay: float = 1.0, max_delay: float = 30.0,
                       sleep=asyncio.sleep) -> Any:
    attempt = 0
    while True:
        attempt += 1
        try:
            resp = await client.request(method, url, params=params, json=json_body, headers=headers)
        except (httpx.TransportError, OSError) as e:  # resets, disconnects, timeouts, DNS
            if attempt > retries:
                raise FetchError(f"{label}: network error after {attempt} tries ({type(e).__name__})") from None
            delay = min(max_delay, base_delay * 2 ** (attempt - 1)) * (0.75 + random.random() / 2)
            log.info("%s: %s, retry %d in %.1fs", label, type(e).__name__, attempt, delay)
            await sleep(delay)
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt > retries:
                raise FetchError(f"{label}: HTTP {resp.status_code} after {attempt} tries", resp.status_code)
            ra = _retry_after(resp)
            delay = min(max_delay, ra if ra is not None else base_delay * 2 ** (attempt - 1))
            log.info("%s: HTTP %d, retry %d in %.1fs", label, resp.status_code, attempt, delay)
            await sleep(delay)
            continue
        body: Any
        try:
            body = resp.json()
        except (json.JSONDecodeError, ValueError):
            body = None
        if resp.status_code >= 400:
            raise FetchError(f"{label}: HTTP {resp.status_code}", resp.status_code, body)
        if body is None:
            raise FetchError(f"{label}: response was not JSON", resp.status_code)
        return body
