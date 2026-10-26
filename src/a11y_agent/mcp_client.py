"""The agent's side of MCP: start the server, talk to it, and use its tools.

Two kinds of use, over one connection (one server process):

* **Deterministic steps.** The graph's ``scan`` and ``verify`` nodes need a
  scanner. ``McpScanner`` has the same ``scan_html`` method as the in-process
  ``Scanner``, but each call goes to the server's ``scan_html`` tool. The
  graph does not change at all; only the object passed in does.

* **Tools the model chooses to call.** ``explain_rule`` and ``contrast_fix``
  are loaded as LangChain tools with ``langchain-mcp-adapters``, and offered
  to the model while it works on a fix (see ``agent.py``). The model decides
  whether and when to call them.

    async with connect() as server:
        scan = await server.scanner.scan_html("<img src='a.png'>")
        tools = server.model_tools()          # [explain_rule, contrast_fix]
"""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools
from mcp import ClientSession
from mcp.client.stdio import get_default_environment

from .config import ROOT
from .scanner import ScanResult, Violation, ViolationNode

SERVER_NAME = "accessibility"
MODEL_TOOLS = ("explain_rule", "contrast_fix")       # offered to the model; scanning stays with the graph
PASS_THROUGH_ENV = ("PLAYWRIGHT_BROWSERS_PATH", "PYTHONPATH", "VIRTUAL_ENV", "DATA_DIR", "AXE_VERSION")


class McpToolError(RuntimeError):
    """A tool on the MCP server reported an error."""


def server_connection() -> dict:
    """How to start our server: this Python, ``-m a11y_agent.mcp_server``, over stdio.

    The child process gets a minimal environment (MCP's safe default plus the
    few variables the server needs), not ours, so secrets such as the API key
    are not passed to a process that does not need them.
    """
    env = get_default_environment()
    env.update({k: os.environ[k] for k in PASS_THROUGH_ENV if k in os.environ})
    return {"transport": "stdio", "command": sys.executable, "args": ["-m", "a11y_agent.mcp_server"],
            "env": env, "cwd": str(ROOT)}


def _tool_text(result) -> str:
    return " ".join(getattr(block, "text", "") for block in result.content).strip()


def scan_from_report(report: dict) -> ScanResult:
    """Turn the ``scan_html`` tool's structured output back into a ScanResult."""
    return ScanResult(
        violations=[
            Violation(rule_id=v["rule_id"], impact=v["impact"], help=v["help"], help_url=v["help_url"],
                      tags=list(v["tags"]),
                      nodes=[ViolationNode(target=list(e["target"]), html=e["html"], failure_summary=e["failure_summary"])
                             for e in v["elements"]])
            for v in report["violations"]
        ],
        axe_version=report["axe_version"],
    )


class McpScanner:
    """Same interface as ``Scanner.scan_html``, but the scan runs on the MCP server."""

    def __init__(self, session: ClientSession, max_nodes: int = 1000):
        self.session = session
        self.max_nodes = max_nodes
        self.calls = 0

    async def scan_html(self, html: str) -> ScanResult:
        self.calls += 1
        result = await self.session.call_tool("scan_html", {"html": html, "max_nodes": self.max_nodes})
        if result.isError:
            raise McpToolError(_tool_text(result))
        return scan_from_report(result.structuredContent)


@dataclass
class McpServer:
    """An open connection to the server: the raw session, its tools as LangChain tools, and a scanner."""
    session: ClientSession
    tools: list[BaseTool]
    scanner: McpScanner

    def model_tools(self, names: tuple[str, ...] = MODEL_TOOLS) -> list[BaseTool]:
        return [t for t in self.tools if t.name in names]


@asynccontextmanager
async def connect(connection: dict | None = None) -> AsyncIterator[McpServer]:
    """Start the server (or connect to one) and keep one session open for the whole block."""
    client = MultiServerMCPClient({SERVER_NAME: connection or server_connection()})
    async with client.session(SERVER_NAME) as session:
        tools = await load_mcp_tools(session, server_name=SERVER_NAME)
        yield McpServer(session=session, tools=tools, scanner=McpScanner(session))
