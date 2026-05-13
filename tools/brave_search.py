"""
tools/brave_search.py — Brave Web Search system tool.

Built-in tool loaded by local_runner.py at startup.
Uses the @register_tool decorator from the new runtime.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request


try:
    from app.core.runtime.local_runner import register_tool
except ImportError:
    # Fallback when loaded via importlib at startup before app is on sys.path
    def register_tool(name, description, input_schema, **_kw):
        def wrapper(func):
            return func
        return wrapper


@register_tool(
    name="brave_search",
    description=(
        "Search the web using the Brave Search API. "
        "Returns the top web results (title, description, URL) for the given query. "
        "Useful for gathering live, up-to-date information on any topic."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query string.",
            },
            "count": {
                "type": "integer",
                "description": "Number of results to return (1–20, default 10).",
                "default": 10,
                "minimum": 1,
                "maximum": 20,
            },
        },
        "required": ["query"],
    },
)
def brave_search(arguments: dict) -> dict:
    api_key = os.environ.get("BRAVE_SEARCH_API_KEY", "").strip()
    if not api_key:
        return {"content": [{"type": "text", "text": "Error: BRAVE_SEARCH_API_KEY not set"}]}

    query = arguments.get("query", "").strip()
    if not query:
        return {"content": [{"type": "text", "text": "Error: 'query' argument is required"}]}

    count = max(1, min(int(arguments.get("count", 10)), 20))

    params = urllib.parse.urlencode(
        {"q": query, "count": count, "text_decorations": "false", "search_lang": "en"}
    )
    url = f"https://api.search.brave.com/res/v1/web/search?{params}"
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/json")
    req.add_header("X-Subscription-Token", api_key)

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"content": [{"type": "text", "text": f"HTTP {exc.code}: {exc.reason}"}]}
    except Exception as exc:
        return {"content": [{"type": "text", "text": f"Error: {exc}"}]}

    results = data.get("web", {}).get("results", [])
    if not results:
        return {"content": [{"type": "text", "text": "No results found."}]}

    lines = []
    for idx, item in enumerate(results, start=1):
        title = item.get("title", "").strip()
        desc = item.get("description", "").strip()
        link = item.get("url", "").strip()
        entry = f"[{idx}] {title}"
        if desc:
            entry += f"\n{desc}"
        if link:
            entry += f"\nSource: {link}"
        lines.append(entry)

    return {"content": [{"type": "text", "text": "\n\n".join(lines)}]}
