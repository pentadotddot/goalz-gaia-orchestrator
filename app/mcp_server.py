"""
MCP (Model Context Protocol) server for Gaia Orchestrator.

Exposes wiki-creation tools over JSON-RPC 2.0 / SSE transport so that
ClickUp AI Agents can call them directly.

Endpoints (mounted at /mcp):
  GET  /mcp/sse        – SSE connection for the MCP client
  POST /mcp/messages/  – JSON-RPC messages
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from mcp.server import Server
from mcp.server.sse import SseServerTransport
import mcp.types as types
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import get_settings
from app.models import TargetLocation, WikiCreateRequest, WikiPage
from app.oauth import validate_bearer_token
from app.wiki_builder import get_job, run_wiki_creation

log = logging.getLogger(__name__)

# ── MCP Server ───────────────────────────────────────────────────

server = Server("gaia-orchestrator")

# -- Tool schemas --

_CREATE_WIKI_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "url": {
            "type": "string",
            "description": (
                "ClickUp URL where the wiki should be created. "
                "Doc-page URL → pages nested under that page; "
                "Doc-root URL → pages at top level; "
                "Space URL → brand-new Doc created. "
                "Example: https://app.clickup.com/90151997238/v/dc/2kyqmktp-35355/2kyqmktp-581535"
            ),
        },
        "doc_name": {
            "type": "string",
            "description": "Title for a new Doc (only used when target is a Space URL).",
            "default": "Wiki",
        },
        "pages": {
            "type": "array",
            "description": "Tree of pages. Each item has 'title' (str), 'content' (markdown str), and optional 'children' (same structure).",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "content": {"type": "string"},
                    "children": {"type": "array", "items": {"type": "object"}},
                },
                "required": ["title", "content"],
            },
        },
    },
    "required": ["url", "pages"],
}

_CHECK_STATUS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "job_id": {"type": "string", "description": "Job ID returned by create_wiki."},
    },
    "required": ["job_id"],
}


# -- Tool list --

@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="create_wiki",
            description=(
                "Create a structured wiki in ClickUp. Provide a ClickUp URL "
                "and a tree of pages with titles and markdown content. "
                "Waits for the upload to finish and returns the result."
            ),
            inputSchema=_CREATE_WIKI_SCHEMA,
        ),
        types.Tool(
            name="check_wiki_status",
            description="Check the status of a wiki creation job by its job_id.",
            inputSchema=_CHECK_STATUS_SCHEMA,
        ),
    ]


# -- Tool dispatch --

@server.call_tool()
async def handle_call_tool(
    name: str, arguments: dict[str, Any] | None
) -> list[types.TextContent]:
    arguments = arguments or {}
    try:
        if name == "create_wiki":
            result = await _create_wiki(arguments)
        elif name == "check_wiki_status":
            result = await _check_status(arguments)
        else:
            result = {"error": f"Unknown tool: {name}"}
    except Exception as exc:
        log.exception("Tool %s failed", name)
        result = {"error": str(exc)}

    return [types.TextContent(type="text", text=json.dumps(result, indent=2))]


# -- Tool implementations --

def _build_pages(raw: list[dict]) -> list[WikiPage]:
    out: list[WikiPage] = []
    for p in raw:
        out.append(WikiPage(
            title=p.get("title", "Untitled"),
            content=p.get("content", ""),
            children=_build_pages(p.get("children", [])),
        ))
    return out


async def _create_wiki(args: dict) -> dict:
    url = args.get("url", "")
    if not url:
        return {"error": "Missing required parameter: url"}
    raw_pages = args.get("pages", [])
    if not raw_pages:
        return {"error": "Missing required parameter: pages (must be non-empty)"}

    pages = _build_pages(raw_pages)
    request = WikiCreateRequest(
        doc_name=args.get("doc_name", "Wiki"),
        target=TargetLocation(url=url),
        pages=pages,
    )

    settings = get_settings()
    if not settings.clickup_api_key:
        return {"error": "CLICKUP_API_KEY is not configured on the server"}

    job_id = await run_wiki_creation(request, settings)
    log.info("MCP create_wiki → job %s started", job_id)

    # Poll until done (up to ~3 min)
    for _ in range(90):
        await asyncio.sleep(2)
        job = get_job(job_id)
        if job and job.status.value in ("completed", "failed"):
            return job.model_dump()

    # Timed out – return partial
    job = get_job(job_id)
    if job:
        d = job.model_dump()
        d["warning"] = "Job still running. Use check_wiki_status to poll."
        return d
    return {"error": "Job disappeared", "job_id": job_id}


async def _check_status(args: dict) -> dict:
    job_id = args.get("job_id", "")
    if not job_id:
        return {"error": "Missing required parameter: job_id"}
    job = get_job(job_id)
    if not job:
        return {"error": f"Job '{job_id}' not found"}
    return job.model_dump()


# ── Bearer-auth ASGI middleware ──────────────────────────────────

class BearerAuthMiddleware:
    """
    Wraps an ASGI app and requires a valid JWT Bearer token.
    Skipped entirely when JWT_SECRET is blank (dev mode).
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        settings = get_settings()
        # If no JWT secret configured → skip auth (dev mode)
        if not settings.jwt_secret:
            await self.app(scope, receive, send)
            return

        # Check Authorization header
        headers = dict(scope.get("headers", []))
        auth = headers.get(b"authorization", b"").decode()
        if auth.startswith("Bearer "):
            token = auth[7:]
            if validate_bearer_token(token):
                await self.app(scope, receive, send)
                return

        # Check query-param fallback (some SSE clients can't set headers)
        qs = scope.get("query_string", b"").decode()
        if "token=" in qs:
            from urllib.parse import parse_qs
            params = parse_qs(qs)
            for t in params.get("token", []):
                if validate_bearer_token(t):
                    await self.app(scope, receive, send)
                    return

        # Unauthorized
        resp = JSONResponse({"error": "unauthorized"}, status_code=401)
        await resp(scope, receive, send)


# ── Build the mountable Starlette app ────────────────────────────

def create_mcp_app() -> ASGIApp:
    """
    Return an ASGI app that serves the MCP protocol over SSE.
    Mount it with:  fastapi_app.mount("/mcp", create_mcp_app())
    """
    sse = SseServerTransport("/messages/")

    async def handle_sse(request: Request):
        async with sse.connect_sse(
            request.scope, request.receive, request._send
        ) as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )

    inner = Starlette(
        routes=[
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=sse.handle_post_message),
        ],
    )

    return BearerAuthMiddleware(inner)
