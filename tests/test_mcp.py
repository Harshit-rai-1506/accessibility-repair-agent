"""The MCP server and the agent as an MCP client (Phase 3).

These tests start the real server as a separate process over stdio, exactly
as the agent, Claude Desktop or the MCP Inspector would, and use scripted
fake models, so no API key is needed.
"""

import asyncio
import json
from pathlib import Path

import pytest

from a11y_agent.agent import AgentContext, build_graph, run_config
from a11y_agent.benchmark import BenchmarkItem
from a11y_agent.evaluate import run_evaluation
from a11y_agent.mcp_client import connect
from a11y_agent.model_client import ModelClient, RateLimiter, ResponseCache
from a11y_agent.scanner import Scanner
from fakes import EditModel, ToolModel, page_fixer, rule_in, submit, tool_call

pytestmark = [pytest.mark.browser, pytest.mark.data]

ROOT = Path(__file__).resolve().parents[1]
PAGE = ('<!doctype html><html><head><title>Shop</title></head><body><main><h1>Shop</h1>'
        '<img src="cat.png"><button></button><p>Welcome to our shop.</p></main></body></html>')
CONTRAST = ('<!doctype html><html lang="en"><head><title>T</title></head><body><main><h1>T</h1>'
            '<span class="note">Here is how you know</span>'
            '<style>.note { color: #005ea2; background-color: #f1f1f1; }</style></main></body></html>')


def with_server(work):
    """Start the MCP server, run ``work(server)``, stop the server."""
    async def go():
        async with connect() as server:
            return await work(server)
    return asyncio.run(go())


def run_graph(scanner, model_llm, html, *, targets=None, tools=None, max_attempts=3):
    model = ModelClient("fake:model", llm=model_llm, limiter=RateLimiter())
    return build_graph().ainvoke(
        {"html": html, "targets": targets or []}, run_config(max_attempts=max_attempts),
        context=AgentContext(scanner=scanner, model=model, max_attempts=max_attempts, tools=tools))


def outcome(state, rule):
    return next(o for o in state["outcomes"] if o["rule_id"] == rule)


def test_server_offers_tools_resources_and_prompts():
    async def work(server):
        s = server.session
        tools = {t.name: t for t in (await s.list_tools()).tools}
        templates = [t.uriTemplate for t in (await s.list_resource_templates()).resourceTemplates]
        prompts = [p.name for p in (await s.list_prompts()).prompts]
        scan = await s.call_tool("scan_html", {"html": '<img src="a.png">', "max_nodes": 1})
        verify = await s.call_tool("verify_fix", {"original_html": '<img src="a.png">',
                                                  "fixed_html": '<img src="a.png" alt="A logo">', "rule_id": "image-alt"})
        deleted = await s.call_tool("verify_fix", {"original_html": '<p>Hello there</p><img src="a.png">',
                                                   "fixed_html": "<p>Hello there</p>", "rule_id": "image-alt"})
        contrast = await s.call_tool("contrast_fix", {"foreground": "#767676", "background": "#ffffff",
                                                      "target_ratio": 7})
        unknown = await s.call_tool("explain_rule", {"rule_id": "image-alts"})
        private = await s.call_tool("scan_url", {"url": "http://localhost:8000/"})
        resource = await s.read_resource("axe-rule://image-alt")
        return tools, templates, prompts, scan, verify, deleted, contrast, unknown, private, resource

    tools, templates, prompts, scan, verify, deleted, contrast, unknown, private, resource = with_server(work)
    assert set(tools) == {"scan_html", "scan_url", "verify_fix", "explain_rule", "contrast_fix", "search_wcag"}
    assert tools["scan_html"].annotations.readOnlyHint is True
    assert "html" in tools["scan_html"].inputSchema["required"] and tools["scan_html"].outputSchema
    assert templates == ["axe-rule://{rule_id}"] and prompts == ["repair_violation"]
    rules = {v["rule_id"] for v in scan.structuredContent["violations"]}
    assert "image-alt" in rules and scan.structuredContent["violation_score"] > 0
    assert verify.structuredContent["passed"] is True
    assert deleted.structuredContent["passed"] is False                       # deleting the image is not a fix
    assert any("removed content" in p for p in deleted.structuredContent["problems"])
    assert contrast.structuredContent["ratio"] == 4.54 and contrast.structuredContent["suggestions"]
    assert unknown.isError and "Did you mean: image-alt" in unknown.content[0].text      # errors are readable
    assert private.isError and "private or local" in private.content[0].text
    assert "1.1.1 Non-text Content" in resource.contents[0].text


def test_mcp_scanner_gives_the_same_scan_as_the_local_scanner():
    html = (ROOT / "examples" / "broken_page.html").read_text(encoding="utf-8")

    async def local():
        async with Scanner() as scanner:
            return await scanner.scan_html(html)
    remote = with_server(lambda server: server.scanner.scan_html(html))
    here = asyncio.run(local())
    assert {(v.rule_id, v.impact, len(v.nodes)) for v in remote.violations} == \
           {(v.rule_id, v.impact, len(v.nodes)) for v in here.violations}
    assert remote.get("image-alt").wcag_criteria == ["1.1.1"]


def test_agent_through_mcp_matches_the_in_process_agent():
    """Same graph, same model replies: only where the scans run changes, so the result must not."""
    async def local():
        async with Scanner() as scanner:
            return await run_graph(scanner, EditModel(replies=[page_fixer]), PAGE)

    async def remote(server):
        state = await run_graph(server.scanner, EditModel(replies=[page_fixer]), PAGE)
        return state, server.scanner.calls
    here = asyncio.run(local())
    there, scans = with_server(remote)
    assert there["current_html"] == here["current_html"]
    assert there["outcomes"] == here["outcomes"]
    assert scans == 4                                    # the first scan + one verify per rule, all over MCP


def contrast_then_submit(messages):
    """Ask contrast_fix for a colour, then submit an edit that uses the suggestion."""
    last = messages[-1]
    if last.type != "tool":
        return tool_call("contrast_fix", {"foreground": "#005ea2", "background": "#f1f1f1", "target_ratio": 7})
    color = json.loads(last.content)["suggestions"][0]["color"]
    return submit(("color: #005ea2;", f"color: {color};"), explanation=f"Darkened the text to {color}.")


def test_model_uses_the_contrast_tool_and_the_fix_passes():
    llm = ToolModel(replies=[contrast_then_submit])

    async def work(server):
        return await run_graph(server.scanner, llm, CONTRAST, targets=["color-contrast-enhanced"],
                               tools=server.model_tools())
    state = with_server(work)
    o = outcome(state, "color-contrast-enhanced")
    assert o["status"] == "fixed" and o["attempts"] == 1
    assert llm.calls == 2                                     # tool call, then submit_fix
    first_messages, first_choice = llm.seen[0]
    assert first_choice == "required"
    assert llm.seen[1][0][-1].type == "tool"                  # the tool result was sent back to the model
    propose_step = next(s for s in state["steps"] if s["node"] == "propose")
    assert "contrast_fix(" in propose_step["detail"]
    assert len(state["usage"]) == 2


def test_tool_errors_go_back_to_the_model():
    def replies(messages):
        last = messages[-1]
        if last.type != "tool":
            return tool_call("explain_rule", {"rule_id": "image-alts"})
        assert last.status == "error" and "Did you mean" in last.content
        return submit(('<img src="cat.png">', '<img src="cat.png" alt="A cat">'))
    llm = ToolModel(replies=[replies])
    state = with_server(lambda server: run_graph(server.scanner, llm, PAGE, targets=["image-alt"],
                                                 tools=server.model_tools()))
    assert outcome(state, "image-alt")["status"] == "fixed"


def test_tool_rounds_are_bounded_then_submit_is_required():
    def replies(messages):
        if messages[-1].type == "tool" and sum(m.type == "tool" for m in messages) >= 3:
            return submit(('<img src="cat.png">', '<img src="cat.png" alt="A cat">'))
        return tool_call("explain_rule", {"rule_id": "image-alt"}, call_id=f"c{len(messages)}")
    llm = ToolModel(replies=[replies])
    state = with_server(lambda server: run_graph(server.scanner, llm, PAGE, targets=["image-alt"],
                                                 tools=server.model_tools()))
    assert outcome(state, "image-alt")["status"] == "fixed"
    assert [choice for _, choice in llm.seen] == ["required", "required", "required", "submit_fix"]


def test_a_text_answer_is_still_accepted_and_required_falls_back_to_auto():
    text = json.dumps({"explanation": "Added alt text.",
                       "edits": [{"find": '<img src="cat.png">', "replace": '<img src="cat.png" alt="A cat">'}]})
    from langchain_core.messages import AIMessage
    llm = ToolModel(replies=[AIMessage(content=text)], reject_required=True)
    state = with_server(lambda server: run_graph(server.scanner, llm, PAGE, targets=["image-alt"],
                                                 tools=server.model_tools()))
    assert outcome(state, "image-alt")["status"] == "fixed"
    assert llm.seen[0][1] == "auto"                            # the provider refused "required"


def test_agent_tools_as_an_evaluation_method(tmp_path):
    def by_rule(messages):
        rule = rule_in(messages[1].content)
        if rule == "image-alt":
            return submit(('<img src="cat.png">', '<img src="cat.png" alt="A cat">'))
        return submit(("<button></button>", '<button aria-label="Search"></button>'))
    items = [
        BenchmarkItem(id="t-001", dataset="t", rule_id="image-alt", category="Syntax", impact="critical",
                      url="https://example.com", description="", html='<img src="cat.png">'),
        BenchmarkItem(id="t-002", dataset="t", rule_id="button-name", category="Syntax", impact="critical",
                      url="https://example.com", description="", html="<button></button>"),
    ]
    cache = ResponseCache(tmp_path / "cache.sqlite")

    def run(fresh):
        llm = ToolModel(replies=[by_rule])
        summary = asyncio.run(run_evaluation("agent_tools", items, dataset="t", split="dev", model_spec="fake:model",
                                             out_dir=tmp_path / "agent_tools", llm=llm, cache=cache,
                                             limiter=RateLimiter(), quiet=True, fresh=fresh))
        return summary, llm.calls
    summary, calls = run(fresh=False)
    assert summary["all"]["score_decrease"] == 1.0 and summary["status"] == {"ok": 2} and calls == 2
    again, calls_again = run(fresh=True)                       # tool-call replies are cached too
    assert again["all"]["score_decrease"] == 1.0 and calls_again == 0


def test_the_same_server_over_http():
    """Streamable HTTP: the server runs on its own; clients connect by URL instead of starting it."""
    import os
    import socket
    import subprocess
    import sys
    import time

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = subprocess.Popen([sys.executable, "-m", "a11y_agent.mcp_server", "--http", "--port", str(port)],
                              cwd=ROOT, env=dict(os.environ), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.2)

        async def call():
            async with connect({"transport": "streamable_http", "url": f"http://127.0.0.1:{port}/mcp"}) as remote:
                return await remote.session.call_tool("contrast_fix", {"foreground": "#000", "background": "#fff"})
        result = asyncio.run(call())
        assert result.structuredContent["ratio"] == 21.0
    finally:
        server.terminate()
        server.wait(timeout=10)


@pytest.fixture
def hash_embeddings(monkeypatch):
    """The server builds its index with the download-free test embedder."""
    monkeypatch.setenv("A11Y_EMBEDDINGS", "hash")


def test_search_wcag_tool(hash_embeddings):
    async def work(server):
        tools = {t.name for t in (await server.session.list_tools()).tools}
        result = await server.session.call_tool("search_wcag", {"query": "alt attributes on img elements",
                                                                "success_criteria": ["1.1.1"], "k": 3})
        guidance = await server.retriever.guidance("Images must have alternative text", ["1.1.1"], 2)
        return tools, result, guidance
    tools, result, guidance = with_server(work)
    assert "search_wcag" in tools
    passages = result.structuredContent["passages"]
    assert len(passages) == 3 and all("1.1.1" in p["sc"].split(",") for p in passages)
    assert len(guidance) == 2 and all(p["doc_type"] in ("technique", "failure", "understanding") for p in guidance)


def test_agent_rag_as_an_evaluation_method(tmp_path, hash_embeddings):
    def by_rule(messages):
        assert "Guidance from the W3C" in messages[1].content          # retrieval ran before the first attempt
        return submit(('<img src="cat.png">', '<img src="cat.png" alt="A cat">'))
    items = [BenchmarkItem(id="t-001", dataset="t", rule_id="image-alt", category="Syntax", impact="critical",
                           url="https://example.com", description="", html='<img src="cat.png">')]
    llm = ToolModel(replies=[by_rule])
    summary = asyncio.run(run_evaluation("agent_rag", items, dataset="t", split="dev", model_spec="fake:model",
                                         out_dir=tmp_path / "agent_rag", llm=llm, cache=ResponseCache(tmp_path / "c.sqlite"),
                                         limiter=RateLimiter(), quiet=True))
    assert summary["all"]["score_decrease"] == 1.0
    assert "search_wcag" in llm.offered[0] and "contrast_fix" in llm.offered[0]
