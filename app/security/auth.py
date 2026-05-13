"""
app/security/auth.py — JWT validation delegated to Grafux-backend.

Grafux-mcp trusts Grafux-backend as the auth authority. Tokens are validated
by calling the backend's internal validation endpoint, avoiding any local
secret storage.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import settings

logger = logging.getLogger(__name__)

_http_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class AuthContext:
    """Parsed identity information validated by Grafux-backend."""

    user_id: str
    org_id: str
    project_id: str | None
    role: str
    raw: dict[str, Any]


# ── Internal service token (service-to-service) ───────────────────────────────

class InternalServiceAuth:
    """Validate inbound requests from other Grafux services."""

    def __call__(self, request: Request) -> None:
        token = request.headers.get("X-Internal-Service-Key", "")
        if not token or token != settings.internal_service_key:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invalid or missing internal service key",
            )


require_internal = InternalServiceAuth()


# ── User JWT (forwarded from Grafux-backend / client) ────────────────────────

async def _validate_token_with_backend(token: str) -> dict[str, Any]:
    """
    Call Grafux-backend's internal token validation endpoint.

    The backend returns the decoded claims on success or a 401 on failure.
    """
    url = f"{settings.backend_internal_url}/internal/auth/validate"
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Internal-Service-Key": settings.internal_service_key,
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url, headers=headers)

    if resp.status_code == 200:
        return resp.json()
    if resp.status_code in (401, 403):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail="Authentication service unavailable",
    )


async def get_auth_context(
    credentials: HTTPAuthorizationCredentials | None = Depends(_http_bearer),
) -> AuthContext:
    """
    FastAPI dependency — validates JWT and returns an AuthContext.

    Raises 401 if the token is missing or invalid.
    """
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        claims = await _validate_token_with_backend(credentials.credentials)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Token validation failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Authentication service error",
        ) from exc

    user_id = claims.get("sub") or claims.get("user_id")
    org_id = claims.get("org_id") or claims.get("organization_id")

    if not user_id or not org_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing required claims (sub, org_id)",
        )

    return AuthContext(
        user_id=str(user_id),
        org_id=str(org_id),
        project_id=str(claims["project_id"]) if claims.get("project_id") else None,
        role=claims.get("role", "member"),
        raw=claims,
    )


async def get_optional_auth_context(
    credentials: HTTPAuthorizationCredentials | None = Depends(_http_bearer),
) -> AuthContext | None:
    """Like get_auth_context but returns None instead of raising for unauthenticated requests."""
    if not credentials:
        return None
    try:
        return await get_auth_context(credentials)
    except HTTPException:
        return None


# ── WebSocket token extraction ────────────────────────────────────────────────

async def get_ws_auth_context(token: str) -> AuthContext:
    """Validate a token passed as a WebSocket query parameter."""
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing token")
    from fastapi.security import HTTPAuthorizationCredentials
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    return await get_auth_context(creds)
