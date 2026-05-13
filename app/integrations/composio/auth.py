"""
app/integrations/composio/auth.py — Composio OAuth flow helpers.

OAuth credentials are NEVER stored in Grafux-mcp.
This module initiates flows and delegates credential storage to Grafux-backend.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


async def initiate_oauth(
    provider: str,
    org_id: str,
    user_id: str,
    redirect_uri: str,
) -> dict[str, Any]:
    """
    Start a Composio OAuth connection flow.

    Returns the redirect URL that the user must visit to authorize the integration.
    Credentials are ultimately stored in Grafux-backend's secret manager.
    """
    if not settings.composio_api_key:
        raise ValueError("COMPOSIO_API_KEY not configured")

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            "https://backend.composio.dev/api/v1/connectedAccounts",
            headers={
                "x-api-key": settings.composio_api_key,
                "Content-Type": "application/json",
            },
            json={
                "appName": provider,
                "userUuid": f"{org_id}:{user_id}",
                "redirectUri": redirect_uri,
                "authMode": "OAUTH2",
            },
        )
        resp.raise_for_status()
        data = resp.json()

    return {
        "connection_id": data.get("connectedAccountId"),
        "redirect_url": data.get("redirectUrl"),
        "provider": provider,
    }


async def notify_backend_of_connection(
    org_id: str,
    provider: str,
    connection_id: str,
) -> None:
    """
    Notify Grafux-backend that a Composio OAuth connection was established.

    The backend persists the connection metadata in its secret store.
    Grafux-mcp never sees or stores the OAuth tokens.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.post(
                f"{settings.backend_internal_url}/internal/mcp/composio/connected",
                headers={"X-Internal-Service-Key": settings.internal_service_key},
                json={
                    "org_id": org_id,
                    "provider": provider,
                    "connection_id": connection_id,
                },
            )
            resp.raise_for_status()
        except Exception as exc:
            logger.error("Failed to notify backend of Composio connection: %s", exc)
