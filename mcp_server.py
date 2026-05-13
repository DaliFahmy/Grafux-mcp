# Render compatibility shim.
# The Render service was originally configured with:
#   uvicorn mcp_server:app --host 0.0.0.0 --port $PORT
# This file makes that command work while the real app lives in app/main.py.
# Update the Render start command to "uvicorn app.main:app ..." to remove this.
from app.main import app  # noqa: F401
