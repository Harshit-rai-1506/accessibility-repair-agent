"""Repair methods that the evaluation compares.

A *method* receives one benchmark item (broken HTML + the rule it breaks)
and returns a proposed fix. The evaluation harness then scans the fix and
scores it, the same way for every method. Keeping the methods behind one
small interface is what makes the comparison fair: only the method changes.

    noop        returns the HTML unchanged; checks that the harness scores "nothing" as 0
    zero_shot   one model call: rule name + help text, "return the corrected HTML"
    accessguru  the published AccessGuru method: persona + 4-step conversation,
                one corrective re-prompt if the fix fails, best candidate kept
    agent       our LangGraph agent (Phase 2): JSON edits, verified by re-scanning,
                retried with the scanner's feedback, up to 3 attempts
    agent_tools the same agent as an MCP client (Phase 3): its scans run on the MCP
                server, and the model may call explain_rule and contrast_fix
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from .benchmark import BenchmarkItem
from .model_client import CallRecord, ModelClient, RequestTooLarge
from .prompts import (
    ACCESSGURU_MARKERS,
    ACCESSGURU_STEP3,
    ACCESSGURU_STEP4,
    ACCESSGURU_SYSTEM,
    accessguru_step1,
    accessguru_step2,
    extract_html,
    zero_shot_messages,
)
from .scanner import ScanResult, Scanner
from .scoring import evaluate_fix


@dataclass
class FixContext:
    """What a method may use: the model, the scanner, and the original scan."""
    model: ModelClient | None
    scanner: Scanner
    before: ScanResult


@dataclass
class FixResult:
    html: str | None                  # proposed replacement for the snippet; None = no usable answer
    status: str = "ok"                # ok | no_html | too_large | truncated
    calls: list[CallRecord] = field(default_factory=list)
    response: str = ""                # the last raw reply, kept for error analysis
    passes: int = 1                   # how many attempts the method made
    note: str = ""


class Method(Protocol):
    name: str
    uses_model: bool

    async def fix(self, item: BenchmarkItem, ctx: FixContext) -> FixResult: ...


class NoOp:
    """Change nothing. The harness must score this as a 0.0 decrease."""
    name = "noop"
    uses_model = False

    async def fix(self, item: BenchmarkItem, ctx: FixContext) -> FixResult:
        return FixResult(html=item.html, note="unchanged")


def _status_for(reply: str, html: str | None, record: CallRecord) -> str:
    if html is not None:
        return "ok"
    return "truncated" if record.finish_reason == "length" else "no_html"


class ZeroShot:
    """Method A: a single, plain request."""
    name = "zero_shot"
    uses_model = True

    async def fix(self, item: BenchmarkItem, ctx: FixContext) -> FixResult:
        try:
            reply, record = await ctx.model.chat(zero_shot_messages(item.rule_id, item.html),
                                                 max_tokens=ctx.model.output_budget(item.html))
        except RequestTooLarge as e:
            return FixResult(html=None, status="too_large", note=str(e))
        html = extract_html(reply)
        return FixResult(html=html, status=_status_for(reply, html, record), calls=[record], response=reply)


class AccessGuru:
    """Method B: the AccessGuru method (Fathallah et al., ASSETS 2025).

    Pass 1: persona, then four turns in one conversation:
      1. explain the violation   2. propose a correction (with WCAG text)
      3. critique it             4. confirm, with the code between markers
    If the scanner says pass 1 did not fully fix the violation, pass 2 runs the
    same conversation with a stricter first turn. Of the original HTML and the
    two candidates, the one with the lowest violation score is kept (the most
    recent wins ties), so the method never returns something worse than the
    input. This selection step is part of their method and uses the scanner.
    """
    name = "accessguru"
    uses_model = True

    async def _conversation(self, item: BenchmarkItem, ctx: FixContext, reprompt: bool,
                            calls: list[CallRecord]) -> tuple[str | None, str, str]:
        history = [("system", ACCESSGURU_SYSTEM)]
        budget = ctx.model.output_budget(item.html)
        reply, record = "", None
        for prompt in (accessguru_step1(item, reprompt), accessguru_step2(item.rule_id),
                       ACCESSGURU_STEP3, ACCESSGURU_STEP4):
            history.append(("human", prompt))
            reply, record = await ctx.model.chat(history, max_tokens=budget)
            calls.append(record)
            history.append(("ai", reply))
        html = extract_html(reply, ACCESSGURU_MARKERS)
        return html, reply, _status_for(reply, html, record)

    async def _score(self, html: str | None, item: BenchmarkItem, ctx: FixContext) -> int | None:
        if html is None:
            return None
        after = await ctx.scanner.scan_html(html)
        return evaluate_fix(ctx.before, after, item.rule_id).new_score

    async def fix(self, item: BenchmarkItem, ctx: FixContext) -> FixResult:
        calls: list[CallRecord] = []
        original_score = evaluate_fix(ctx.before, ctx.before, item.rule_id).new_score
        candidates = [(original_score, 0, item.html, "original")]
        reply, status, passes, note = "", "no_html", 0, ""
        try:
            for passes, reprompt in enumerate((False, True), start=1):
                html, reply, status = await self._conversation(item, ctx, reprompt, calls)
                score = await self._score(html, item, ctx)
                if score is not None:
                    candidates.append((score, passes, html, f"pass {passes}"))
                if score == 0:
                    break
        except RequestTooLarge as e:
            status, note = "too_large", str(e)

        # Lowest score wins; among equal scores, the most recent candidate.
        best_score, _, best_html, label = min(candidates, key=lambda c: (c[0], -c[1]))
        if label == "original":
            note = note or "no candidate improved on the original"
            if status == "ok":
                status = "no_improvement"
        return FixResult(html=best_html, status=status if label == "original" else "ok", calls=calls,
                         response=reply, passes=passes, note=note or f"kept {label}")


class AgentMethod:
    """Method C: the LangGraph agent from ``agent.py``, asked to fix one rule.

    The graph runs with ``targets=[the benchmark rule]`` and the harness's own
    scan passed in, so the agent and the harness start from the same picture.
    """
    name = "agent"
    uses_model = True
    max_attempts = 3
    _graph = None

    @classmethod
    def graph(cls):
        if cls._graph is None:
            from .agent import build_graph
            cls._graph = build_graph()            # no checkpointer: the harness saves results per item
        return cls._graph

    def agent_context(self, ctx: FixContext):
        from .agent import AgentContext
        return AgentContext(scanner=ctx.scanner, model=ctx.model, max_attempts=self.max_attempts, max_rules=1)

    async def fix(self, item: BenchmarkItem, ctx: FixContext) -> FixResult:
        from .agent import compact_scan, run_config

        state = await self.graph().ainvoke(
            {"html": item.html, "targets": [item.rule_id], "scan": compact_scan(ctx.before)},
            run_config(max_attempts=self.max_attempts, max_rules=1, run_name=f"{self.name}:{item.id}",
                       tags=["a11y-agent", "evaluation"], metadata={"item": item.id, "rule": item.rule_id}),
            context=self.agent_context(ctx),
        )
        calls = [CallRecord(**u) for u in state.get("usage", [])]
        outcome = next((o for o in state.get("outcomes", []) if o["rule_id"] == item.rule_id), {})
        kind = outcome.get("status", "not_fixed")
        trace = "\n".join(f"{s['node']}: {s['detail']}" for s in state.get("steps", []))
        status = {"fixed": "ok", "improved": "ok", "too_large": "too_large"}.get(kind, "no_improvement")
        html = state["current_html"] if kind in ("fixed", "improved") else item.html
        return FixResult(html=html, status=status, calls=calls, response=trace,
                         passes=outcome.get("attempts", 0), note=f"{kind} after {outcome.get('attempts', 0)} attempt(s)")


class AgentToolsMethod(AgentMethod):
    """Method C with tools (Phase 3): the same graph, connected to the MCP server.

    One server process serves the whole evaluation run (``async with`` opens
    the connection before the first item and closes it after the last). The
    agent's own scans go to the server's ``scan_html`` tool; the model may call
    ``explain_rule`` and ``contrast_fix``. The harness still scores every fix
    with its own, separate scanner.
    """
    name = "agent_tools"

    def __init__(self):
        self.server = None
        self._connection = None

    async def __aenter__(self):
        from .mcp_client import connect
        self._connection = connect()
        self.server = await self._connection.__aenter__()
        return self

    async def __aexit__(self, *exc):
        if self._connection is not None:
            await self._connection.__aexit__(*exc)
        self.server = self._connection = None

    def agent_context(self, ctx: FixContext):
        from .agent import AgentContext
        if self.server is None:
            raise RuntimeError("use 'async with' around agent_tools so the MCP server is running")
        return AgentContext(scanner=self.server.scanner, model=ctx.model, max_attempts=self.max_attempts,
                            max_rules=1, tools=self.server.model_tools())


METHODS: dict[str, type] = {m.name: m for m in (NoOp, ZeroShot, AccessGuru, AgentMethod, AgentToolsMethod)}


def get_method(name: str) -> Method:
    if name not in METHODS:
        raise ValueError(f"unknown method {name!r}; choose from {sorted(METHODS)}")
    return METHODS[name]()
