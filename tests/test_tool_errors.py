"""Ablehnungen (ValueError) müssen beim MCP-Client mit Begründung ankommen."""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402


def test_value_error_message_reaches_client():
    with pytest.raises(ToolError, match="Ungültige handler_id"):
        asyncio.run(server.mcp.call_tool("set_php_handler", {"handler_id": "x y", "domain": "example.com"}))


def test_module_functions_still_raise_value_error():
    with pytest.raises(ValueError, match="Ungültige handler_id"):
        server.set_php_handler("x y", domain="example.com")
