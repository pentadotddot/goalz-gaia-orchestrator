"""
FastAPI routes for the Gaia Orchestrator wiki-creation service.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import PlainTextResponse

from app.config import Settings, get_settings
from app.models import (
    JobStatusResponse,
    TargetLocation,
    WikiCreateRequest,
    WikiCreateResponse,
    WikiPage,
    JobStatus,
)
from app.wiki_builder import get_job, list_jobs, run_wiki_creation

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["wiki"])


# ── Auth dependency ────────────────────────────────────────────────

async def verify_api_secret(
    x_api_secret: Annotated[str | None, Header()] = None,
    settings: Settings = Depends(get_settings),
):
    """
    If `API_SECRET` is configured, every request must include a matching
    `X-Api-Secret` header.  When the secret is blank, auth is disabled
    (useful for local development).
    """
    if settings.api_secret and settings.api_secret != x_api_secret:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-Api-Secret header",
        )


# ── Endpoints ──────────────────────────────────────────────────────

@router.post(
    "/wiki",
    response_model=WikiCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Create a structured wiki in ClickUp",
    dependencies=[Depends(verify_api_secret)],
)
async def create_wiki(
    request: WikiCreateRequest,
    settings: Settings = Depends(get_settings),
):
    """
    Accepts a JSON payload describing a wiki structure and starts an
    asynchronous job that creates the corresponding Doc + pages in ClickUp.

    Returns immediately with a `job_id` that can be polled via
    `GET /api/v1/wiki/{job_id}`.
    """
    if not settings.clickup_api_key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="CLICKUP_API_KEY is not configured on the server",
        )

    log.info(
        "Received wiki creation request: '%s' (%d top-level pages)",
        request.doc_name,
        len(request.pages),
    )

    job_id = await run_wiki_creation(request, settings)
    return WikiCreateResponse(job_id=job_id, status=JobStatus.queued)


@router.get(
    "/wiki/{job_id}",
    response_model=JobStatusResponse,
    summary="Get wiki-creation job status",
    dependencies=[Depends(verify_api_secret)],
)
async def get_wiki_job(job_id: str):
    """Return the current status of a wiki-creation job."""
    job = get_job(job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job '{job_id}' not found",
        )
    return job


@router.get(
    "/wiki",
    response_model=list[JobStatusResponse],
    summary="List all wiki-creation jobs",
    dependencies=[Depends(verify_api_secret)],
)
async def list_wiki_jobs():
    """Return a list of all wiki-creation jobs (most recent first)."""
    return list(reversed(list_jobs()))


@router.get(
    "/health",
    summary="Health check",
    tags=["system"],
)
async def health():
    return {"status": "ok"}


# ── GET-based wiki creation (for ClickUp "Load web pages" tool) ──


def _build_pages(raw: list[dict]) -> list[WikiPage]:
    """Recursively convert raw dicts into WikiPage models."""
    out: list[WikiPage] = []
    for p in raw:
        out.append(WikiPage(
            title=p.get("title", "Untitled"),
            content=p.get("content", ""),
            children=_build_pages(p.get("children", [])),
        ))
    return out


def _count_pages(pages: list[WikiPage]) -> int:
    total = len(pages)
    for p in pages:
        total += _count_pages(p.children)
    return total


def _format_tree(pages, indent=0) -> str:
    lines = []
    for p in pages:
        status = p.status if hasattr(p, "status") else ""
        pid = p.clickup_page_id if hasattr(p, "clickup_page_id") else ""
        prefix = "  " * indent
        extra = f" (ID: {pid})" if pid else ""
        stat = f" [{status}]" if status else ""
        lines.append(f"{prefix}- {p.title}{extra}{stat}")
        children = p.children if hasattr(p, "children") else []
        if children:
            lines.append(_format_tree(children, indent + 1))
    return "\n".join(lines)


@router.get(
    "/wiki/create",
    response_class=PlainTextResponse,
    summary="Create wiki via GET (for ClickUp agent Load web pages tool)",
    tags=["agent"],
)
async def create_wiki_get(
    url: str = Query(..., description="ClickUp Doc/Page/Space URL"),
    pages: str = Query(..., description="JSON array of pages: [{title, content, children}]"),
    doc_name: str = Query(default="Wiki", description="Doc name (for new docs)"),
    settings: Settings = Depends(get_settings),
):
    """
    Create a wiki via GET request. Designed for ClickUp SuperAgent's
    "Load web pages" tool.

    The agent constructs a URL with `url` and `pages` (JSON) as query
    parameters, then "loads" it. This endpoint triggers the wiki creation
    and returns a plain-text result the agent can read.

    Example:
        /api/v1/wiki/create?url=https://app.clickup.com/...&doc_name=My+Wiki&pages=[{"title":"Hello","content":"# Hello","children":[]}]
    """
    if not settings.clickup_api_key:
        return PlainTextResponse(
            "ERROR: CLICKUP_API_KEY is not configured on the server.",
            status_code=500,
        )

    # Parse pages JSON
    try:
        raw_pages = json.loads(pages)
        if not isinstance(raw_pages, list) or len(raw_pages) == 0:
            raise ValueError("pages must be a non-empty JSON array")
        wiki_pages = _build_pages(raw_pages)
    except (json.JSONDecodeError, ValueError) as e:
        return PlainTextResponse(f"ERROR: Invalid pages parameter: {e}", status_code=400)

    total = _count_pages(wiki_pages)

    # Build request
    request = WikiCreateRequest(
        doc_name=doc_name,
        target=TargetLocation(url=url),
        pages=wiki_pages,
    )

    # Start job
    job_id = await run_wiki_creation(request, settings)
    log.info("GET wiki/create: job %s started (%d pages)", job_id, total)

    # Wait for completion (up to ~3 min)
    for _ in range(90):
        await asyncio.sleep(2)
        job = get_job(job_id)
        if job and job.status.value in ("completed", "failed"):
            break

    job = get_job(job_id)
    if not job:
        return PlainTextResponse(f"ERROR: Job {job_id} not found.", status_code=500)

    # Build human-readable result
    lines = [
        f"WIKI CREATION {'COMPLETED' if job.status.value == 'completed' else 'FAILED'}",
        f"Job ID:    {job.job_id}",
        f"Status:    {job.status.value}",
        f"Doc ID:    {job.doc_id}",
        f"Uploaded:  {job.uploaded}",
        f"Failed:    {job.failed}",
        "",
        "Pages created:",
        _format_tree(job.pages),
    ]
    if job.error:
        lines.append(f"\nError: {job.error}")

    return PlainTextResponse("\n".join(lines))


# ── Webhook endpoint (for ClickUp Automations) ──────────────────


@router.post(
    "/webhook/clickup",
    summary="Receive webhook from ClickUp Automation",
    tags=["webhook"],
)
async def webhook_clickup(
    request: dict,
    settings: Settings = Depends(get_settings),
):
    """
    Receives a webhook POST from a ClickUp Automation.

    The SuperAgent creates a task with the wiki JSON payload in the
    task description. The automation fires this webhook, and we
    extract the JSON from the task description field.

    Tries multiple strategies to find the wiki payload:
      1. Direct WikiCreateRequest format (body IS the payload)
      2. Task description field containing JSON
      3. Any string field containing valid wiki JSON
    """
    if not settings.clickup_api_key:
        raise HTTPException(status_code=500, detail="CLICKUP_API_KEY not configured")

    log.info("Webhook received: %s", json.dumps(request, default=str)[:500])

    wiki_payload = None

    # Strategy 1: body is already our WikiCreateRequest format
    if "pages" in request and "target" in request:
        wiki_payload = request
        log.info("Webhook: payload is direct WikiCreateRequest format")

    # Strategy 2: look for task description containing JSON
    if not wiki_payload:
        description = (
            request.get("task_description")
            or request.get("description")
            or request.get("Task Description")
            or ""
        )
        if description:
            wiki_payload = _try_parse_wiki_json(description)
            if wiki_payload:
                log.info("Webhook: extracted payload from task description")

    # Strategy 3: scan all string values for valid wiki JSON
    if not wiki_payload:
        for key, value in request.items():
            if isinstance(value, str) and len(value) > 20:
                wiki_payload = _try_parse_wiki_json(value)
                if wiki_payload:
                    log.info("Webhook: extracted payload from field '%s'", key)
                    break

    if not wiki_payload:
        log.error("Webhook: could not find wiki payload in request")
        raise HTTPException(
            status_code=400,
            detail=(
                "Could not find wiki JSON payload in the webhook data. "
                "Make sure the task description contains the full wiki JSON: "
                '{"doc_name": "...", "target": {"url": "..."}, "pages": [...]}'
            ),
        )

    # Build request and start job
    try:
        pages = _build_pages(wiki_payload.get("pages", []))
        target_data = wiki_payload.get("target", {})
        if isinstance(target_data, str):
            target_data = {"url": target_data}

        wiki_request = WikiCreateRequest(
            doc_name=wiki_payload.get("doc_name", "Wiki"),
            target=TargetLocation(**target_data),
            pages=pages,
        )
    except Exception as e:
        log.error("Webhook: invalid payload structure: %s", e)
        raise HTTPException(status_code=400, detail=f"Invalid wiki payload: {e}")

    job_id = await run_wiki_creation(wiki_request, settings)
    log.info("Webhook: job %s started (%d pages)", job_id, _count_pages(pages))

    return {
        "status": "accepted",
        "job_id": job_id,
        "total_pages": _count_pages(pages),
        "message": "Wiki creation started",
    }


def _try_parse_wiki_json(text: str) -> dict | None:
    """Try to extract a valid wiki JSON payload from a string."""
    text = text.strip()

    # Try direct parse
    try:
        data = json.loads(text)
        if isinstance(data, dict) and "pages" in data:
            return data
    except (json.JSONDecodeError, ValueError):
        pass

    # Try to find JSON object in the text (agent might add extra text around it)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(text[start:end + 1])
            if isinstance(data, dict) and "pages" in data:
                return data
        except (json.JSONDecodeError, ValueError):
            pass

    return None
