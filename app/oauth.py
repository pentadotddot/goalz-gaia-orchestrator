"""
OAuth 2.1 with PKCE – required by ClickUp MCP integration.

Implements:
  - /.well-known/oauth-authorization-server   (discovery)
  - /oauth/authorize                          (consent screen + code grant)
  - /oauth/token                              (code → token exchange)
  - /oauth/register                           (dynamic client registration)

Tokens are short-lived JWTs verified on every MCP request.
"""

from __future__ import annotations

import base64
import hashlib
import html
import logging
import secrets
import time
from typing import Optional
from urllib.parse import urlencode

import jwt
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.config import get_settings

log = logging.getLogger(__name__)

router = APIRouter(tags=["oauth"])

# ── In-memory stores (swap for Redis / DB in production) ─────────

_auth_codes: dict[str, dict] = {}
_refresh_tokens: dict[str, dict] = {}
_registered_clients: dict[str, dict] = {}   # client_id → metadata


# ── Helpers ──────────────────────────────────────────────────────

def _jwt_secret() -> str:
    s = get_settings()
    if s.jwt_secret:
        return s.jwt_secret
    # Fallback chain: api_secret → deterministic default
    return s.api_secret or "gaia-orchestrator-dev-jwt-secret"


def _issuer(request: Request) -> str:
    """Return the OAuth issuer URL (public base URL of this service)."""
    s = get_settings()
    if s.oauth_issuer:
        return s.oauth_issuer.rstrip("/")
    # Auto-detect from the incoming request
    return str(request.base_url).rstrip("/")


def validate_bearer_token(token: str) -> Optional[dict]:
    """Validate a JWT Bearer token.  Returns the payload or None."""
    try:
        return jwt.decode(token, _jwt_secret(), algorithms=["HS256"])
    except jwt.InvalidTokenError:
        return None


# ── Discovery ────────────────────────────────────────────────────

@router.get("/.well-known/oauth-authorization-server")
async def oauth_metadata(request: Request):
    iss = _issuer(request)
    return {
        "issuer": iss,
        "authorization_endpoint": f"{iss}/oauth/authorize",
        "token_endpoint": f"{iss}/oauth/token",
        "registration_endpoint": f"{iss}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
        "scopes_supported": ["mcp:tools"],
    }


# ── Dynamic client registration (MCP spec recommended) ──────────

@router.post("/oauth/register")
async def register_client(request: Request):
    data = await request.json()
    client_id = secrets.token_urlsafe(16)
    meta = {
        "client_id": client_id,
        "client_name": data.get("client_name", "Unknown"),
        "redirect_uris": data.get("redirect_uris", []),
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    _registered_clients[client_id] = meta
    log.info("Registered OAuth client: %s (%s)", client_id, meta["client_name"])
    return JSONResponse(meta, status_code=201)


# ── Authorization endpoint ───────────────────────────────────────

@router.get("/oauth/authorize", response_class=HTMLResponse)
async def authorize_get(request: Request):
    """Render the consent page."""
    params = request.query_params
    response_type = params.get("response_type", "code")
    client_id = params.get("client_id", "")
    redirect_uri = params.get("redirect_uri", "")
    code_challenge = params.get("code_challenge", "")
    code_challenge_method = params.get("code_challenge_method", "S256")
    state = params.get("state", "")
    scope = params.get("scope", "mcp:tools")

    page = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Authorize – Gaia Orchestrator</title>
<style>
  body{{font-family:system-ui,sans-serif;display:flex;justify-content:center;
       align-items:center;min-height:100vh;background:#0f172a;color:#e2e8f0;margin:0}}
  .card{{background:#1e293b;padding:2.5rem;border-radius:1rem;max-width:420px;
        box-shadow:0 25px 50px rgba(0,0,0,.5);text-align:center}}
  h1{{font-size:1.5rem;margin-bottom:.5rem}}
  p{{color:#94a3b8;line-height:1.6}}
  .client{{color:#60a5fa;font-weight:600}}
  .btn{{display:inline-block;padding:.75rem 2rem;border-radius:.5rem;font-size:1rem;
       font-weight:600;cursor:pointer;border:none;margin:.5rem}}
  .btn-primary{{background:#3b82f6;color:#fff}} .btn-primary:hover{{background:#2563eb}}
  .btn-secondary{{background:#334155;color:#94a3b8}}
</style></head><body>
<div class="card">
  <h1>Gaia Orchestrator</h1>
  <p><span class="client">{html.escape(client_id or 'ClickUp')}</span> wants to
     connect to create wikis on your behalf.</p>
  <form method="POST" action="/oauth/authorize">
    <input type="hidden" name="response_type" value="{html.escape(response_type)}">
    <input type="hidden" name="client_id"     value="{html.escape(client_id)}">
    <input type="hidden" name="redirect_uri"  value="{html.escape(redirect_uri)}">
    <input type="hidden" name="code_challenge" value="{html.escape(code_challenge)}">
    <input type="hidden" name="code_challenge_method" value="{html.escape(code_challenge_method)}">
    <input type="hidden" name="state"         value="{html.escape(state)}">
    <input type="hidden" name="scope"         value="{html.escape(scope)}">
    <button type="submit" name="action" value="approve" class="btn btn-primary">Authorize</button>
    <button type="submit" name="action" value="deny"    class="btn btn-secondary">Deny</button>
  </form>
</div></body></html>"""
    return HTMLResponse(page)


@router.post("/oauth/authorize")
async def authorize_post(request: Request):
    """Process the user's authorization decision."""
    form = await request.form()
    redirect_uri = str(form.get("redirect_uri", ""))
    state = str(form.get("state", ""))
    action = str(form.get("action", "deny"))

    sep = "&" if "?" in redirect_uri else "?"

    if action != "approve":
        params = {"error": "access_denied", "error_description": "User denied access"}
        if state:
            params["state"] = state
        return RedirectResponse(f"{redirect_uri}{sep}{urlencode(params)}", status_code=302)

    # Issue authorization code
    code = secrets.token_urlsafe(32)
    _auth_codes[code] = {
        "client_id": str(form.get("client_id", "")),
        "redirect_uri": redirect_uri,
        "code_challenge": str(form.get("code_challenge", "")),
        "code_challenge_method": str(form.get("code_challenge_method", "S256")),
        "scope": str(form.get("scope", "mcp:tools")),
        "expires_at": time.time() + 300,
    }

    params = {"code": code}
    if state:
        params["state"] = state
    return RedirectResponse(f"{redirect_uri}{sep}{urlencode(params)}", status_code=302)


# ── Token endpoint ───────────────────────────────────────────────

@router.post("/oauth/token")
async def token_endpoint(request: Request):
    ct = request.headers.get("content-type", "")
    if "json" in ct:
        data = await request.json()
    else:
        data = dict(await request.form())

    grant_type = data.get("grant_type")
    if grant_type == "authorization_code":
        return _exchange_code(data)
    if grant_type == "refresh_token":
        return _do_refresh(data)
    return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)


def _exchange_code(data: dict) -> JSONResponse:
    code = data.get("code", "")
    code_verifier = data.get("code_verifier", "")
    client_id = data.get("client_id", "")
    redirect_uri = data.get("redirect_uri")

    stored = _auth_codes.pop(code, None)
    if not stored:
        return JSONResponse({"error": "invalid_grant", "error_description": "Invalid or expired code"}, status_code=400)
    if stored["expires_at"] < time.time():
        return JSONResponse({"error": "invalid_grant", "error_description": "Code expired"}, status_code=400)
    if stored["client_id"] != client_id:
        return JSONResponse({"error": "invalid_grant", "error_description": "Client ID mismatch"}, status_code=400)
    if redirect_uri and stored["redirect_uri"] != redirect_uri:
        return JSONResponse({"error": "invalid_grant", "error_description": "Redirect URI mismatch"}, status_code=400)

    # PKCE verification
    if stored["code_challenge_method"] == "S256":
        digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
        expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    else:
        expected = code_verifier
    if expected != stored["code_challenge"]:
        return JSONResponse({"error": "invalid_grant", "error_description": "PKCE verification failed"}, status_code=400)

    return _issue_tokens(client_id, stored["scope"])


def _do_refresh(data: dict) -> JSONResponse:
    token = data.get("refresh_token", "")
    client_id = data.get("client_id", "")
    stored = _refresh_tokens.pop(token, None)
    if not stored or stored["client_id"] != client_id:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    return _issue_tokens(client_id, stored["scope"])


def _issue_tokens(client_id: str, scope: str) -> JSONResponse:
    now = time.time()
    access = jwt.encode(
        {"sub": client_id, "scope": scope, "iat": now, "exp": now + 3600 * 8},
        _jwt_secret(),
        algorithm="HS256",
    )
    refresh = secrets.token_urlsafe(48)
    _refresh_tokens[refresh] = {"client_id": client_id, "scope": scope, "created_at": now}

    return JSONResponse({
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": 3600 * 8,
        "refresh_token": refresh,
        "scope": scope,
    })
