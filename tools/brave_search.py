"""
Brave Web Search MCP Tool
=========================
Exposes a `brave_search` tool that performs a live web search via the Brave
Search API and returns the top results as plain text that can be written to
an output port.

Environment variable required:
    BRAVE_SEARCH_API_KEY  — Brave Search subscription token.
                            Get one at https://api.search.brave.com/

Tool input schema:
    query  (str, required) — the search query
    count  (int, optional) — number of results to return (default 10, max 20)

Tool output:
    A string containing numbered result entries (title, description, URL).
"""

import os
import json
import urllib.request
import urllib.parse
import urllib.error


TOOL_NAME = "brave_search"
TOOL_DESCRIPTION = (
    "Search the web using the Brave Search API. "
    "Returns the top web results (title, description, URL) for the given query. "
    "Useful for gathering live, up-to-date information on any topic."
)
TOOL_INPUT_SCHEMA = {
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
}


def run(arguments: dict) -> str:
    """Execute the Brave Search and return formatted plain-text results."""
    api_key = os.environ.get("BRAVE_SEARCH_API_KEY", "").strip()
    if not api_key:
        return "Error: BRAVE_SEARCH_API_KEY environment variable is not set."

    query = arguments.get("query", "").strip()
    if not query:
        return "Error: 'query' argument is required."

    count = int(arguments.get("count", 10))
    count = max(1, min(count, 20))

    params = urllib.parse.urlencode({
        "q": query,
        "count": count,
        "text_decorations": "false",
        "search_lang": "en",
    })
    url = f"https://api.search.brave.com/res/v1/web/search?{params}"

    req = urllib.request.Request(url)
    req.add_header("Accept", "application/json")
    req.add_header("X-Subscription-Token", api_key)

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return f"Error: Brave Search API returned HTTP {exc.code}: {exc.reason}"
    except Exception as exc:  # noqa: BLE001
        return f"Error: {exc}"

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return f"Error: Failed to parse Brave Search response: {exc}"

    results = data.get("web", {}).get("results", [])
    if not results:
        return "No results found."

    lines = []
    for idx, item in enumerate(results, start=1):
        title = item.get("title", "").strip()
        description = item.get("description", "").strip()
        link = item.get("url", "").strip()
        entry = f"[{idx}] {title}"
        if description:
            entry += f"\n{description}"
        if link:
            entry += f"\nSource: {link}"
        lines.append(entry)

    return "\n\n".join(lines)
