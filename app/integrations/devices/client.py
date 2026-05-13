"""
app/integrations/devices/client.py — Grafux-devices HTTP/WebSocket client.

Routes device tool calls to the Grafux-devices service and forwards
realtime device events back through the event bus.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

from app.config import settings
from app.core.streaming.event_bus import event_bus

logger = logging.getLogger(__name__)


class DevicesClient:
    """
    Client for Grafux-devices service.

    Handles:
    - Device tool invocation (REST)
    - Realtime device event subscription (WebSocket)
    - OpenClaw integration passthrough
    """

    def __init__(self) -> None:
        self._base_url = settings.devices_internal_url
        self._headers = {"X-Internal-Service-Key": settings.internal_service_key}

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        config: dict[str, Any] | None = None,
        invocation_id: str | None = None,
    ) -> dict[str, Any]:
        """Invoke a device tool via the Grafux-devices REST API."""
        payload = {
            "tool": tool_name,
            "arguments": arguments,
            "config": config or {},
        }
        if invocation_id:
            payload["invocation_id"] = invocation_id

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{self._base_url}/internal/tools/call",
                headers=self._headers,
                json=payload,
            )

        if resp.status_code >= 400:
            error_text = resp.text
            logger.error("Grafux-devices tool call failed %d: %s", resp.status_code, error_text)
            return {
                "content": [{"type": "text", "text": f"Device tool error ({resp.status_code}): {error_text}"}],
                "isError": True,
            }

        return resp.json()

    async def list_tools(self) -> list[dict[str, Any]]:
        """List available device tools from Grafux-devices."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                resp = await client.get(
                    f"{self._base_url}/internal/tools",
                    headers=self._headers,
                )
                resp.raise_for_status()
                return resp.json().get("tools", [])
            except Exception as exc:
                logger.warning("Failed to list device tools: %s", exc)
                return []

    async def stream_device_events(
        self,
        device_id: str,
        invocation_id: str,
        timeout: float = 60.0,
    ) -> None:
        """
        Subscribe to realtime events from a device and forward them
        to the invocation's event bus channel.
        """
        ws_url = self._base_url.replace("http://", "ws://").replace("https://", "wss://")
        ws_url = f"{ws_url}/internal/devices/{device_id}/events"

        try:
            import websockets
            async with websockets.connect(
                ws_url,
                additional_headers=self._headers,
                open_timeout=10,
            ) as ws:
                deadline = asyncio.get_event_loop().time() + timeout
                while asyncio.get_event_loop().time() < deadline:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                        event = json.loads(raw)
                        await event_bus.publish(
                            invocation_id,
                            "device_event",
                            {"device_id": device_id, **event},
                        )
                    except asyncio.TimeoutError:
                        continue
                    except Exception as exc:
                        logger.warning("Device event stream error: %s", exc)
                        break
        except Exception as exc:
            logger.error("Failed to connect to device event stream %s: %s", device_id, exc)

    async def send_command(
        self,
        device_id: str,
        command: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send a realtime command to a device (e.g., OpenClaw move)."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{self._base_url}/internal/devices/{device_id}/command",
                headers=self._headers,
                json={"command": command, "payload": payload or {}},
            )
            resp.raise_for_status()
            return resp.json()
