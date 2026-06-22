# Grafux-mcp

MCP Gateway, Registry, and Distributed Tool Execution Runtime.

Grafux-mcp routes tool invocations across multiple backends — local in-process Python
plugins, remote MCP servers (SSE/HTTP), Composio, E2B sandboxes, and Grafux devices —
behind a single FastAPI service with a WebSocket JSON-RPC gateway and Redis-backed
event streaming.

## Architecture

```
app/
  api/            HTTP (v1) + WebSocket gateway + legacy (Qt/WASM) endpoints
  services/       Invocation lifecycle, discovery, sessions, registry, audit
  core/
    runtime/      Execution router + local plugin runtime
                  (plugin_loader, tool_executor, cloud_syncer, diagnostics;
                   local_runner.py is a backward-compatible facade)
    streaming/    Redis pub/sub event bus + WebSocket fan-out
    lifecycle/    Health monitor, server connection manager
    http_client.py  Pooled httpx.AsyncClient (one per event loop)
    threadpool.py   Bounded executor for blocking I/O (S3/Supabase/file/tool)
    errors.py       MCP tool-result envelope helpers
    exceptions.py   Typed error hierarchy (MCPError, RoutingError, ...)
  integrations/   remote / composio / devices / e2b backends
  models/         SQLAlchemy ORM (PostgreSQL)
  schemas/        Pydantic v2 request/response models
  cache/          Redis client with in-process fallback
  security/       JWT validation (delegated to Grafux-backend), internal keys
  shared/         factories (generate_uuid / utcnow)
```

Key reliability properties:

- **Concurrency cap** — `max_concurrent_invocations` is enforced with a semaphore;
  overload applies back-pressure instead of exhausting resources.
- **Transient-only retries** — failed invocations retry (with jittered backoff) only on
  timeout/connection errors; deterministic failures fail fast, so non-idempotent tools
  aren't re-run.
- **Tracked background tasks** — async invocations and the periodic S3 sync hold strong
  references, log their failures, and are cancelled on shutdown.
- **Graceful degradation** — Redis falls back to an in-process stand-in when unreachable
  (single-instance dev only).

## Running

```bash
pip install -r requirements.txt
cp .env.example .env          # then fill in DATABASE_URL, REDIS_URL, INTERNAL_SERVICE_KEY, ...
python -m app.main --port 8002
# or: uvicorn app.main:app --host 0.0.0.0 --port 8002
```

In `production` the service refuses to boot if `INTERNAL_SERVICE_KEY` is still the
default `change_me`.

### Endpoints

- `GET  /api/v1/health` · `/health/liveness` · `/health/readiness`
- `POST /api/v1/execute` (sync) · `/execute/async` · `GET/DELETE /execute/{id}`
- `GET  /api/v1/tools` · `POST /api/v1/registry/servers` · `/api/v1/sessions`
- `GET  /ws` — JSON-RPC 2.0 gateway (token via `?token=`)
- Legacy (Qt/WASM): `GET /health`, `GET /api/tools`, `POST /api/tools/{name}`, `/reload-tools`, `/sync-tool`

## Development

```bash
make install     # deps + test deps (aiosqlite, redis)
make test        # pytest
make lint        # ruff check
make format      # ruff format + black
```

### Testing notes

The ORM models use PostgreSQL-only column types (`JSONB`, `UUID`) that don't render on
SQLite, so tests don't build a real DB. DB-touching tests use the lightweight
`tests/_fakes.py::FakeSession` (which also lets them assert query counts). Redis is forced
to its in-process fallback in `tests/conftest.py`.
