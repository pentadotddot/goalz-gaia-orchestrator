"""
Pydantic models for the Wiki creation API.

The SuperAgent in ClickUp sends a JSON payload describing the wiki
structure and where to place it. These models validate that payload.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ── Request models ──────────────────────────────────────────────────

class WikiPage(BaseModel):
    """A single page in the wiki tree (recursive)."""

    title: str = Field(..., min_length=1, max_length=500, description="Page title")
    content: str = Field(
        default="",
        description="Markdown / rich-text content of the page",
    )
    children: list[WikiPage] = Field(
        default_factory=list,
        description="Nested child pages",
    )


class TargetLocation(BaseModel):
    """
    Where in ClickUp the wiki should be created.

    Priority logic:
      1. If `doc_id` is provided → pages are added to the existing Doc.
         If `parent_page_id` is also provided → pages are nested under that page.
      2. If `doc_id` is NOT provided → a new Doc is created inside the Space
         identified by `space_id`.
    """

    workspace_id: Optional[str] = Field(
        default=None,
        description="ClickUp Workspace (Team) ID. Auto-detected if omitted.",
    )
    space_id: Optional[str] = Field(
        default=None,
        description="Space ID – required only when creating a brand-new Doc.",
    )
    doc_id: Optional[str] = Field(
        default=None,
        description="Existing Doc ID to add pages to.",
    )
    parent_page_id: Optional[str] = Field(
        default=None,
        description="Existing Page ID to nest new pages under.",
    )


class WikiCreateRequest(BaseModel):
    """Top-level request body sent by the ClickUp SuperAgent."""

    doc_name: str = Field(
        default="Wiki",
        min_length=1,
        max_length=500,
        description="Title for the new Doc (used only when creating a new one).",
    )
    target: TargetLocation = Field(
        ...,
        description="Where in ClickUp to place the wiki.",
    )
    pages: list[WikiPage] = Field(
        ...,
        min_length=1,
        description="The wiki tree – list of top-level pages with optional children.",
    )


# ── Response models ─────────────────────────────────────────────────

class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"


class PageResult(BaseModel):
    """Result of uploading a single page."""

    title: str
    clickup_page_id: str = ""
    status: str = "pending"
    error: str = ""
    children: list[PageResult] = Field(default_factory=list)


class WikiCreateResponse(BaseModel):
    """Immediate response acknowledging the request (async job)."""

    job_id: str
    status: JobStatus = JobStatus.queued
    message: str = "Wiki creation job queued"


class JobStatusResponse(BaseModel):
    """Full status of a wiki-creation job."""

    job_id: str
    status: JobStatus
    doc_id: str = ""
    workspace_id: str = ""
    total_pages: int = 0
    uploaded: int = 0
    failed: int = 0
    skipped: int = 0
    pages: list[PageResult] = Field(default_factory=list)
    error: str = ""
