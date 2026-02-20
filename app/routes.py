"""
FastAPI routes for the Gaia Orchestrator wiki-creation service.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status

from app.config import Settings, get_settings
from app.models import (
    JobStatusResponse,
    WikiCreateRequest,
    WikiCreateResponse,
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
