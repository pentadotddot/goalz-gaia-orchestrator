"""
ClickUp API client (v2 + v3).

Handles authentication, retries with exponential back-off,
and the specific endpoints needed for Doc / Wiki creation.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)


class ClickUpClient:
    """Async HTTP client for ClickUp REST APIs."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.clickup.com",
        retries: int = 5,
        retry_base_delay: float = 3.0,
        timeout: float = 60.0,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.retries = retries
        self.retry_base_delay = retry_base_delay
        self._client = httpx.AsyncClient(
            headers={
                "Authorization": api_key,
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )

    async def close(self):
        await self._client.aclose()

    # ── Low-level helpers ──────────────────────────────────────────

    async def _get(self, url: str, params: dict | None = None) -> dict:
        resp = await self._client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()

    async def _post(self, url: str, data: dict) -> dict:
        last_exc: Exception | None = None
        for attempt in range(self.retries):
            try:
                resp = await self._client.post(url, json=data)
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                delay = self.retry_base_delay * (2 ** attempt)
                log.warning(
                    "Connection error (attempt %d/%d), retrying in %.0fs: %s",
                    attempt + 1, self.retries, delay, type(exc).__name__,
                )
                last_exc = exc
                await _async_sleep(delay)
                continue

            if resp.status_code in (200, 201):
                return resp.json()

            if resp.status_code in (429, 500, 502, 503):
                delay = self.retry_base_delay * (2 ** attempt)
                log.warning(
                    "POST %s -> %d (attempt %d/%d), retrying in %.0fs …",
                    url, resp.status_code, attempt + 1, self.retries, delay,
                )
                last_exc = httpx.HTTPStatusError(
                    f"{resp.status_code}", request=resp.request, response=resp,
                )
                await _async_sleep(delay)
                continue

            # Non-retryable error
            resp.raise_for_status()

        # Final attempt
        resp = await self._client.post(url, json=data)
        if resp.status_code not in (200, 201):
            log.error("POST %s -> %d: %s", url, resp.status_code, resp.text[:500])
            resp.raise_for_status()
        return resp.json()

    # ── Discovery (v2) ─────────────────────────────────────────────

    async def get_teams(self) -> list[dict]:
        data = await self._get(f"{self.base_url}/api/v2/team")
        return data.get("teams", [])

    async def get_spaces(self, team_id: str) -> list[dict]:
        data = await self._get(f"{self.base_url}/api/v2/team/{team_id}/space")
        return data.get("spaces", [])

    async def get_user(self) -> dict:
        data = await self._get(f"{self.base_url}/api/v2/user")
        return data.get("user", {})

    async def get_task(self, task_id: str) -> dict:
        """Fetch a single task by ID (v2). Returns the full task dict."""
        return await self._get(f"{self.base_url}/api/v2/task/{task_id}")

    # ── Docs API (v3) ──────────────────────────────────────────────

    async def create_doc(
        self,
        workspace_id: str,
        title: str,
        parent: dict | None = None,
    ) -> dict:
        payload: dict[str, Any] = {"name": title}
        if parent:
            payload["parent"] = parent
        log.info("Creating Doc '%s' in workspace %s …", title, workspace_id)
        data = await self._post(
            f"{self.base_url}/api/v3/workspaces/{workspace_id}/docs",
            payload,
        )
        log.info("  Doc created: ID=%s", data.get("id", ""))
        return data

    async def create_page(
        self,
        workspace_id: str,
        doc_id: str,
        title: str,
        content: str,
        parent_page_id: str | None = None,
        *,
        max_content_size: int = 90_000,
    ) -> dict:
        # Truncate oversized content
        if len(content) > max_content_size:
            content = (
                content[:max_content_size]
                + "\n\n---\n*Content truncated (original too large for ClickUp)*\n"
            )

        payload: dict[str, Any] = {"name": title, "content": content}
        if parent_page_id:
            payload["parent_page_id"] = parent_page_id

        data = await self._post(
            f"{self.base_url}/api/v3/workspaces/{workspace_id}/docs/{doc_id}/pages",
            payload,
        )
        return data

    async def get_doc(self, workspace_id: str, doc_id: str) -> dict:
        return await self._get(
            f"{self.base_url}/api/v3/workspaces/{workspace_id}/docs/{doc_id}"
        )


# ── Utility ────────────────────────────────────────────────────────

async def _async_sleep(seconds: float):
    """asyncio-friendly sleep."""
    import asyncio
    await asyncio.sleep(seconds)
