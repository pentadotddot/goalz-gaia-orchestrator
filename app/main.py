"""
Gaia Orchestrator – FastAPI entry point.

Run with:
    uvicorn app.main:app --reload
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.mcp_server import create_mcp_app
from app.oauth import router as oauth_router
from app.routes import router as api_router

settings = get_settings()

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
    datefmt="%H:%M:%S",
)

app = FastAPI(
    title="Gaia Orchestrator",
    description=(
        "Receives a structured wiki payload from a ClickUp SuperAgent (or MCP agent) "
        "and creates the corresponding Doc / pages in ClickUp via the API.\n\n"
        "## Endpoints\n"
        "- **REST API** → `/api/v1/wiki` (direct HTTP)\n"
        "- **MCP (SSE)** → `/mcp/sse` (for ClickUp AI Agents)\n"
        "- **OAuth 2.1** → `/.well-known/oauth-authorization-server`\n"
    ),
    version="2.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# Allow ClickUp / any origin to call us (tighten in production)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# REST API routes
app.include_router(api_router)

# OAuth 2.1 routes  (/.well-known/*, /oauth/*)
app.include_router(oauth_router)

# MCP server over SSE  (/mcp/sse, /mcp/messages/)
app.mount("/mcp", create_mcp_app())

log = logging.getLogger("gaia")


@app.on_event("startup")
async def _startup():
    if not settings.clickup_api_key:
        log.warning("CLICKUP_API_KEY is not set – wiki uploads will fail")
    else:
        log.info("CLICKUP_API_KEY loaded")

    if settings.jwt_secret:
        log.info("OAuth / MCP auth ENABLED (JWT_SECRET is set)")
    else:
        log.info("OAuth / MCP auth DISABLED (JWT_SECRET is blank – dev mode)")

    log.info(
        "Gaia Orchestrator ready  →  REST: /docs  |  MCP: /mcp/sse  |  OAuth: /.well-known/oauth-authorization-server",
    )
