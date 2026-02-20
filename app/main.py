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
from app.routes import router

settings = get_settings()

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
    datefmt="%H:%M:%S",
)

app = FastAPI(
    title="Gaia Orchestrator",
    description=(
        "Receives a structured wiki payload from a ClickUp SuperAgent and "
        "creates the corresponding Doc / pages in ClickUp via the API."
    ),
    version="1.0.0",
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

app.include_router(router)

log = logging.getLogger("gaia")


@app.on_event("startup")
async def _startup():
    if not settings.clickup_api_key:
        log.warning("CLICKUP_API_KEY is not set – wiki uploads will fail")
    else:
        log.info("CLICKUP_API_KEY loaded")
    log.info(
        "Gaia Orchestrator is ready  (http://%s:%s/docs)",
        settings.host,
        settings.port,
    )
