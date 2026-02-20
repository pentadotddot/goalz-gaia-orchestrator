"""
Wiki Builder – the core logic that takes a WikiCreateRequest,
creates pages in ClickUp, and tracks progress in a Job store.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from app.clickup_client import ClickUpClient
from app.config import Settings
from app.models import (
    JobStatus,
    JobStatusResponse,
    PageResult,
    WikiCreateRequest,
    WikiPage,
)

log = logging.getLogger(__name__)


# ── In-memory job store ────────────────────────────────────────────

_jobs: dict[str, JobStatusResponse] = {}


def get_job(job_id: str) -> JobStatusResponse | None:
    return _jobs.get(job_id)


def list_jobs() -> list[JobStatusResponse]:
    return list(_jobs.values())


# ── Counting helper ────────────────────────────────────────────────

def _count_pages(pages: list[WikiPage]) -> int:
    total = len(pages)
    for p in pages:
        total += _count_pages(p.children)
    return total


# ── Recursive uploader ─────────────────────────────────────────────

async def _upload_pages(
    client: ClickUpClient,
    workspace_id: str,
    doc_id: str,
    pages: list[WikiPage],
    job: JobStatusResponse,
    results: list[PageResult],
    parent_page_id: str | None = None,
    depth: int = 0,
    delay: float = 1.2,
    max_content_size: int = 90_000,
):
    """Recursively create pages and populate *results* in-place."""
    for page in pages:
        pr = PageResult(title=page.title, status="uploading")
        results.append(pr)

        try:
            log.info("%sUploading: %s", "  " * min(depth, 8), page.title)
            result = await client.create_page(
                workspace_id=workspace_id,
                doc_id=doc_id,
                title=page.title,
                content=page.content,
                parent_page_id=parent_page_id,
                max_content_size=max_content_size,
            )
            page_id = result.get("id", "")
            pr.clickup_page_id = page_id
            pr.status = "uploaded"
            job.uploaded += 1

            await asyncio.sleep(delay)

            # Recurse children
            if page.children:
                await _upload_pages(
                    client=client,
                    workspace_id=workspace_id,
                    doc_id=doc_id,
                    pages=page.children,
                    job=job,
                    results=pr.children,
                    parent_page_id=page_id,
                    depth=depth + 1,
                    delay=delay,
                    max_content_size=max_content_size,
                )

        except Exception as exc:
            log.error("  FAILED: %s – %s", page.title, exc)
            pr.status = "failed"
            pr.error = str(exc)
            job.failed += 1


# ── Main entry point (runs as background task) ─────────────────────

async def run_wiki_creation(
    request: WikiCreateRequest,
    settings: Settings,
) -> str:
    """
    Create a background job that uploads the wiki to ClickUp.
    Returns the job_id immediately.
    """
    job_id = uuid.uuid4().hex[:12]
    total = _count_pages(request.pages)

    job = JobStatusResponse(
        job_id=job_id,
        status=JobStatus.queued,
        total_pages=total,
    )
    _jobs[job_id] = job

    # Launch background task
    asyncio.create_task(_execute_job(job_id, request, settings))
    return job_id


async def _execute_job(
    job_id: str,
    request: WikiCreateRequest,
    settings: Settings,
):
    job = _jobs[job_id]
    job.status = JobStatus.running

    client = ClickUpClient(
        api_key=settings.clickup_api_key,
        base_url=settings.clickup_api_base,
        retries=settings.api_retries,
        retry_base_delay=settings.api_retry_base_delay,
    )

    try:
        # Resolve workspace ID
        workspace_id = request.target.workspace_id
        if not workspace_id:
            teams = await client.get_teams()
            if not teams:
                raise RuntimeError("No ClickUp workspaces found for this API key")
            workspace_id = str(teams[0]["id"])
            log.info("Auto-detected workspace: %s (ID %s)", teams[0].get("name"), workspace_id)
        job.workspace_id = workspace_id

        # Resolve or create the Doc
        doc_id = request.target.doc_id
        if not doc_id:
            if not request.target.space_id:
                raise ValueError(
                    "Either target.doc_id (existing doc) or target.space_id "
                    "(to create a new doc) must be provided."
                )
            doc = await client.create_doc(
                workspace_id=workspace_id,
                title=request.doc_name,
                parent={"id": request.target.space_id, "type": 4},
            )
            doc_id = doc.get("id", "")
            log.info("Created new Doc: %s", doc_id)
        job.doc_id = doc_id

        parent_page_id = request.target.parent_page_id

        # Upload pages
        log.info(
            "Starting upload of %d pages to doc %s …",
            job.total_pages, doc_id,
        )
        await _upload_pages(
            client=client,
            workspace_id=workspace_id,
            doc_id=doc_id,
            pages=request.pages,
            job=job,
            results=job.pages,
            parent_page_id=parent_page_id,
            delay=settings.upload_delay,
            max_content_size=settings.max_content_size,
        )

        job.status = JobStatus.completed if job.failed == 0 else JobStatus.failed
        log.info(
            "Job %s finished – uploaded: %d, failed: %d",
            job_id, job.uploaded, job.failed,
        )

    except Exception as exc:
        log.exception("Job %s crashed: %s", job_id, exc)
        job.status = JobStatus.failed
        job.error = str(exc)

    finally:
        await client.close()
