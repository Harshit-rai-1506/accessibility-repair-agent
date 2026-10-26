"""The LangGraph agent, end to end with the real scanner and a scripted fake model (no API key)."""

import asyncio
import re
from pathlib import Path

import pytest

from a11y_agent.agent import (
    AgentContext,
    build_graph,
    build_request,
    compact_scan,
    find_violation,
    mermaid,
    per_rule_log,
    recursion_limit,
    run_config,
)
from a11y_agent.benchmark import BenchmarkItem
from a11y_agent.evaluate import run_evaluation
from a11y_agent.model_client import ModelClient, QuotaExhausted, RateLimiter, ResponseCache, _error_kind
from a11y_agent.scanner import Scanner
from fakes import EditModel, RateLimitError, page_fixer, proposal, rule_in

ROOT = Path(__file__).resolve().parents[1]

PAGE = ('<!doctype html><html><head><title>Shop</title></head><body><main><h1>Shop</h1>'
        '<img src="cat.png"><button></button><p>Welcome to our shop.</p></main></body></html>')
IMG_ONLY = ('<!doctype html><html lang="en"><head><title>T</title></head><body><main><h1>T</h1>'
            '<img src="cat.png"><p>Some text here to keep.</p></main></body></html>')

FIX_IMG = proposal(('<img src="cat.png">', '<img src="cat.png" alt="A cat">'))
BREAKS_LINK = proposal(('<img src="cat.png">', '<img src="cat.png" alt="A cat"><a href="/more"></a>'))
DELETES_IMG = proposal(('<img src="cat.png">', ""))
NOT_THERE = proposal(('<img src="dog.png">', '<img src="dog.png" alt="A dog">'))
DAILY_LIMIT = RateLimitError("Rate limit reached for model in organization on tokens per day (TPD): "
                             "Limit 200000, Used 199990. Please try again in 1h2m3s.")


def run_agent(html, replies, *, targets=None, max_attempts=3, max_request_tokens=0, checkpointer=None,
              config=None, graph_input="default", llm=None):
    llm = llm or EditModel(replies=replies)
    model = ModelClient("fake:model", llm=llm, limiter=RateLimiter(), max_request_tokens=max_request_tokens)

    async def go():
        async with Scanner() as scanner:
            graph = build_graph(checkpointer=checkpointer)
            payload = {"html": html, "targets": targets or []} if graph_input == "default" else graph_input
            return await graph.ainvoke(payload, config or run_config(max_attempts=max_attempts),
                                       context=AgentContext(scanner=scanner, model=model, max_attempts=max_attempts))
    return asyncio.run(go()), llm


def outcome(state, rule):
    return next(o for o in state["outcomes"] if o["rule_id"] == rule)


pytestmark = [pytest.mark.browser, pytest.mark.data]


def test_fixes_every_rule_on_a_page():
    state, llm = run_agent(PAGE, [page_fixer])
    assert state["summary"]["initial_score"] > 0 and state["summary"]["final_score"] == 0
    assert set(state["summary"]["by_status"]["fixed"]) == {"image-alt", "button-name", "html-has-lang"}
    assert 'alt="A cat"' in state["current_html"] and 'lang="en"' in state["current_html"]
    assert "Welcome to our shop." in state["current_html"]
    assert llm.calls == 3
    # most severe first: the two critical rules before the serious one
    picked = [s["rule"] for s in state["steps"] if s["node"] == "pick" and s.get("rule")]
    assert picked[-1] == "html-has-lang"
    assert [s["node"] for s in state["steps"]][:2] == ["scan", "pick"] and state["steps"][-1]["node"] == "report"


def test_only_the_requested_rule_is_fixed():
    state, llm = run_agent(PAGE, [page_fixer], targets=["button-name"])
    assert state["summary"]["by_status"] == {"fixed": ["button-name"]}
    assert "image-alt" in state["summary"]["remaining_rules"]
    assert llm.calls == 1


def test_retries_with_the_scanners_feedback():
    state, llm = run_agent(IMG_ONLY, [BREAKS_LINK, FIX_IMG], targets=["image-alt"])
    assert outcome(state, "image-alt")["status"] == "fixed"
    assert outcome(state, "image-alt")["attempts"] == 2
    assert "<a href" not in state["current_html"]
    retry_prompt = llm.prompts[1]
    assert "Earlier attempts did not work" in retry_prompt
    assert '"link-name"' in retry_prompt                          # the rule the first attempt broke
    assert llm.prompts[0].count("Earlier attempts") == 0


def test_unusable_replies_become_feedback():
    state, llm = run_agent(IMG_ONLY, ["not json", NOT_THERE, FIX_IMG], targets=["image-alt"])
    assert outcome(state, "image-alt")["status"] == "fixed"
    assert outcome(state, "image-alt")["attempts"] == 3
    assert "not valid JSON" in llm.prompts[1]
    assert "was not found" in llm.prompts[2]


def test_gives_up_after_max_attempts_and_keeps_the_original():
    state, llm = run_agent(IMG_ONLY, [NOT_THERE], targets=["image-alt"], max_attempts=2)
    assert llm.calls == 2
    assert outcome(state, "image-alt")["status"] == "not_fixed"
    assert state["current_html"] == IMG_ONLY


def test_deleting_content_is_never_accepted():
    state, llm = run_agent(IMG_ONLY, [DELETES_IMG], targets=["image-alt"], max_attempts=2)
    assert outcome(state, "image-alt")["status"] == "not_fixed"
    assert "removed content" in llm.prompts[1]
    assert state["current_html"] == IMG_ONLY


def test_partial_improvement_is_kept_when_out_of_attempts():
    """Target fixed but a less severe rule now fails: lower score, so it is kept (as the metric rewards)."""
    state, _ = run_agent(IMG_ONLY, [BREAKS_LINK], targets=["image-alt"], max_attempts=2)
    o = outcome(state, "image-alt")
    assert o["status"] == "improved" and o["introduced"] == ["link-name"]
    assert o["new_score"] < o["old_score"]
    assert 'alt="A cat"' in state["current_html"]


def test_page_mode_queues_what_a_kept_fix_broke():
    def fixer(prompt):
        if rule_in(prompt) == "image-alt":
            return BREAKS_LINK
        return proposal(('<a href="/more"></a>', '<a href="/more">More</a>'))
    state, _ = run_agent(IMG_ONLY, [fixer], max_attempts=1)
    assert outcome(state, "image-alt")["status"] == "improved"
    assert outcome(state, "link-name")["status"] == "fixed"
    assert state["summary"]["final_score"] == 0


def test_daily_quota_stops_the_whole_run():
    with pytest.raises(QuotaExhausted):
        run_agent(PAGE, [page_fixer], llm=EditModel(replies=[page_fixer], raise_error=DAILY_LIMIT))


def test_request_too_large_is_reported_without_retrying():
    state, llm = run_agent(IMG_ONLY, [FIX_IMG], targets=["image-alt"], max_request_tokens=50)
    assert outcome(state, "image-alt")["status"] == "too_large"
    assert llm.calls == 0


def test_long_pages_are_excerpted_to_fit():
    filler = "".join(f"<p>Paragraph {i} of filler text for a long page.</p>\n" for i in range(1500))
    html = f'<!doctype html><html lang="en"><head><title>T</title></head><body><main><h1>T</h1>{filler}<img src="cat.png">{filler}</main></body></html>'

    async def scan():
        async with Scanner() as scanner:
            return compact_scan(await scanner.scan_html(html))
    violation = find_violation(asyncio.run(scan()), "image-alt")
    model = ModelClient("fake:model", llm=object(), max_request_tokens=8000)
    messages, max_tokens, excerpted = build_request({"current_html": html, "feedback": []}, violation, model)
    request = sum(len(t) // 4 + 5 for _, t in messages) + max_tokens
    assert excerpted and request <= 8000 and max_tokens >= 1024
    assert '<img src="cat.png">' in messages[1][1]


def test_checkpoints_let_a_stopped_run_resume(tmp_path):
    """Stop at the daily quota on the second rule, then resume: the first rule is not redone."""
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    calls = {"n": 0}

    def quota_after_one(prompt):
        calls["n"] += 1
        if calls["n"] > 1:
            raise DAILY_LIMIT
        return page_fixer(prompt)

    config = run_config(thread_id="page-1")
    db = str(tmp_path / "checkpoints.sqlite")

    async def first_run():
        async with AsyncSqliteSaver.from_conn_string(db) as saver, Scanner() as scanner:
            graph = build_graph(checkpointer=saver)
            model = ModelClient("fake:model", llm=EditModel(replies=[quota_after_one]), limiter=RateLimiter())
            with pytest.raises(QuotaExhausted):
                await graph.ainvoke({"html": PAGE}, config, context=AgentContext(scanner=scanner, model=model))
            snapshot = await graph.aget_state(config)
            return snapshot.next, snapshot.values

    pending, values = asyncio.run(first_run())
    assert pending == ("propose",)                       # stopped inside the second rule
    assert [o["status"] for o in values["outcomes"]] == ["fixed"]

    resumed = EditModel(replies=[page_fixer])

    async def second_run():                              # a new process would do exactly this
        async with AsyncSqliteSaver.from_conn_string(db) as saver, Scanner() as scanner:
            graph = build_graph(checkpointer=saver)
            model = ModelClient("fake:model", llm=resumed, limiter=RateLimiter())
            return await graph.ainvoke(None, config, context=AgentContext(scanner=scanner, model=model))

    state = asyncio.run(second_run())
    assert state["summary"]["final_score"] == 0
    assert resumed.calls == 2                            # only the two rules that were left


def test_per_rule_log_reducer():
    assert per_rule_log(["a"], ["b"]) == ["a", "b"]
    assert per_rule_log(["a"], None) == []
    assert per_rule_log(None, ["x"]) == ["x"]


def test_recursion_limit_covers_the_worst_case():
    # 25 rules x (pick + 3 x (propose + verify) + accept/give_up) + scan, last pick, report
    assert recursion_limit(3, 25) >= 25 * 8 + 3


def test_new_error_kinds():
    class Err(Exception):
        status_code = 400
    assert _error_kind(Err("json_validate_failed: Generated JSON does not match the expected schema"))[0] == "bad_output"
    assert _error_kind(Err("Please reduce the length of the messages or completion."))[0] == "too_large"


class SchemaRejectingModel(EditModel):
    """A provider that does not support response_format: 400 if it is sent."""
    seen_formats: list = []

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen_formats.append("response_format" in kwargs)
        if "response_format" in kwargs:
            error = Exception("Error code: 400 - 'response_format' of type 'json_schema' is not supported")
            error.status_code = 400
            raise error
        return super()._generate(messages, stop, run_manager, **kwargs)


def test_falls_back_to_prompt_only_json_when_schemas_are_unsupported():
    llm = SchemaRejectingModel(replies=[page_fixer])
    state, _ = run_agent(PAGE, [page_fixer], llm=llm)
    assert state["summary"]["final_score"] == 0
    assert llm.seen_formats[:2] == [True, False]          # asked once with the schema, then never again
    assert llm.seen_formats.count(True) == 1


def test_graph_diagram_in_the_docs_is_current():
    """docs/02-agent.md embeds the graph; regenerate it with `python -m a11y_agent.agent --graph`."""
    doc = (ROOT / "docs" / "02-agent.md").read_text(encoding="utf-8")
    edges = [line.strip() for line in mermaid().splitlines() if re.search(r"-->|\.->", line)]
    assert edges and all(edge in doc for edge in edges)


def test_agent_as_an_evaluation_method(tmp_path):
    items = [
        BenchmarkItem(id="t-001", dataset="t", rule_id="image-alt", category="Syntax", impact="critical",
                      url="https://example.com", description="", html='<img src="cat.png">'),
        BenchmarkItem(id="t-002", dataset="t", rule_id="button-name", category="Syntax", impact="critical",
                      url="https://example.com", description="", html="<button></button>"),
    ]
    cache = ResponseCache(tmp_path / "cache.sqlite")

    def run(fresh):
        return asyncio.run(run_evaluation("agent", items, dataset="t", split="dev", model_spec="fake:model",
                                          out_dir=tmp_path / "agent", llm=EditModel(replies=[page_fixer]),
                                          cache=cache, limiter=RateLimiter(), quiet=True, fresh=fresh))
    summary = run(fresh=False)
    assert summary["all"]["score_decrease"] == 1.0
    assert summary["calls"] == 2 and summary["status"] == {"ok": 2}
    again = run(fresh=True)                              # same prompts -> answered from the cache
    assert again["all"]["score_decrease"] == 1.0 and again["paid_cost_usd"] is None
