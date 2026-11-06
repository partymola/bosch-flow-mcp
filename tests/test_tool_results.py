"""Every tool's result reaches a client as text alone.

A tool annotated `-> str` would otherwise also carry `structuredContent`, the
same JSON again as an escaped string, and declare an `outputSchema`. A client
that shows the model the structured copy shows it a longer reply than the text.
These go through the server's registry, the layer that builds both.
"""

import asyncio
import importlib
import json
import pkgutil

import pytest

import bosch_flow_mcp.tools as tools_pkg
from bosch_flow_mcp.mcp_instance import mcp


def _defined_tools() -> list[str]:
    names = []
    for module_info in pkgutil.iter_modules(tools_pkg.__path__):
        module = importlib.import_module(f"bosch_flow_mcp.tools.{module_info.name}")
        names += [
            name
            for name in dir(module)
            if name.startswith("bosch_") and callable(getattr(module, name))
        ]
    return sorted(names)


DEFINED = _defined_tools()


def _registered():
    return asyncio.run(mcp.list_tools())


def test_every_defined_tool_is_registered():
    """So the tests below cannot pass over an empty or partial registry."""
    assert DEFINED
    assert sorted(tool.name for tool in _registered()) == DEFINED


def test_no_tool_declares_an_output_schema():
    assert [tool.name for tool in _registered() if tool.output_schema is not None] == []


@pytest.mark.parametrize("name", DEFINED)
def test_a_call_returns_text_and_no_structured_content(name, tmp_path, monkeypatch):
    """The gate refuses, so every tool answers at once without a sync or a request."""
    from bosch_flow_mcp import helpers

    monkeypatch.setattr(helpers, "BOSCH_TOKENS_PATH", tmp_path / "absent.json")
    tool = next(tool for tool in _registered() if tool.name == name)
    args = {arg: "x" for arg in tool.input_schema.get("required", [])}

    result = asyncio.run(mcp.call_tool(name, args))

    assert result.structured_content is None
    text = "".join(c.text for c in result.content)
    assert json.loads(text) == {"error": "Bosch not configured. Run: bosch-flow-mcp auth"}
