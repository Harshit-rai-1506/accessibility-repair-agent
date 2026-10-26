"""MCP server: the agent's tools, usable by any MCP client.

The Model Context Protocol (MCP) is a standard way for an AI application to
use tools that live in another process. This server wraps the project's
accessibility tools so that our agent, Claude Desktop, an IDE or the MCP
Inspector can all call them the same way.

Tools (functions a client can call; their JSON schemas come from the type hints):
    scan_html(html, max_nodes)                      axe-core violations in an HTML string
    scan_url(url, max_nodes)                        the same for a public web page
    verify_fix(original_html, fixed_html, rule_id)  did a fix work without breaking or deleting anything?
    explain_rule(rule_id)                           what an axe rule requires and how to pass it
    contrast_fix(foreground, background, target_ratio)
                                                    contrast ratio, and the closest colours that pass
    search_wcag(query, success_criteria, k)         passages from the W3C's WCAG documents (Phase 4)

Resource (read-only data a client can fetch by URI):
    axe-rule://{rule_id}                            same text as explain_rule

Prompt (a ready-made instruction a chat client can offer its user):
    repair_violation(rule_id, html)

Run it:
    python -m a11y_agent.mcp_server                       # stdio: the client starts this process
    python -m a11y_agent.mcp_server --http --port 8765    # HTTP: http://127.0.0.1:8765/mcp

Errors inside a tool (an unknown rule, a colour that cannot be read, HTML
that is too large) are returned to the client as error results with a
readable message, so a model can correct itself instead of crashing.
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import ipaddress
import os
import socket
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Annotated
from urllib.parse import urlparse

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from .contrast import ColorError, suggest_colors
from .scanner import ScanResult, Scanner
from .scoring import violation_score
from .verification import judge_fix

MAX_HTML_CHARS = 2_000_000      # refuse larger inputs: they would only waste time and memory


# ---------------------------------------------------------------------------
# One browser for the whole server, started on first use
# ---------------------------------------------------------------------------

@dataclass
class ServerState:
    """Created when the server starts and closed when it stops (the 'lifespan')."""
    scanner: Scanner | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def get_scanner(self) -> Scanner:
        async with self.lock:
            if self.scanner is None:
                self.scanner = await Scanner().__aenter__()
            return self.scanner

    async def close(self) -> None:
        if self.scanner is not None:
            await self.scanner.__aexit__(None, None, None)
            self.scanner = None


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[ServerState]:
    state = ServerState()
    try:
        yield state
    finally:
        await state.close()


mcp = FastMCP(
    "accessibility-repair",
    instructions=("Tools for finding and fixing web accessibility (WCAG) problems in HTML with axe-core. "
                  "Typical use: scan_html to find violations, explain_rule to understand one, contrast_fix "
                  "for colour problems, then verify_fix to check a corrected version."),
    lifespan=lifespan,
    log_level="WARNING",                 # logs go to stderr; keep them to problems only
)


def _state(ctx: Context) -> ServerState:
    return ctx.request_context.lifespan_context


# ---------------------------------------------------------------------------
# Output schemas (clients see these as each tool's outputSchema)
# ---------------------------------------------------------------------------

class ElementReport(BaseModel):
    html: str = Field(description="The element as axe-core saw it (at most 300 characters, else the opening tag).")
    target: list[str] = Field(description="CSS selector path to the element.")
    failure_summary: str = Field(description="axe-core's explanation of what is wrong.")


class ViolationReport(BaseModel):
    rule_id: str
    impact: str | None
    help: str
    help_url: str
    wcag: list[str] = Field(description="WCAG success criteria, e.g. ['1.1.1']. Empty for best-practice rules.")
    tags: list[str]
    element_count: int
    elements: list[ElementReport]


class ScanReport(BaseModel):
    axe_version: str
    violation_score: int = Field(description="Sum of impact scores (critical 5, serious 4, moderate 3, minor 2).")
    violations: list[ViolationReport]


class FixVerdict(BaseModel):
    passed: bool = Field(description="True if the rule is fixed, nothing else broke, and the content was kept.")
    target_fixed: bool
    introduced: list[str] = Field(description="Rules that fail after the fix but did not before.")
    old_score: int
    new_score: int
    text_kept: float
    elements_kept: float
    content_ok: bool
    problems: list[str]


class ColorSuggestion(BaseModel):
    change: str = Field(description="'text color' or 'background color'.")
    color: str
    ratio: float


class ContrastReport(BaseModel):
    foreground: str
    background: str
    ratio: float
    target_ratio: float
    passes: bool
    suggestions: list[ColorSuggestion]
    note: str


def report_from_scan(result: ScanResult, max_nodes: int) -> ScanReport:
    return ScanReport(
        axe_version=result.axe_version,
        violation_score=violation_score(result),
        violations=[
            ViolationReport(rule_id=v.rule_id, impact=v.impact, help=v.help, help_url=v.help_url,
                            wcag=v.wcag_criteria, tags=v.tags, element_count=len(v.nodes),
                            elements=[ElementReport(html=n.html, target=n.target, failure_summary=n.failure_summary)
                                      for n in v.nodes[:max_nodes]])
            for v in result.violations
        ],
    )


def _check_html(html: str, name: str = "html") -> None:
    if len(html) > MAX_HTML_CHARS:
        raise ValueError(f"{name} is {len(html):,} characters; the limit is {MAX_HTML_CHARS:,}.")


def _check_public_url(url: str) -> None:
    """Only public http(s) pages: never this machine or the local network.

    Without this, anyone who can reach the server could make it load internal
    addresses (server-side request forgery). Redirects after the first
    request are not checked here; Phase 5 adds stricter network guardrails.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("url must start with http:// or https:// and include a host name.")
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(parsed.hostname, None)}
    except socket.gaierror:
        raise ValueError(f"cannot resolve the host name {parsed.hostname!r}.")
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%")[0])
        if not ip.is_global:
            raise ValueError(f"{parsed.hostname} resolves to a private or local address ({ip}); only public pages can be scanned.")


# ---------------------------------------------------------------------------
# Rule explanations (from axe-core's rule metadata and the WCAG index)
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _rule_docs() -> dict[str, tuple[str, str]]:
    from .config import settings
    from .loaders import AxeRuleLoader, WCAGIndex
    try:
        index = WCAGIndex.build(settings.corpus_dir / "wcag")
    except FileNotFoundError:               # WCAG documents not downloaded: criterion numbers only
        index = None
    return {d.metadata["rule_id"]: (d.page_content, d.metadata["url"]) for d in AxeRuleLoader(index=index).load()}


def rule_explanation(rule_id: str) -> str:
    docs = _rule_docs()
    if rule_id not in docs:
        close = difflib.get_close_matches(rule_id, docs, n=3)
        hint = f" Did you mean: {', '.join(close)}?" if close else ""
        raise ValueError(f"Unknown axe-core rule {rule_id!r}.{hint}")
    text, url = docs[rule_id]
    return f"{text}\nMore detail: {url}"


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


@mcp.tool(annotations=READ_ONLY)
async def scan_html(
    html: Annotated[str, Field(description="The HTML to check: a full page or a fragment.")],
    ctx: Context,
    max_nodes: Annotated[int, Field(ge=0, le=1000, description="Failing elements to list per rule.")] = 10,
) -> ScanReport:
    """Check HTML for accessibility violations with axe-core (WCAG 2.0-2.2 A/AA/AAA and best practices).

    The page is loaded in a headless browser with network access blocked.
    Returns every violated rule with its impact, WCAG criteria and failing elements.
    """
    _check_html(html)
    scanner = await _state(ctx).get_scanner()
    return report_from_scan(await scanner.scan_html(html), max_nodes)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
async def scan_url(
    url: Annotated[str, Field(description="A public http(s) web page.")],
    ctx: Context,
    max_nodes: Annotated[int, Field(ge=0, le=1000, description="Failing elements to list per rule.")] = 10,
) -> ScanReport:
    """Load a public web page in a headless browser and check it for accessibility violations."""
    _check_public_url(url)
    scanner = await _state(ctx).get_scanner()
    return report_from_scan(await scanner.scan_url(url), max_nodes)


@mcp.tool(annotations=READ_ONLY)
async def verify_fix(
    original_html: Annotated[str, Field(description="The HTML before the fix.")],
    fixed_html: Annotated[str, Field(description="The HTML after the fix.")],
    rule_id: Annotated[str, Field(description="The axe-core rule the fix was for, e.g. 'image-alt'.")],
    ctx: Context,
) -> FixVerdict:
    """Check a fix: is the rule's violation gone, did no other rule start failing, and was the content kept?

    Scores follow the AccessGuru metric (0 = fixed cleanly). 'problems' explains any failure.
    """
    _check_html(original_html, "original_html")
    _check_html(fixed_html, "fixed_html")
    scanner = await _state(ctx).get_scanner()
    before, after = await scanner.scan_html(original_html), await scanner.scan_html(fixed_html)
    if before.get(rule_id) is None:
        raise ValueError(f"The rule {rule_id!r} does not fail in original_html, so there is nothing to verify. "
                         f"Failing rules: {', '.join(sorted(before.rule_ids)) or 'none'}.")
    return FixVerdict(**judge_fix(before, after, rule_id, original_html, fixed_html).to_dict())


@mcp.tool(annotations=READ_ONLY)
def explain_rule(
    rule_id: Annotated[str, Field(description="An axe-core rule id, e.g. 'color-contrast' or 'link-name'.")],
) -> str:
    """Explain an axe-core rule: what it checks, its impact, the WCAG criteria, and the conditions that make an element pass."""
    return rule_explanation(rule_id)


@mcp.tool(annotations=READ_ONLY)
def contrast_fix(
    foreground: Annotated[str, Field(description="Text colour, e.g. '#767676' or 'rgb(118, 118, 118)'.")],
    background: Annotated[str, Field(description="Background colour behind the text.")],
    target_ratio: Annotated[float, Field(description="Required ratio: 4.5 for normal text (AA), 3 for large text "
                                                     "(AA), 7 for normal text (AAA, rule color-contrast-enhanced), "
                                                     "4.5 for large text (AAA).")] = 4.5,
) -> ContrastReport:
    """Compute the contrast ratio of two colours and suggest the closest colours that reach the target ratio.

    Use this instead of guessing colours: it applies the exact WCAG formula.
    """
    try:
        return ContrastReport(**suggest_colors(foreground, background, target_ratio))
    except ColorError as e:
        raise ValueError(str(e)) from e


class Passage(BaseModel):
    title: str
    section: str
    url: str
    doc_type: str = Field(description="technique, failure, understanding, success_criterion or axe_rule")
    technique_id: str = Field(description="e.g. H37 or F65; empty for other pages")
    sc: str = Field(description="success criteria the page belongs to, comma-separated")
    text: str


class SearchResults(BaseModel):
    passages: list[Passage]


@mcp.tool(annotations=READ_ONLY)
async def search_wcag(
    query: Annotated[str, Field(description="What to look up, e.g. 'how to give an icon link an accessible name'.")],
    success_criteria: Annotated[list[str], Field(description="Only these WCAG success criteria, e.g. ['2.4.4']. "
                                                             "Empty for all.")] = [],  # noqa: B006
    k: Annotated[int, Field(ge=1, le=10, description="How many passages to return.")] = 3,
    page_types: Annotated[list[str], Field(description="Only these page types: technique, failure, understanding, "
                                                       "success_criterion, axe_rule. Empty for all.")] = [],  # noqa: B006
) -> SearchResults:
    """Search the W3C's WCAG 2.2 Understanding documents, Techniques and Failures, and axe-core's rule texts.

    Hybrid search (keywords + meaning). Returns the best passages with title, section and link.
    """
    from .retrieval import shared_index
    index = await asyncio.to_thread(shared_index)        # built on first use (embeddings are cached)
    hits = index.search(query, k, criteria=success_criteria or None, doc_types=tuple(page_types) or None)
    return SearchResults(passages=[Passage(**h.as_dict()) for h in hits])


@mcp.resource("axe-rule://{rule_id}", mime_type="text/plain")
def axe_rule(rule_id: str) -> str:
    """What an axe-core rule checks and how to pass it."""
    return rule_explanation(rule_id)


@mcp.prompt()
def repair_violation(rule_id: str, html: str) -> str:
    """Fix one accessibility violation in an HTML snippet, checking the result with the tools."""
    return (f'Fix the accessibility violation "{rule_id}" in the HTML below.\n'
            "1. Call scan_html to see the failing elements and axe-core's explanation.\n"
            "2. Call explain_rule if you are unsure what the rule requires; call contrast_fix for colour problems.\n"
            "3. Change as little as possible. Keep all text and elements; never delete or hide content.\n"
            "4. Call verify_fix with the original and your corrected HTML, and repeat until it passes.\n"
            "5. Reply with the corrected HTML and a one-sentence explanation.\n\n"
            f"```html\n{html}\n```")


def own_stdin() -> None:
    """Give the stdio protocol its own handle to standard input, and point fd 0 at an empty file.

    Over stdio, one thread is always blocked reading the next message from
    standard input. On Windows, a pending read blocks every other operation on
    that pipe, and some libraries touch standard input when their DLL loads
    (numpy does): the import waits for the read, the read waits for the client,
    and the client waits for our answer, forever. With the protocol reading a
    private copy of the handle (not inherited by child processes) and fd 0 on
    NUL, nothing else can reach the pipe. Harmless elsewhere, so done everywhere.
    """
    try:
        protocol = os.dup(0)
    except OSError:                                     # no standard input at all: nothing to protect
        return
    sys.stdin = open(protocol, "r", encoding="utf-8")   # what the MCP stdio transport reads
    empty = os.open(os.devnull, os.O_RDONLY)
    os.dup2(empty, 0)
    os.close(empty)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the accessibility MCP server.")
    parser.add_argument("--http", action="store_true", help="serve over streamable HTTP instead of stdio")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP host (default: this machine only)")
    parser.add_argument("--port", type=int, default=8765, help="HTTP port")
    args = parser.parse_args(argv)
    if args.http:
        mcp.settings.host, mcp.settings.port = args.host, args.port
        print(f"MCP server on http://{args.host}:{args.port}{mcp.settings.streamable_http_path}", file=sys.stderr)
        mcp.run(transport="streamable-http")
    else:
        own_stdin()
        mcp.run(transport="stdio")       # stdout carries the protocol: never print to it
    return 0


if __name__ == "__main__":
    sys.exit(main())
