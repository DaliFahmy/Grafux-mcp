"""
app/integrations/e2b/executor.py — Python code execution inside E2B sandboxes.

Streams stdout/stderr back via Redis pub/sub so connected WebSocket clients
receive live output.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from app.core.streaming.event_bus import event_bus

logger = logging.getLogger(__name__)


class E2BExecutor:
    """Executes Python code inside an E2B sandbox and streams the output."""

    async def execute(
        self,
        code: str,
        arguments: dict[str, Any],
        org_id: str,
        invocation_id: str,
        persistent_sandbox: bool = False,
        db: Any = None,
    ) -> dict[str, Any]:
        """
        Run `code` (or a generated snippet from `arguments`) in an E2B sandbox.
        Streams stdout/stderr as log events.
        Returns MCP-style content response.
        """
        if not code:
            code = self._build_code_from_arguments(arguments)

        sandbox = None
        is_new = False
        try:
            if db is not None:
                from app.integrations.e2b.sandbox import sandbox_manager
                sandbox, is_new = await sandbox_manager.get_or_create_sandbox(
                    org_id=org_id,
                    db=db,
                    persistent=persistent_sandbox,
                )
            else:
                sandbox = await self._create_ephemeral_sandbox()
                is_new = True

            result = await self._run_in_sandbox(sandbox, code, invocation_id)

            return {
                "content": [{"type": "text", "text": result["output"]}],
                "isError": result.get("error", False),
                "diagnostics": {
                    "stdout": result.get("stdout", ""),
                    "stderr": result.get("stderr", ""),
                    "exit_code": result.get("exit_code", 0),
                },
            }

        except Exception as exc:
            logger.error("E2B execution failed for invocation %s: %s", invocation_id, exc)
            await event_bus.publish_log(invocation_id, "error", f"Sandbox error: {exc}")
            return {
                "content": [{"type": "text", "text": str(exc)}],
                "isError": True,
            }
        finally:
            if sandbox and is_new and not persistent_sandbox:
                try:
                    from app.integrations.e2b.sandbox import sandbox_manager
                    await sandbox_manager.destroy_sandbox(sandbox)
                except Exception:
                    pass

    async def _create_ephemeral_sandbox(self) -> Any:
        from app.config import settings
        try:
            from e2b_code_interpreter import AsyncSandbox
            return await AsyncSandbox.create(api_key=settings.e2b_api_key)
        except ImportError:
            from e2b import AsyncSandbox
            return await AsyncSandbox.create(api_key=settings.e2b_api_key)

    async def _run_in_sandbox(
        self, sandbox: Any, code: str, invocation_id: str
    ) -> dict[str, Any]:
        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []

        async def on_stdout(output: Any) -> None:
            text = str(output) if not isinstance(output, str) else output
            stdout_chunks.append(text)
            await event_bus.publish_log(invocation_id, "stdout", text)

        async def on_stderr(output: Any) -> None:
            text = str(output) if not isinstance(output, str) else output
            stderr_chunks.append(text)
            await event_bus.publish_log(invocation_id, "stderr", text)

        exit_code = 0
        error = False

        try:
            # Try the code-interpreter API (richer output)
            execution = await sandbox.notebook.exec_cell(
                code,
                on_stdout=on_stdout,
                on_stderr=on_stderr,
            )
            stdout = "".join(stdout_chunks)
            stderr = "".join(stderr_chunks)
            # execution.results contains rich outputs (DataFrames, images, etc.)
            text_output = stdout
            if hasattr(execution, "results"):
                for r in execution.results:
                    if hasattr(r, "text"):
                        text_output += r.text or ""
            if hasattr(execution, "error") and execution.error:
                error = True
                stderr += f"\nError: {execution.error}"

        except AttributeError:
            # Fallback: basic process run
            proc = await sandbox.process.start(
                cmd=f'python3 -c "{code}"',
                on_stdout=on_stdout,
                on_stderr=on_stderr,
            )
            await proc.wait()
            exit_code = proc.exit_code or 0
            stdout = "".join(stdout_chunks)
            stderr = "".join(stderr_chunks)
            text_output = stdout or stderr
            error = exit_code != 0

        return {
            "output": text_output,
            "stdout": "".join(stdout_chunks),
            "stderr": "".join(stderr_chunks),
            "exit_code": exit_code,
            "error": error,
        }

    def _build_code_from_arguments(self, arguments: dict[str, Any]) -> str:
        """Build a minimal Python snippet from tool arguments if no explicit code is given."""
        lines = ["# Auto-generated execution from tool arguments"]
        for k, v in arguments.items():
            if isinstance(v, str) and "\n" in v:
                lines.append(f"# {k}:")
                lines.append(v)
            else:
                lines.append(f"{k} = {repr(v)}")
        lines.append("print('Done')")
        return "\n".join(lines)
