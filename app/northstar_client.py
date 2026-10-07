"""HTTP client for the Northstar read APIs.

Reads are always live: technician availability changes during a shift, so nothing here
caches across calls. Retries are bounded with a short backoff and then give up — a failed
dependency becomes a visible failure, never a guess.

Known API behaviour (see exploration/FINDINGS.md):
- `q=` filters only on customers, agreements and work-order-history.
  On requests and work-orders it is silently ignored and the full list comes back,
  so those routes are filtered client-side by the caller.
- Successful list calls return {"results": [...]}.
- A dependency timeout returns 504.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from .config import Settings

logger = logging.getLogger("northstar.client")

# Transport-level failures worth retrying. 504 is Northstar's documented dependency timeout.
RETRYABLE_STATUS = frozenset({502, 503, 504, 429})

DEFAULT_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = 0.25


class LookupFailure(RuntimeError):
    """A read we depend on did not answer. Never swallowed into a default value."""

    def __init__(self, route: str, detail: str) -> None:
        super().__init__(f"{route}: {detail}")
        self.route = route
        self.detail = detail


class WorkOrderRejected(RuntimeError):
    """The API refused the write on its own terms (400/422). A retry will not help."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"work-orders: HTTP {status_code} {detail}")
        self.status_code = status_code
        self.detail = detail


class NorthstarClient:
    """Thin wrapper. One method per route; no business logic."""

    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
        attempts: int = DEFAULT_ATTEMPTS,
        backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
    ) -> None:
        self._settings = settings
        self._client = client
        self._owns_client = client is None
        self._attempts = max(1, attempts)
        self._backoff = backoff_seconds

    async def __aenter__(self) -> NorthstarClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._settings.northstar_timeout_seconds)
            self._owns_client = True
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get(self, route: str, q: str | None = None) -> list[dict[str, Any]]:
        """GET one route and return its `results`, retrying bounded on transport faults."""
        if self._client is None:
            raise RuntimeError("NorthstarClient used outside its context manager")

        params: dict[str, str] = {"route": route}
        if q is not None:
            params["q"] = q

        # The token goes in the header only — never a query string, never a log line.
        headers = {
            "authorization": f"Bearer {self._settings.northstar_token.reveal()}",
            "accept": "application/json",
        }

        last_detail = "no attempt made"
        for attempt in range(1, self._attempts + 1):
            try:
                response = await self._client.get(
                    self._settings.northstar_api_base,
                    params=params,
                    headers=headers,
                    timeout=self._settings.northstar_timeout_seconds,
                )
                if response.status_code in RETRYABLE_STATUS:
                    last_detail = f"HTTP {response.status_code}"
                elif response.status_code >= 400:
                    # Client errors will not improve on retry.
                    raise LookupFailure(route, f"HTTP {response.status_code}")
                else:
                    payload = response.json()
                    results = payload.get("results")
                    if not isinstance(results, list):
                        raise LookupFailure(route, "response had no results array")
                    return results
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_detail = f"{type(exc).__name__}"
            except ValueError:
                last_detail = "response was not valid JSON"

            if attempt < self._attempts:
                logger.warning("route=%s attempt %d/%d failed (%s)", route, attempt, self._attempts, last_detail)
                await asyncio.sleep(self._backoff * attempt)

        raise LookupFailure(route, f"{last_detail} after {self._attempts} attempts")

    # --- routes ---------------------------------------------------------
    # q= is passed only where the API honours it.

    async def customers(self, q: str | None = None) -> list[dict[str, Any]]:
        return await self._get("customers", q)

    async def agreements(self, q: str | None = None) -> list[dict[str, Any]]:
        return await self._get("agreements", q)

    async def technicians(self) -> list[dict[str, Any]]:
        return await self._get("technicians")

    async def work_order_history(self, q: str | None = None) -> list[dict[str, Any]]:
        return await self._get("work-order-history", q)

    async def work_orders(self) -> list[dict[str, Any]]:
        """Full list: q= is ignored on this route, so callers filter."""
        return await self._get("work-orders")

    async def requests(self) -> list[dict[str, Any]]:
        """Full list: q= is ignored on this route, so callers filter."""
        return await self._get("requests")

    # --- writes ---------------------------------------------------------

    async def create_work_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST ?route=work-orders. One attempt; the caller owns retry and reconciliation.

        A write is never retried blindly here: a timeout leaves the outcome uncertain, and
        the documented recovery is to reconcile by request or event reference first
        (SYS-CATALOG-001). Returns the {workOrder, duplicate} envelope.

        Raises LookupFailure on a timeout or 5xx so the caller can reconcile, and
        WorkOrderRejected on a 4xx, which will not improve on retry.
        """
        if self._client is None:
            raise RuntimeError("NorthstarClient used outside its context manager")

        try:
            response = await self._client.post(
                self._settings.northstar_api_base,
                timeout=self._settings.northstar_timeout_seconds * 2,
                params={"route": "work-orders"},
                headers={
                    "authorization": f"Bearer {self._settings.northstar_token.reveal()}",
                    "content-type": "application/json",
                },
                json=payload,
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise LookupFailure("work-orders", type(exc).__name__) from exc

        if response.status_code in RETRYABLE_STATUS:
            # Includes the documented 504: outcome unknown, reconcile before retrying.
            raise LookupFailure("work-orders", f"HTTP {response.status_code}")

        if response.status_code >= 400:
            detail = response.text[:300]
            raise WorkOrderRejected(response.status_code, detail)

        try:
            return response.json()
        except ValueError as exc:
            raise LookupFailure("work-orders", "response was not valid JSON") from exc
