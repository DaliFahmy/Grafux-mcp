"""M2 — the MCP error/text envelope helpers produce the canonical shape."""

from __future__ import annotations

from app.core.errors import tool_error, tool_text


def test_tool_text_shape():
    assert tool_text("hi") == {"content": [{"type": "text", "text": "hi"}]}


def test_tool_error_sets_is_error():
    result = tool_error("boom")
    assert result["isError"] is True
    assert result["content"] == [{"type": "text", "text": "boom"}]
    assert "diagnostics" not in result


def test_tool_error_with_diagnostics():
    result = tool_error("boom", diagnostics={"improvements": "fix it"})
    assert result["diagnostics"] == {"improvements": "fix it"}
