"""
FastAPI routes for the Gaia Orchestrator wiki-creation service.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request as FastAPIRequest, status
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
from app.clickup_client import ClickUpClient
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
            content=p.get("content", "") or p.get("summary", ""),
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
    raw_request: FastAPIRequest,
    settings: Settings = Depends(get_settings),
):
    """
    Receives a webhook POST from a ClickUp Automation.

    The SuperAgent creates a task with the wiki JSON payload in the
    task description. The automation fires this webhook, and we
    extract the JSON from the task description field.

    Also handles ClickUp's "Test webhook" button gracefully.
    """
    # Parse body (accept any format)
    body_bytes = await raw_request.body()
    body_text = body_bytes.decode("utf-8", errors="replace")
    log.info("Webhook received (%d bytes): %.500s", len(body_bytes), body_text)

    # Try to parse as JSON
    request_data: dict = {}
    try:
        parsed = json.loads(body_text) if body_text.strip() else {}
        if isinstance(parsed, dict):
            request_data = parsed
        elif isinstance(parsed, list) and len(parsed) > 0 and isinstance(parsed[0], dict):
            request_data = parsed[0]
    except (json.JSONDecodeError, ValueError):
        log.info("Webhook: body is not JSON, treating as raw text")

    if not settings.clickup_api_key:
        raise HTTPException(status_code=500, detail="CLICKUP_API_KEY not configured")

    # ── Test webhook detection ──
    # ClickUp test webhooks send sample/placeholder data.
    # Accept them with 200 so the test passes.
    is_test = (
        not body_text.strip()
        or body_text.strip() in ("{}", "[]", "null", "test")
        or request_data.get("event") == "test"
    )

    if is_test and "pages" not in request_data:
        log.info("Webhook: test/empty payload detected – returning 200 OK")
        return {
            "status": "ok",
            "message": "Webhook received successfully. Send a task with wiki JSON in the description to create pages.",
        }

    # ── Find the wiki payload ──

    wiki_payload = None

    # Priority 1: body IS the wiki payload (direct Swagger / manual test)
    wiki_payload = _find_wiki_payload(request_data)
    if wiki_payload:
        log.info("Webhook: found wiki payload directly in request body")

    # Priority 2: "payload" query parameter
    if not wiki_payload:
        payload_param = raw_request.query_params.get("payload", "")
        if payload_param:
            log.info("Webhook: found 'payload' query param (%d chars)", len(payload_param))
            wiki_payload = _try_parse_wiki_json(payload_param)
            if wiki_payload:
                log.info("Webhook: extracted wiki JSON from 'payload' query param")

    # Priority 3: scan the raw body text
    if not wiki_payload:
        wiki_payload = _find_wiki_payload_in_text(body_text)
        if wiki_payload:
            log.info("Webhook: found wiki JSON in raw body text")

    # Priority 4: extract task_id from the webhook body and fetch description from ClickUp API
    if not wiki_payload:
        task_id = _extract_task_id(request_data)
        if task_id:
            log.info("Webhook: found task_id=%s, fetching description from ClickUp API…", task_id)
            wiki_payload = await _fetch_wiki_payload_from_task(task_id, settings)
            if wiki_payload:
                log.info("Webhook: extracted wiki JSON from task description (via API)")
            else:
                log.warning("Webhook: task %s description did not contain valid wiki JSON", task_id)
        else:
            log.info("Webhook: no task_id found in webhook body")

    if not wiki_payload:
        log.warning("Webhook: no wiki payload found – returning guidance")
        return {
            "status": "ignored",
            "message": (
                "No wiki JSON payload found. Ensure the task description "
                "contains JSON like: "
                '{"doc_name": "...", "target": {"url": "..."}, "pages": [...]}'
            ),
        }

    # ── Build request and start job ──
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
        return {"status": "error", "message": f"Invalid wiki payload: {e}"}

    job_id = await run_wiki_creation(wiki_request, settings)
    total = _count_pages(pages)
    log.info("Webhook: job %s started (%d pages)", job_id, total)

    return {
        "status": "accepted",
        "job_id": job_id,
        "total_pages": total,
        "message": "Wiki creation started",
    }


def _is_valid_task_id(val) -> bool:
    """Check if a value looks like a real ClickUp task ID (alphanumeric, 5+ chars)."""
    if not val:
        return False
    s = str(val).strip()
    # Reject empty, template placeholders like "{}", "{{Task ID}}", UUIDs
    if not s or s in ("{}", "null", "undefined") or s.startswith("{{"):
        return False
    if len(s) < 5:
        return False
    # ClickUp task IDs are typically 9-char alphanumeric (e.g. "86c8ce274")
    return True


def _extract_task_id(data: dict) -> str | None:
    """
    Try to extract a ClickUp task ID from the webhook body.

    ClickUp Automation webhooks send the task data in:
      - data["payload"]["id"]   ← most common (ClickUp Automation format)
      - data["task_id"]
      - data["task"]["id"]
      - data["history_items"][0]["after"]["id"]
    """
    # Priority 1: payload.id (ClickUp Automation webhook format)
    payload_obj = data.get("payload")
    if isinstance(payload_obj, dict):
        tid = payload_obj.get("id")
        if _is_valid_task_id(tid):
            return str(tid)

    # Priority 2: direct task_id / taskId field
    for key in ("task_id", "taskId"):
        val = data.get(key)
        if _is_valid_task_id(val):
            return str(val)

    # Priority 3: task.id
    task_obj = data.get("task")
    if isinstance(task_obj, dict):
        tid = task_obj.get("id")
        if _is_valid_task_id(tid):
            return str(tid)

    # Priority 4: history_items[0].after.id (ClickUp webhook v2 format)
    history = data.get("history_items")
    if isinstance(history, list) and history:
        after = history[0].get("after") if isinstance(history[0], dict) else None
        if isinstance(after, dict) and _is_valid_task_id(after.get("id")):
            return str(after["id"])

    return None


async def _fetch_wiki_payload_from_task(task_id: str, settings) -> dict | None:
    """Fetch a task from ClickUp by ID and try to parse wiki JSON from its description."""
    client = ClickUpClient(
        api_key=settings.clickup_api_key,
        base_url=settings.clickup_api_base,
    )
    try:
        task = await client.get_task(task_id)
        task_name = task.get("name", "?")

        # Try each text field — text_content is plain text (most reliable),
        # then description, then markdown_description
        for field_name in ("text_content", "description", "markdown_description"):
            text = task.get(field_name, "") or ""
            if not text:
                continue
            log.info(
                "Webhook: task '%s' field '%s' (%d chars): %.300s",
                task_name, field_name, len(text), text,
            )
            result = _try_parse_wiki_json(text)
            if result:
                log.info("Webhook: parsed wiki JSON from task field '%s'", field_name)
                return result

        log.warning("Webhook: no wiki JSON found in any task field for '%s'", task_name)
        return None
    except Exception as exc:
        log.error("Webhook: failed to fetch task %s: %s", task_id, exc)
        return None
    finally:
        await client.close()


def _find_wiki_payload(data: dict) -> dict | None:
    """Try to find a wiki payload in a dict (direct or nested in fields)."""
    if not data:
        return None

    # Strategy 1: body IS the payload
    if "pages" in data and ("target" in data or "doc_name" in data):
        return data

    # Strategy 2: task description fields
    for field in [
        "task_description", "description", "Task Description",
        "task_content", "content", "body", "text",
    ]:
        value = data.get(field, "")
        if isinstance(value, str) and value:
            result = _try_parse_wiki_json(value)
            if result:
                return result

    # Strategy 3: scan all string values
    for key, value in data.items():
        if isinstance(value, str) and len(value) > 20:
            result = _try_parse_wiki_json(value)
            if result:
                return result
        elif isinstance(value, dict):
            result = _find_wiki_payload(value)
            if result:
                return result

    return None


def _find_wiki_payload_in_text(text: str) -> dict | None:
    """Try to find wiki JSON anywhere in raw text."""
    if not text or len(text) < 10:
        return None
    return _try_parse_wiki_json(text)


def _clean_rich_text(text: str) -> str:
    """
    Clean up rich-text artifacts that ClickUp might inject into
    task descriptions before we try to parse JSON from them.
    """
    # Strip HTML tags (e.g. <a href="...">url</a> → url)
    text = re.sub(r"<[^>]+>", "", text)
    # Unescape HTML entities (&quot; → ", &amp; → &, etc.)
    text = html.unescape(text)
    # Replace unicode smart quotes with ASCII equivalents
    text = text.replace("\u201c", '"').replace("\u201d", '"')  # " "
    text = text.replace("\u201e", '"')                          # „ (Hungarian)
    text = text.replace("\u2018", "'").replace("\u2019", "'")  # ' '
    # Normalize whitespace (non-breaking spaces, zero-width chars)
    text = text.replace("\u00a0", " ").replace("\u200b", "")
    # Strip leading/trailing whitespace
    text = text.strip()
    return text


def _repair_json(text: str, max_fixes: int = 5000) -> str:
    """
    Iteratively repair invalid JSON by fixing errors at the exact position
    reported by Python's ``json.loads()``.

    This is far more robust than the old approach of tracking string
    boundaries character-by-character, because ClickUp may alter escape
    sequences (``\\n`` → real newline, ``\\"`` → ``"``, ``\\t`` → tab, etc.)
    and the manual tracker can desync.  Here the JSON decoder itself tells
    us **what** is wrong and **where**, and we patch it.

    Handled repairs:
      - *Invalid control characters* inside strings (newlines, carriage
        returns, tabs, and other control chars).
      - *Invalid ``\\escape``* sequences (e.g. ``\\P`` → ``\\\\P``).
      - *Trailing commas* before ``}`` or ``]``.
    """
    # Pre-pass: remove trailing commas (common with LLM-generated JSON)
    text = re.sub(r",(\s*[}\]])", r"\1", text)

    for _ in range(max_fixes):
        try:
            json.loads(text)
            return text
        except json.JSONDecodeError as e:
            pos = e.pos
            if pos is None or pos < 0 or pos > len(text):
                break

            if "Invalid control character" in e.msg and pos < len(text):
                ch = text[pos]
                repl = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}.get(ch, "")
                text = text[:pos] + repl + text[pos + 1:]

            elif "Invalid \\escape" in e.msg and pos < len(text):
                # pos points to the char after the backslash; insert an
                # extra backslash so \X becomes \\X (literal backslash).
                text = text[:pos] + "\\" + text[pos:]

            else:
                # Structural error we cannot auto-fix — log and bail out
                snippet = text[max(0, pos - 30) : pos + 40]
                log.warning(
                    "JSON repair: unfixable at pos %d: %s – …%s…",
                    pos,
                    e.msg,
                    snippet.replace("\n", "↵"),
                )
                break

    return text


def _normalize_pages(pages: list) -> list:
    """Recursively normalise page dicts coming from the agent."""
    out = []
    for p in pages:
        if not isinstance(p, dict):
            continue
        # Map "summary" → "content" if content is missing
        if "content" not in p and "summary" in p:
            p["content"] = p.pop("summary")
        p["children"] = _normalize_pages(p.get("children", []))
        out.append(p)
    return out


def _normalize_wiki_payload(data: dict) -> dict:
    """
    Remap non-standard field names the ClickUp agent might use so they
    match the expected WikiCreateRequest schema.

    Handled mappings:
      - ``target_url`` (str)  →  ``target: {"url": ...}``
      - ``summary`` → ``content``  in every page (recursive)
    """
    # target_url → target
    if "target_url" in data and "target" not in data:
        data["target"] = {"url": data.pop("target_url")}

    # Ensure target is a dict (agent might pass a bare URL string)
    if isinstance(data.get("target"), str):
        data["target"] = {"url": data["target"]}

    # Normalise pages
    if "pages" in data and isinstance(data["pages"], list):
        data["pages"] = _normalize_pages(data["pages"])

    return data


def _try_parse_wiki_json(text: str) -> dict | None:
    """Try to extract a valid wiki JSON payload from a string.

    Strategy order:
      1. Extract from fenced code blocks (```...```) — most reliable when
         the agent wraps its JSON in a code block.
      2. Direct ``json.loads`` on the whole text.
      3. ``find("{")`` / ``rfind("}")`` substring extraction.
    Each strategy is tried on both the raw text and a cleaned version.
    """

    # ── Strategy 0: fenced code blocks ──
    # Match ```json ... ``` or ``` ... ``` (with optional language tag)
    code_blocks = re.findall(r"```(?:\w*\n|\n)?(.*?)```", text, re.DOTALL)
    for block in code_blocks:
        block = block.strip()
        if not block:
            continue
        # Try raw, then repaired (ClickUp may render \n inside strings)
        for candidate in [block, _repair_json(block)]:
            try:
                data = json.loads(candidate)
                if isinstance(data, dict) and "pages" in data:
                    log.debug("Parsed wiki JSON from fenced code block")
                    return _normalize_wiki_payload(data)
            except (json.JSONDecodeError, ValueError):
                pass

    # ── Strategies 1+: direct parse / substring extraction / repair ──
    #
    # We try the raw text first, then the rich-text-cleaned version.
    # For each, we try: direct parse → substring extraction → repair.
    last_error: json.JSONDecodeError | None = None
    last_candidate: str = ""

    for label, attempt_text in [
        ("raw", text.strip()),
        ("cleaned", _clean_rich_text(text)),
    ]:
        if not attempt_text:
            continue

        # Try direct parse
        try:
            data = json.loads(attempt_text)
            if isinstance(data, dict) and "pages" in data:
                return _normalize_wiki_payload(data)
        except (json.JSONDecodeError, ValueError):
            pass

        # Try to find JSON object in the text
        start = attempt_text.find("{")
        end = attempt_text.rfind("}")
        if start == -1 or end <= start:
            continue

        candidate = attempt_text[start:end + 1]

        # Try raw candidate
        try:
            data = json.loads(candidate)
            if isinstance(data, dict) and "pages" in data:
                return _normalize_wiki_payload(data)
        except (json.JSONDecodeError, ValueError):
            pass

        # Try iterative repair (handles control chars + invalid escapes)
        try:
            repaired = _repair_json(candidate)
            data = json.loads(repaired)
            if isinstance(data, dict) and "pages" in data:
                log.info("Parsed wiki JSON after iterative repair (%s)", label)
                return _normalize_wiki_payload(data)
        except (json.JSONDecodeError, ValueError) as exc:
            last_error = exc if isinstance(exc, json.JSONDecodeError) else None
            last_candidate = repaired

    # ── Nothing worked — log details so we can diagnose remotely ──
    if last_error and last_candidate:
        pos = last_error.pos or 0
        snippet = last_candidate[max(0, pos - 40) : pos + 60].replace("\n", "↵")
        log.warning(
            "JSON parse failed after all strategies: %s at pos %d – …%s…",
            last_error.msg,
            pos,
            snippet,
        )
        log.warning(
            "Candidate length: %d chars, first 200: %.200s",
            len(last_candidate),
            last_candidate.replace("\n", "↵"),
        )

    return None
