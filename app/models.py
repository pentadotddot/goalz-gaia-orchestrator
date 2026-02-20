"""
Pydantic models for the Wiki creation API.

The SuperAgent in ClickUp sends a JSON payload describing the wiki
structure and where to place it. These models validate that payload.
"""

from __future__ import annotations

import logging
import re
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, model_validator

log = logging.getLogger(__name__)


# ── ClickUp URL parser ──────────────────────────────────────────────

# Doc URL:   https://app.clickup.com/{team_id}/v/dc/{doc_id}/{page_id}
# Space URL: https://app.clickup.com/{team_id}/v/s/{space_id}
# List URL:  https://app.clickup.com/{team_id}/v/li/{list_id}

_CLICKUP_DOC_RE = re.compile(
    r"https?://app\.clickup\.com/(?P<team_id>[^/]+)/v/dc/(?P<doc_id>[^/]+)(?:/(?P<page_id>[^/?\s]+))?"
)
_CLICKUP_SPACE_RE = re.compile(
    r"https?://app\.clickup\.com/(?P<team_id>[^/]+)/v/s/(?P<space_id>[^/?\s]+)"
)


def parse_clickup_url(url: str) -> dict:
    """
    Extract workspace_id, doc_id, page_id, or space_id from a ClickUp URL.

    Supported formats:
      - Doc page:  https://app.clickup.com/90151997238/v/dc/2kyqmktp-35355/2kyqmktp-581535
      - Doc root:  https://app.clickup.com/90151997238/v/dc/2kyqmktp-35355
      - Space:     https://app.clickup.com/90151997238/v/s/90152044016

    Returns dict with extracted fields (only non-None values).
    """
    m = _CLICKUP_DOC_RE.match(url)
    if m:
        result = {"workspace_id": m.group("team_id"), "doc_id": m.group("doc_id")}
        if m.group("page_id"):
            result["parent_page_id"] = m.group("page_id")
        log.info("Parsed Doc URL → %s", result)
        return result

    m = _CLICKUP_SPACE_RE.match(url)
    if m:
        result = {"workspace_id": m.group("team_id"), "space_id": m.group("space_id")}
        log.info("Parsed Space URL → %s", result)
        return result

    return {}


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

    You can provide EITHER:
      - A ClickUp **url** (easiest – IDs are extracted automatically), OR
      - Individual IDs (workspace_id, doc_id, space_id, parent_page_id).

    URL examples:
      - Doc page:  https://app.clickup.com/90151997238/v/dc/2kyqmktp-35355/2kyqmktp-581535
        → adds pages under that specific page in that doc
      - Doc root:  https://app.clickup.com/90151997238/v/dc/2kyqmktp-35355
        → adds pages at the top level of that doc
      - Space:     https://app.clickup.com/90151997238/v/s/90152044016
        → creates a brand-new doc in that space

    Priority logic (after URL parsing):
      1. If `doc_id` is present → pages are added to the existing Doc.
         If `parent_page_id` is also present → pages are nested under that page.
      2. If `doc_id` is absent → a new Doc is created in `space_id`.
    """

    url: Optional[str] = Field(
        default=None,
        description=(
            "A ClickUp URL pointing to a Doc, page, or Space. "
            "IDs are extracted automatically. This is the easiest option."
        ),
    )
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

    @model_validator(mode="after")
    def _resolve_url(self) -> "TargetLocation":
        """If a url was provided, parse it and fill in any missing ID fields."""
        if self.url:
            parsed = parse_clickup_url(self.url)
            if not parsed:
                log.warning("Could not parse ClickUp URL: %s", self.url)
            else:
                # Only fill in fields that weren't explicitly provided
                if not self.workspace_id and "workspace_id" in parsed:
                    self.workspace_id = parsed["workspace_id"]
                if not self.doc_id and "doc_id" in parsed:
                    self.doc_id = parsed["doc_id"]
                if not self.parent_page_id and "parent_page_id" in parsed:
                    self.parent_page_id = parsed["parent_page_id"]
                if not self.space_id and "space_id" in parsed:
                    self.space_id = parsed["space_id"]
        return self


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
