"""Local tool display-name lookups resolve via NAME_INDEX (not a full TOOLS scan)."""

from __future__ import annotations

import pytest

import app.core.runtime.plugin_loader as pl
from app.core.runtime.tool_executor import call_tool_direct


@pytest.fixture
def clean_registry():
    pl.TOOLS.clear()
    pl.NAME_INDEX.clear()
    pl._plugins_loaded.set()
    yield
    pl.TOOLS.clear()
    pl.NAME_INDEX.clear()


def test_register_populates_name_index(clean_registry):
    @pl.register_tool(
        name="greet",
        description="",
        input_schema={},
        username="u",
        project="p",
        category="c",
    )
    def greet(args):
        return {"content": [{"type": "text", "text": "hi"}]}

    assert pl.NAME_INDEX["greet"] == ["u/p/c/greet"]


def test_display_name_resolves_via_index(clean_registry):
    # Registered with a category, but called WITHOUT one: the namespaced/direct
    # candidates miss, so resolution must go through NAME_INDEX.
    @pl.register_tool(
        name="greet",
        description="",
        input_schema={},
        username="u",
        project="p",
        category="c",
    )
    def greet(args):
        return {"content": [{"type": "text", "text": "hi"}]}

    result = call_tool_direct("greet", {}, "u", "p", None)
    assert result["content"][0]["text"] == "hi"


def test_reload_clears_name_index(clean_registry):
    @pl.register_tool(name="solo", description="", input_schema={})
    def solo(args):
        return {"content": [{"type": "text", "text": "x"}]}

    assert "solo" in pl.NAME_INDEX
    pl.TOOLS.clear()
    pl.NAME_INDEX.clear()
    assert "solo" not in pl.NAME_INDEX
