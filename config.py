"""
config.py — Server-wide configuration, environment setup, and shared utilities.

Imported by every other module; must have no internal dependencies.
"""

import os
import sys
import threading
from pathlib import Path


# ── Optional dependencies ─────────────────────────────────────────────────────

try:
    import boto3  # noqa: F401  (availability check only)
    S3_AVAILABLE = True
except ImportError:
    print("[WARN] boto3 not installed - S3 sync will not be available", file=sys.stderr)
    print("       Install with: pip install boto3", file=sys.stderr)
    S3_AVAILABLE = False

# Set to 0 to disable periodic sync (startup sync still runs once)
SYNC_INTERVAL_SECONDS = int(os.environ.get('S3_SYNC_INTERVAL', '600'))


# ── Google Cloud credentials setup ───────────────────────────────────────────
# On Render, set GCP_SERVICE_ACCOUNT_JSON to the full JSON content of your
# service account key file so Vertex AI picks up credentials automatically.

_gcp_sa_json = os.environ.get('GCP_SERVICE_ACCOUNT_JSON', '')
if _gcp_sa_json:
    import tempfile
    _creds_file = tempfile.NamedTemporaryFile(
        mode='w', suffix='.json', delete=False, prefix='gcp_sa_'
    )
    _creds_file.write(_gcp_sa_json)
    _creds_file.flush()
    _creds_file.close()
    os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = _creds_file.name
    print(f"[INFO] GCP credentials written to {_creds_file.name}", file=sys.stderr)
else:
    print(
        "[WARN] GCP_SERVICE_ACCOUNT_JSON not set - "
        "Vertex AI tools will fail unless credentials are configured another way",
        file=sys.stderr,
    )


# ── Project root ──────────────────────────────────────────────────────────────

def _get_project_root() -> Path:
    """Return the repository/project root directory.

    config.py lives at the repo root alongside mcp_server.py, so the parent
    of this file is always the correct project root both locally and on Render
    (/opt/render/project/src/).
    """
    return Path(__file__).resolve().parent


# ── Per-thread log accumulator for tool calls ─────────────────────────────────
# The WebSocket handler collects [WARN] messages emitted during a tool call and
# includes them in the JSON-RPC error response so the Qt client can display
# them in the warnings / errors output ports.

_tool_call_logs = threading.local()


def _tl_log_warn(msg: str) -> None:
    """Print [WARN] to stderr and accumulate in the current thread's buffer."""
    print(f"[WARN] {msg}", file=sys.stderr)
    if not hasattr(_tool_call_logs, 'warnings'):
        _tool_call_logs.warnings = []
    _tool_call_logs.warnings.append(msg)
