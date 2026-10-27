"""The repair agent: a LangGraph state machine that fixes accessibility violations
and proves every fix by scanning again.

The graph (``python -m a11y_agent.agent --graph`` prints it as Mermaid):

    START -> scan -> screen -> pick
    pick     --next rule-->      retrieve -> propose    or  --all done--> approve -> report -> END
    propose  --edits applied-->  guard      or  --retry--> propose   or  --give up--> give_up
    guard    --passed-->         verify     or  --retry--> propose   or  --give up--> give_up
    verify   --passed-->         accept     or  --retry--> propose   or  --give up--> give_up
    accept -> pick               give_up -> pick

What each part does:

* **State** (``AgentState``): everything the agent knows, as plain JSON-like
  values so LangGraph can save it after every step (a *checkpoint*). Some
  fields have a *reducer*: instead of being overwritten, a node's update is
  combined with the old value (``steps`` and ``outcomes`` keep growing;
  ``feedback`` grows during one rule and is cleared for the next).
* **Nodes** are plain async functions: they read the state and return only
  the fields they change.
* **Edges** decide what runs next. The *conditional* edges (after ``pick``,
  ``propose`` and ``verify``) are where the agent's loop lives: retry with the
  scanner's feedback, or stop after ``max_attempts``.
* **Runtime context** (``AgentContext``): the scanner and the model client are
  passed in per run, not stored in the state, because they are live objects
  that cannot (and should not) be saved in a checkpoint.

The model decides only *how* to fix a rule. *Whether* a fix worked is decided
by axe-core and the content check, never by the model.

Guardrails (Phase 5, ``guardrails.py``) are three of the nodes: ``screen``
looks for instructions aimed at the model before the page reaches it,
``guard`` rejects fixes that remove or hide content, add scripts or URLs, or
break the markup, and ``approve`` pauses for a person to approve the final
diff (``--approve``; LangGraph's ``interrupt``). Without guardrails in the
runtime context the three do nothing, so the earlier methods are unchanged.

Usage:

    python -m a11y_agent.agent examples/broken_page.html                 # fix every violation
    python -m a11y_agent.agent page.html --rules image-alt link-name     # only these rules
    python -m a11y_agent.agent page.html --thread my-page                # saved checkpoints: resume after a stop
    python -m a11y_agent.agent page.html --tools                         # as an MCP client, with model tools
    python -m a11y_agent.agent page.html --rag                           # ... plus WCAG guidance (Phase 4)
    python -m a11y_agent.agent page.html --tools --guard                 # with guardrails (Phase 5)
    python -m a11y_agent.agent page.html --tools --approve               # ... and a person approves the diff
    python -m a11y_agent.agent --graph                                   # print the graph (Mermaid)
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import difflib
import operator
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.runtime import Runtime
from langgraph.types import interrupt

from .config import settings
from .edits import (
    SUBMIT_TOOL,
    EditError,
    FixProposal,
    ProposalError,
    apply_edits,
    describe_edits,
    focus_html,
    parse_proposal,
    proposal_format,
    proposal_from_args,
    submit_tool_spec,
)
from .model_client import (
    BadReply,
    CallRecord,
    ModelClient,
    RequestTooLarge,
    estimate_tokens,
    lc_messages_tokens,
    messages_tokens,
    tools_tokens,
)
from .guardrails import check_fix, review_flags
from .prompts import agent_messages
from .scanner import ScanResult, Scanner, Violation, ViolationNode
from .scoring import impact_score
from .verification import judge_fix

MAX_ATTEMPTS = 3          # tries without progress per rule before giving up
MAX_TOTAL_ATTEMPTS = 10   # tries per rule in all, including ones that made progress
MAX_TOOL_ROUNDS = 3       # tool-calling rounds per attempt before the model must submit (Phase 3)
TOOL_RESULTS_RESERVE = 800    # tokens kept free for tool results when tools are offered
MAX_RULES = 25            # rules per run, a hard bound on cost
NODES_SHOWN = 10          # failing elements described to the model per attempt (more follow in later attempts)
OUTPUT_TOKENS = (1024, 3072)   # min and max reply budget (reasoning + JSON edits)
BUDGET_MARGIN = 0.9       # plan requests at 90% of the per-request limit; token counts are estimates


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def per_rule_log(current: list | None, update: list | None) -> list:
    """Reducer: append a node's entries; an update of ``None`` clears the list.

    ``pick`` sends ``None`` when it starts a new rule, so feedback and
    candidates from the previous rule never leak into the next one.
    """
    if update is None:
        return []
    return (current or []) + update


class AgentState(TypedDict, total=False):
    # Input
    html: str                     # the page as given
    targets: list[str]            # rules to fix; empty means every violated rule
    # Working state
    current_html: str             # the page with every accepted fix applied
    scan: dict                    # compact scan of current_html (see compact_scan)
    initial_scan: dict            # the scan of the page as given (the approve step can go back to it)
    initial_score: int
    queue: list[str]              # rules still to fix, most severe first
    done_rules: list[str]         # rules already handled (fixed or not)
    rule: str | None              # the rule being fixed now
    attempt: int                  # attempts made on the current rule
    stalls: int                   # of those, attempts that made no progress
    candidate: dict | None        # the latest proposed fix and, after verify, its result
    verdict: str                  # verify's decision: accept | retry | give_up
    stop_reason: str              # why the current rule cannot continue, e.g. "too_large"
    guidance: list[dict]          # WCAG passages retrieved for the current rule (Phase 4)
    screening: dict               # the input guardrail's result (Phase 5)
    approval: dict                # the approve step's decision and the flags it showed
    feedback: Annotated[list[dict], per_rule_log]     # what went wrong, shown to the model on retry
    candidates: Annotated[list[dict], per_rule_log]   # verified fixes for the current rule
    # Results (grow for the whole run)
    outcomes: Annotated[list[dict], operator.add]     # one entry per rule
    usage: Annotated[list[dict], operator.add]        # one entry per model call (tokens, seconds)
    steps: Annotated[list[dict], operator.add]        # the trace: one entry per node run
    guard_log: Annotated[list[dict], operator.add]    # every guardrail rejection and review flag (Phase 5)
    summary: dict


@dataclass
class AgentContext:
    """Live objects a run needs. Passed with ``context=``; never checkpointed.

    ``scanner`` is anything with an async ``scan_html(html) -> ScanResult``:
    the in-process ``Scanner``, or ``McpScanner``, which scans on the MCP
    server (Phase 3). ``tools`` are LangChain tools the model may call while
    proposing a fix; without them the model answers in one JSON reply (Phase 2).
    """
    scanner: Any
    model: ModelClient
    max_attempts: int = MAX_ATTEMPTS
    max_rules: int = MAX_RULES
    tools: list | None = None
    max_total_attempts: int = MAX_TOTAL_ATTEMPTS
    retriever: Any = None         # anything with async guidance(query, criteria, k) -> [passage]; None = no retrieval
    guard: Any = None             # guardrails.Guardrails; None = no guardrails (Phase 5)

    def out_of_tries(self, attempt: int, stalls: int) -> bool:
        """Stop after ``max_attempts`` tries without progress, or ``max_total_attempts`` in all.

        Attempts that fix some of many failing elements (55 images, say) do not
        use up the first budget, so a long list can be worked through in batches.
        """
        return stalls >= self.max_attempts or attempt >= self.max_total_attempts


# ---------------------------------------------------------------------------
# Scans as plain data (so they can live in checkpointed state)
# ---------------------------------------------------------------------------

def compact_scan(result: ScanResult, max_nodes: int = NODES_SHOWN) -> dict:
    return {
        "axe_version": result.axe_version,
        "violations": [
            {
                "rule_id": v.rule_id,
                "impact": v.impact,
                "help": v.help,
                "criteria": v.wcag_criteria,
                "node_count": len(v.nodes),
                "nodes": [{"html": n.html[:600], "target": n.target, "summary": n.failure_summary[:800]}
                          for n in v.nodes[:max_nodes]],
            }
            for v in result.violations
        ],
    }


def to_scan_result(scan: dict) -> ScanResult:
    """Rebuild a ScanResult from a compact scan, for the scoring functions."""
    return ScanResult(
        violations=[Violation(rule_id=v["rule_id"], impact=v["impact"], help=v["help"], help_url="", tags=[],
                              nodes=[ViolationNode(target=n["target"], html=n["html"], failure_summary=n["summary"])
                                     for n in v["nodes"]])
                    for v in scan["violations"]],
        axe_version=scan["axe_version"],
    )


def find_violation(scan: dict, rule_id: str) -> dict | None:
    return next((v for v in scan["violations"] if v["rule_id"] == rule_id), None)


def scan_score(scan: dict) -> int:
    return sum(impact_score(v["impact"]) for v in scan["violations"])


def _by_severity(scan: dict) -> list[str]:
    return [v["rule_id"] for v in sorted(scan["violations"], key=lambda v: (-impact_score(v["impact"]), v["rule_id"]))]


def _step(node: str, detail: str, **extra: Any) -> list[dict]:
    return [{"node": node, "detail": detail, **extra}]


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

async def scan(state: AgentState, runtime: Runtime[AgentContext]) -> dict:
    """Scan the page once and queue the rules to fix, most severe first."""
    if state.get("scan"):                         # a caller that already scanned can pass the result in
        current = state["scan"]
        detail = "reused the caller's scan"
    else:
        current = compact_scan(await runtime.context.scanner.scan_html(state["html"]))
        detail = "scanned the page"
    present = _by_severity(current)
    wanted = state.get("targets") or present
    queue = [r for r in present if r in wanted]
    outcomes = [{"rule_id": r, "status": "not_found", "attempts": 0} for r in wanted if r not in present]
    limit = runtime.context.max_rules
    outcomes += [{"rule_id": r, "status": "skipped_limit", "attempts": 0} for r in queue[limit:]]
    queue = queue[:limit]
    score = scan_score(current)
    return {
        "current_html": state["html"], "scan": current, "initial_scan": current, "initial_score": score,
        "queue": queue,
        "done_rules": [], "outcomes": outcomes,
        "steps": _step("scan", f"{detail}: {len(present)} rule(s) violated, score {score}; "
                               f"to fix: {', '.join(queue) or 'nothing'}"),
    }


async def screen(state: AgentState, runtime: Runtime[AgentContext]) -> dict:
    """Input guardrail: look for instructions aimed at the model before any of the page reaches it.

    If the page looks like an attack, no rule is sent to the model, unless a
    person (``--approve``) decides to go ahead. Without guardrails: nothing.
    """
    guard = runtime.context.guard
    if guard is None:
        return {}
    result = await guard.screen(state["html"])
    update: dict = {"screening": result.as_dict(), "steps": _step("screen", result.describe())}
    if not result.suspicious:
        return update
    if guard.approval == "ask":
        answer = interrupt({"kind": "screen", "findings": result.as_dict()["findings"],
                            "question": "This page contains text that looks like instructions to an AI model. "
                                        "Send it to the model anyway?"})
        if _decision(answer) in ("continue", "approve", "yes"):
            update["steps"] += _step("screen", "a person allowed the page to be sent to the model")
            return update
    blocked = [{"rule_id": r, "status": "blocked", "attempts": 0, "reason": "prompt_injection"} for r in state["queue"]]
    update.update(queue=[], outcomes=blocked,
                  guard_log=[{"check": "prompt_injection", "kind": "blocked", "detail": result.describe()}])
    update["steps"] += _step("screen", f"stopped: {len(blocked)} rule(s) not sent to the model")
    return update


def _decision(answer) -> str:
    """A person's answer to an interrupt: a dict with "decision", or a plain string."""
    if isinstance(answer, dict):
        return str(answer.get("decision", "")).lower()
    return str(answer or "").strip().lower()


async def pick(state: AgentState) -> dict:
    """Take the next rule from the queue, skipping rules an earlier fix already solved."""
    present = {v["rule_id"] for v in state["scan"]["violations"]}
    queue = list(state["queue"])
    solved = []
    while queue and queue[0] not in present:
        solved.append(queue.pop(0))
    outcomes = [{"rule_id": r, "status": "fixed_by_earlier_fix", "attempts": 0} for r in solved]
    if not queue:
        return {"queue": [], "rule": None, "outcomes": outcomes,
                "steps": _step("pick", "nothing left to fix")}
    rule = queue.pop(0)
    return {
        "queue": queue, "rule": rule, "attempt": 0, "stalls": 0, "candidate": None, "verdict": "", "stop_reason": "",
        "guidance": [],
        "feedback": None, "candidates": None,            # None = clear (see per_rule_log)
        "outcomes": outcomes,
        "steps": _step("pick", f"next rule: {rule}", rule=rule),
    }


GUIDANCE_PASSAGES = 3


def retrieval_query(violation: dict) -> str:
    """What to look up for a violation: the rule's help text plus axe-core's first failure message."""
    first = " ".join((violation["nodes"][0].get("summary") or "").split())[:300] if violation["nodes"] else ""
    return f"{violation.get('help', violation['rule_id'])}. {first}".strip()


async def retrieve(state: AgentState, runtime: Runtime[AgentContext]) -> dict:
    """Look up W3C guidance for the current rule, once per rule (Phase 4).

    The search is limited to the rule's WCAG success criteria (when it has
    any) and to technique, failure and understanding pages. Without a
    retriever in the runtime context this node does nothing.
    """
    retriever = runtime.context.retriever
    if retriever is None:
        return {}
    violation = find_violation(state["scan"], state["rule"])
    passages = await retriever.guidance(retrieval_query(violation), violation.get("criteria") or None,
                                        GUIDANCE_PASSAGES)
    found = "; ".join((p.get("technique_id") + " " if p.get("technique_id") else "") + p["title"][:70]
                      for p in passages) or "nothing"
    return {"guidance": passages, "steps": _step("retrieve", f"{len(passages)} passage(s): {found}", rule=state["rule"])}


def build_request(state: AgentState, violation: dict, model: ModelClient,
                  tool_specs: list[dict] | None = None) -> tuple[list, int, bool]:
    """The messages, the reply budget, and whether the HTML had to be excerpted.

    The free tier limits each request (prompt + tool definitions + reply
    budget) to about 8,000 tokens. If the whole page does not fit next to the
    prompt and a minimum reply budget, only the parts around the failing
    elements are shown. With tools, room is also kept for a few tool results.
    """
    html, feedback, guidance = state["current_html"], state.get("feedback") or [], state.get("guidance") or []
    tools = bool(tool_specs)
    low, high = OUTPUT_TOKENS[0], reply_budget(violation, model)
    limit = int(model.max_request_tokens * BUDGET_MARGIN) if model.max_request_tokens else 0
    shown, excerpted = html, False
    if limit:
        reserve = tools_tokens(tool_specs or []) + (TOOL_RESULTS_RESERVE if tools else 0)
        overhead = messages_tokens(agent_messages(violation, "", feedback=feedback, excerpted=True, tools=tools,
                                                  guidance=guidance))
        html_tokens = max(limit - overhead - reserve - low, 300)
        if estimate_tokens(html) > html_tokens:
            shown, excerpted = focus_html(html, [n["html"] for n in violation["nodes"]], html_tokens * 4)
    messages = agent_messages(violation, shown, feedback=feedback, excerpted=excerpted, tools=tools,
                              guidance=guidance)
    used = messages_tokens(messages) + tools_tokens(tool_specs or [])
    max_tokens = high if not limit else max(low, min(high, limit - used))
    return messages, max_tokens, excerpted


def reply_budget(violation: dict, model: ModelClient) -> int:
    """Largest reply to allow: enough for one edit per failing element, within the provider's output limit.

    A fixed 3,072 tokens cut off replies that had to fix 55 images (dev item
    accessguru-175). Long lists are now fixed in batches (NODES_SHOWN per
    attempt); about 250 tokens per element covers a find, a replace and some
    reasoning.
    """
    wanted = 1024 + 250 * min(violation.get("node_count", 1), 2 * NODES_SHOWN)
    return max(OUTPUT_TOKENS[1], min(model.max_output_tokens, wanted))


def _short(text: str, limit: int = 160) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def ask_with_tools(messages: list[tuple[str, str]], ctx: AgentContext, tool_specs: list[dict],
                         budget: int = OUTPUT_TOKENS[1]) -> tuple[FixProposal, list[CallRecord], list[str]]:
    """Let the model call tools, then return its ``submit_fix`` proposal.

    A small loop inside one attempt: send the conversation; if the model asks
    for tools, run them (on the MCP server), append the results, and send it
    again. After ``MAX_TOOL_ROUNDS`` rounds, or when the next round would not
    fit the request limit, only ``submit_fix`` is offered and the model must
    call it. Tool errors go back to the model as error results.

    Returns (proposal, model calls, notes for the trace). Raises
    ProposalError for an unusable final answer.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    tools = {t.name: t for t in ctx.tools or []}
    submit = submit_tool_spec()
    conversation: list = [SystemMessage(content=text) if role == "system" else HumanMessage(content=text)
                          for role, text in messages]
    records: list[CallRecord] = []
    notes: list[str] = []
    limit = int(ctx.model.max_request_tokens * BUDGET_MARGIN) if ctx.model.max_request_tokens else 0
    try:
        return await _tool_rounds(conversation, ctx, tools, tool_specs, submit, records, notes, limit, budget)
    except (ProposalError, BadReply) as e:
        e.records, e.notes = records, notes                   # keep the cost and trace of a failed attempt
        raise


async def _tool_rounds(conversation: list, ctx: AgentContext, tools: dict, tool_specs: list[dict], submit: dict,
                       records: list[CallRecord], notes: list[str], limit: int, budget: int):
    """The rounds of ``ask_with_tools``; ``records`` and ``notes`` are filled in as it goes."""
    from langchain_core.messages import ToolMessage

    low, high = OUTPUT_TOKENS[0], budget
    for round_number in range(MAX_TOOL_ROUNDS + 1):
        offered, choice = tool_specs + [submit], "required"
        used = lc_messages_tokens(conversation) + tools_tokens(offered)
        if round_number == MAX_TOOL_ROUNDS or (limit and used + low + TOOL_RESULTS_RESERVE // 2 > limit):
            offered, choice = [submit], SUBMIT_TOOL           # last round: answer now
            used = lc_messages_tokens(conversation) + tools_tokens(offered)
        max_tokens = high if not limit else max(low, min(high, limit - used))
        reply, record = await ctx.model.chat_with_tools(conversation, tools=offered, tool_choice=choice,
                                                        max_tokens=max_tokens)
        records.append(record)
        calls = reply.tool_calls or []
        final = next((c for c in calls if c["name"] == SUBMIT_TOOL), None)
        if final is not None:
            return proposal_from_args(final["args"]), records, notes
        if not calls:                                         # answered in text instead of calling submit_fix
            if record.finish_reason == "length":
                raise ProposalError("Your reply was cut off before it finished. Use fewer, shorter edits.")
            return parse_proposal(_content_text(reply.content)), records, notes
        if choice == SUBMIT_TOOL:
            break                                             # told to submit, but called something else
        conversation.append(reply)
        for call in calls:
            tool = tools.get(call["name"])
            if tool is None:
                result = ToolMessage(content=f"Unknown tool {call['name']!r}. Available: {', '.join(tools)}, submit_fix.",
                                     tool_call_id=call["id"], status="error")
            else:
                result = await tool.ainvoke(call)
                result.content = _content_text(result.content)    # plain text for every provider
            conversation.append(result)
            notes.append(f"{call['name']}({_short(call.get('args', {}), 100)}) -> {_short(result.content)}")
    raise ProposalError("You did not call submit_fix. Finish by calling submit_fix with your edits.")


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)


async def propose(state: AgentState, runtime: Runtime[AgentContext]) -> dict:
    """Ask the model for edits and apply them to the current HTML.

    If the reply cannot be used (not valid JSON, an edit that matches nothing,
    no change at all), the problem is recorded as feedback and the next
    attempt sees it, exactly like a fix that failed verification.
    """
    rule, attempt = state["rule"], state["attempt"] + 1
    ctx = runtime.context
    violation = find_violation(state["scan"], rule)
    tool_specs = [convert_to_openai_tool(t) for t in ctx.tools] if ctx.tools else None
    messages, max_tokens, excerpted = build_request(state, violation, ctx.model, tool_specs)
    stalls = state.get("stalls", 0) + 1          # if this attempt fails, it made no progress
    out_of_tries = ctx.out_of_tries(attempt, stalls)

    def failed(problem: str, edits_text: str = "", usage: list | None = None, detail: str = "") -> dict:
        problem += repeated(state.get("feedback") or [], edits_text)
        return {"attempt": attempt, "stalls": stalls, "candidate": None,
                "verdict": "give_up" if out_of_tries else "retry", "usage": usage or [], "feedback": [{"attempt": attempt, "edits": edits_text, "problem": problem}],
                "steps": _step("propose", detail or problem, rule=rule, attempt=attempt)}

    note = " (HTML excerpted)" if excerpted else ""
    try:
        if tool_specs:
            proposal, records, tool_notes = await ask_with_tools(messages, ctx, tool_specs,
                                                                 reply_budget(violation, ctx.model))
        else:
            reply, record = await ctx.model.chat(messages, max_tokens=max_tokens, response_format=proposal_format())
            records, tool_notes = [record], []
            try:
                proposal = parse_proposal(reply)
            except ProposalError as error:
                failure = error
                if record.finish_reason == "length":
                    failure = ProposalError("Your reply was cut off before it finished. Use fewer, shorter edits.")
                failure.records = records             # the call still cost tokens: count them
                raise failure from None
    except RequestTooLarge as e:
        return {"attempt": attempt, "candidate": None, "verdict": "give_up", "stop_reason": "too_large",
                "steps": _step("propose", f"request too large to send: {e}", rule=rule, attempt=attempt)}
    except (BadReply, ProposalError) as e:
        partial = [asdict(r) for r in getattr(e, "records", None) or []]
        tools_used = "; tools: " + " | ".join(e.notes) if getattr(e, "notes", None) else ""
        if isinstance(e, BadReply):
            return failed("The reply was rejected because it did not match the required format.", usage=partial,
                          detail=f"provider rejected the reply{tools_used}: {e}")
        return failed(str(e), usage=partial, detail=f"unusable reply{note}{tools_used}: {e}")

    usage = [asdict(r) for r in records]
    if tool_notes:
        note += "; tools: " + " | ".join(tool_notes)
    edits_text = describe_edits(proposal.edits)
    try:
        new_html = apply_edits(state["current_html"], proposal.edits)
    except EditError as e:
        return failed(str(e), edits_text, usage, detail=f"edits could not be applied{note}: {e}")
    if new_html == state["current_html"]:
        return failed("Your edits did not change the HTML.", edits_text, usage)

    candidate = {"html": new_html, "explanation": proposal.explanation,
                 "edits": [e.model_dump() for e in proposal.edits], "attempt": attempt}
    return {"attempt": attempt, "candidate": candidate, "verdict": "", "usage": usage,
            "steps": _step("propose", f"{len(proposal.edits)} edit(s){note}: {edits_text}", rule=rule,
                           attempt=attempt)}


async def guard(state: AgentState, runtime: Runtime[AgentContext]) -> dict:
    """Output guardrail: check the proposed fix before the scanner sees it.

    A fix that removes or hides content, adds scripts or URLs, or breaks the
    markup is rejected; the reason goes back to the model like scanner
    feedback, and the attempt counts. Without guardrails: nothing.
    """
    if runtime.context.guard is None:
        return {}
    rule, attempt, candidate = state["rule"], state["attempt"], state["candidate"]
    problems = check_fix(state["current_html"], candidate["html"])
    if not problems:
        return {}
    stalls = state.get("stalls", 0) + 1
    verdict = "give_up" if runtime.context.out_of_tries(attempt, stalls) else "retry"
    edits_text = describe_edits(candidate["edits"])
    message = " ".join(p.message for p in problems) + repeated(state.get("feedback") or [], edits_text)
    codes = ", ".join(p.code for p in problems)
    return {"candidate": None, "verdict": verdict, "stalls": stalls,
            "feedback": [{"attempt": attempt, "edits": edits_text, "problem": message}],
            "guard_log": [{"check": p.code, "kind": "rejected", "rule": rule, "attempt": attempt, "detail": p.evidence}
                          for p in problems],
            "steps": _step("guard", f"rejected ({codes}): {'; '.join(p.evidence for p in problems)} -> {verdict}",
                           rule=rule, attempt=attempt)}


def repeated(feedback: list[dict], edits_text: str) -> str:
    """A note for the model if it proposed exactly the same edits as an earlier attempt."""
    same = [f["attempt"] for f in feedback if edits_text and f.get("edits") == edits_text]
    return (f" This is the same change as attempt {same[0]}, which did not work either: try a different approach."
            if same else "")


async def verify(state: AgentState, runtime: Runtime[AgentContext]) -> dict:
    """Scan the candidate and decide: accept, retry with feedback, or give up.

    A fix passes only if the target rule is gone, no other rule newly fails,
    and the page keeps at least 90% of its words and elements
    (``verification.judge_fix``, shared with the MCP tool ``verify_fix``).
    """
    rule, candidate = state["rule"], state["candidate"]
    after = await runtime.context.scanner.scan_html(candidate["html"])
    judgement = judge_fix(to_scan_result(state["scan"]), after, rule, state["current_html"], candidate["html"])

    # Progress: fewer elements fail the rule, and nothing else got worse. Keep that
    # change and let the next attempt continue from it, instead of starting over.
    before_count = find_violation(state["scan"], rule)["node_count"]
    remaining = len(after.get(rule).nodes) if after.get(rule) else 0
    progress = (not judgement.passed and not judgement.target_fixed and remaining < before_count
                and not judgement.introduced and judgement.content_ok)

    stalls = state.get("stalls", 0) + (0 if judgement.passed or progress else 1)
    if judgement.passed:
        verdict = "accept"
    elif runtime.context.out_of_tries(state["attempt"], stalls):
        verdict = "give_up"
    else:
        verdict = "retry"
    result = {**candidate, "scan": compact_scan(after), "old_score": judgement.old_score,
              "new_score": judgement.new_score, "target_fixed": judgement.target_fixed,
              "introduced": judgement.introduced, "text_kept": judgement.text_kept,
              "elements_kept": judgement.elements_kept, "content_ok": judgement.content_ok}
    edits_text = describe_edits(candidate["edits"])
    problem = " ".join(judgement.problems)
    if progress:
        problem = (f"Progress: your change fixed {before_count - remaining} of the {before_count} failing elements "
                   "without breaking anything, so it has been kept (the HTML below includes it). " + problem)
    elif problem:
        problem += repeated(state.get("feedback") or [], edits_text)
    feedback = [{"attempt": state["attempt"], "edits": edits_text, "problem": problem}] if problem else []
    update = {"candidate": result, "candidates": [result], "verdict": verdict, "feedback": feedback, "stalls": stalls,
              "steps": _step("verify", f"{problem or 'passed'} -> {verdict}", rule=rule, attempt=state["attempt"],
                             score=f"{judgement.old_score}->{judgement.new_score}")}
    if progress:
        update.update(current_html=candidate["html"], scan=result["scan"])
    return update


async def accept(state: AgentState) -> dict:
    """Keep the verified fix: it becomes the HTML the next rule starts from."""
    c = state["candidate"]
    outcome = {"rule_id": state["rule"], "status": "fixed", "attempts": state["attempt"],
               "old_score": c["old_score"], "new_score": 0, "explanation": c["explanation"], "edits": c["edits"]}
    return {"current_html": c["html"], "scan": c["scan"], "done_rules": state["done_rules"] + [state["rule"]],
            "outcomes": [outcome],
            "steps": _step("accept", f"fixed {state['rule']} in {state['attempt']} attempt(s)", rule=state["rule"])}


async def give_up(state: AgentState, runtime: Runtime[AgentContext]) -> dict:
    """Out of attempts. Keep the best fix only if it lowered the score without removing content.

    "Lowered the score" means the target rule is gone but a less severe rule
    now fails, which the AccessGuru metric counts as progress. In whole-page
    mode that newly failing rule is queued, so the agent tries to fix it too.
    """
    rule = state["rule"]
    done = state["done_rules"] + [rule]
    better = [c for c in state.get("candidates") or [] if c["content_ok"] and c["new_score"] < c["old_score"]]
    if not better:
        status = "too_large" if state.get("stop_reason") == "too_large" else "not_fixed"
        old = impact_score(find_violation(state["scan"], rule)["impact"])
        last = (state.get("feedback") or [{}])[-1].get("problem", "")
        outcome = {"rule_id": rule, "status": status, "attempts": state["attempt"], "old_score": old,
                   "new_score": old, "last_problem": last}
        return {"done_rules": done, "outcomes": [outcome],
                "steps": _step("give_up", f"left {rule} unfixed ({status})", rule=rule)}

    best = min(better, key=lambda c: (c["new_score"], -c["attempt"]))
    queue = list(state["queue"])
    if not state.get("targets"):
        room = runtime.context.max_rules - len(done) - len(queue)
        queue += [r for r in best["introduced"] if r not in done and r not in queue][: max(room, 0)]
    outcome = {"rule_id": rule, "status": "improved", "attempts": state["attempt"], "old_score": best["old_score"],
               "new_score": best["new_score"], "explanation": best["explanation"], "edits": best["edits"],
               "introduced": best["introduced"]}
    detail = (f"kept attempt {best['attempt']}: {rule} is fixed but {', '.join(best['introduced'])} now fails "
              f"(score {best['old_score']}->{best['new_score']})")
    return {"current_html": best["html"], "scan": best["scan"], "done_rules": done, "queue": queue,
            "outcomes": [outcome], "steps": _step("give_up", detail, rule=rule)}


async def approve(state: AgentState, runtime: Runtime[AgentContext]) -> dict:
    """Action guardrail: the final diff is approved before it counts.

    First an invariant: a page that scores worse than the one given is never
    handed back. Then the review flags (text the model wrote, colours it
    changed, ...). With ``approval="ask"`` the graph pauses here (``interrupt``)
    until a person approves, edits or rejects the change; with "auto" (the
    evaluation) it is approved and the flags are recorded. Without guardrails: nothing.
    """
    guard_rails = runtime.context.guard
    if guard_rails is None:
        return {}
    original, final, update = state["html"], state["current_html"], {}
    log: list[dict] = []
    if scan_score(state["scan"]) > state["initial_score"]:
        log.append({"check": "score_increase", "kind": "rejected",
                    "detail": f"score {state['initial_score']} -> {scan_score(state['scan'])}: kept the original"})
        update = {"current_html": original, "scan": state["initial_scan"]}
        final = original
    if final == original:
        return {**update, "guard_log": log, "approval": {"decision": "nothing to approve", "flags": []},
                "steps": _step("approve", "nothing changed, nothing to approve")}

    flags = [asdict(f) for f in review_flags(original, final)]
    log += [{"check": f["code"], "kind": "flagged", "detail": f["message"]} for f in flags]
    codes = ", ".join(f["code"] for f in flags) or "nothing"
    if guard_rails.approval != "ask":
        return {"guard_log": log, "approval": {"decision": "auto", "flags": flags},
                "steps": _step("approve", f"approved automatically; for a person to check: {codes}")}

    answer = interrupt({"kind": "approve", "score": f"{state['initial_score']} -> {scan_score(state['scan'])}",
                        "flags": flags, "diff": unified_diff(original, final, limit=200), "html": final})
    decision = _decision(answer)
    if decision == "reject":
        return {"current_html": original, "scan": state["initial_scan"], "guard_log": log,
                "approval": {"decision": "rejected", "flags": flags},
                "steps": _step("approve", "a person rejected the change: the original page is kept")}
    if decision == "edit" and isinstance(answer, dict) and answer.get("html"):
        edited = answer["html"]
        scanned = compact_scan(await runtime.context.scanner.scan_html(edited))
        return {"current_html": edited, "scan": scanned, "guard_log": log,
                "approval": {"decision": "edited", "flags": flags},
                "steps": _step("approve", f"a person edited the change (score now {scan_score(scanned)})")}
    return {"guard_log": log, "approval": {"decision": "approved", "flags": flags},
            "steps": _step("approve", f"a person approved the change (flags shown: {codes})")}


async def report(state: AgentState) -> dict:
    """Summarise the run: what was fixed, what was not, the score before and after."""
    final = scan_score(state["scan"])
    by_status: dict[str, list[str]] = {}
    for o in state.get("outcomes", []):
        by_status.setdefault(o["status"], []).append(o["rule_id"])
    summary = {"initial_score": state["initial_score"], "final_score": final, "by_status": by_status,
               "remaining_rules": _by_severity(state["scan"]),
               "model_calls": len(state.get("usage", [])),
               "tokens": sum(u["input_tokens"] + u["output_tokens"] for u in state.get("usage", []))}
    if state.get("guard_log") or state.get("screening") or state.get("approval"):
        log = state.get("guard_log") or []
        summary["guardrails"] = {
            "rejected": dict(Counter(e["check"] for e in log if e["kind"] == "rejected")),
            "flagged": dict(Counter(e["check"] for e in log if e["kind"] == "flagged")),
            "blocked": any(e["kind"] == "blocked" for e in log),
            "approval": (state.get("approval") or {}).get("decision", ""),
        }
    fixed = len(by_status.get("fixed", [])) + len(by_status.get("fixed_by_earlier_fix", []))
    return {"summary": summary,
            "steps": _step("report", f"score {state['initial_score']} -> {final}; {fixed} rule(s) fixed")}


# ---------------------------------------------------------------------------
# Edges
# ---------------------------------------------------------------------------

# Each routing function returns a label; the maps in build_graph turn labels into
# node names. The labels also appear on the arrows of the Mermaid diagram.

def after_pick(state: AgentState) -> str:
    return "next rule" if state.get("rule") else "all done"


def after_propose(state: AgentState) -> str:
    if state.get("candidate"):
        return "edits applied"
    return "give up" if state["verdict"] == "give_up" else "retry"


def after_guard(state: AgentState) -> str:
    if state.get("candidate"):
        return "passed"
    return "give up" if state["verdict"] == "give_up" else "retry"


def after_verify(state: AgentState) -> str:
    return {"accept": "passed", "retry": "retry"}.get(state["verdict"], "give up")


def build_graph(checkpointer=None):
    """Assemble and compile the graph. Pass a checkpointer to save state after every step."""
    graph = StateGraph(AgentState, context_schema=AgentContext)
    for name, fn in [("scan", scan), ("screen", screen), ("pick", pick), ("retrieve", retrieve), ("propose", propose),
                     ("guard", guard), ("verify", verify), ("accept", accept), ("give_up", give_up),
                     ("approve", approve), ("report", report)]:
        graph.add_node(name, fn)
    graph.add_edge(START, "scan")
    graph.add_edge("scan", "screen")
    graph.add_edge("screen", "pick")
    graph.add_conditional_edges("pick", after_pick, {"next rule": "retrieve", "all done": "approve"})
    graph.add_edge("retrieve", "propose")
    graph.add_conditional_edges("propose", after_propose,
                                {"edits applied": "guard", "retry": "propose", "give up": "give_up"})
    graph.add_conditional_edges("guard", after_guard,
                                {"passed": "verify", "retry": "propose", "give up": "give_up"})
    graph.add_conditional_edges("verify", after_verify,
                                {"passed": "accept", "retry": "propose", "give up": "give_up"})
    graph.add_edge("accept", "pick")
    graph.add_edge("give_up", "pick")
    graph.add_edge("approve", "report")
    graph.add_edge("report", END)
    return graph.compile(checkpointer=checkpointer, name="accessibility-repair-agent")


def recursion_limit(max_attempts: int = MAX_ATTEMPTS, max_rules: int = MAX_RULES,
                    max_total_attempts: int = MAX_TOTAL_ATTEMPTS) -> int:
    """LangGraph stops any run after this many steps: a second safety net behind the attempt limits.

    Per rule: pick, retrieve, up to max_total_attempts x (propose + guard + verify), accept or give_up;
    plus scan, screen, the last pick, approve and report.
    """
    return 6 + max_rules * (3 * max(max_attempts, max_total_attempts) + 3)


def mermaid() -> str:
    """The graph as a Mermaid diagram (GitHub renders these in Markdown)."""
    return build_graph().get_graph().draw_mermaid()


# ---------------------------------------------------------------------------
# Running it
# ---------------------------------------------------------------------------

def run_config(*, thread_id: str | None = None, max_attempts: int = MAX_ATTEMPTS, max_rules: int = MAX_RULES,
               run_name: str = "repair", tags: list[str] | None = None, metadata: dict | None = None) -> dict:
    """LangGraph run configuration. ``run_name``, ``tags`` and ``metadata`` label the trace in LangSmith."""
    config: dict = {"recursion_limit": recursion_limit(max_attempts, max_rules), "run_name": run_name,
                    "tags": tags or ["a11y-agent"], "metadata": metadata or {}}
    if thread_id:
        config["configurable"] = {"thread_id": thread_id}
    return config


def _print_step(step: dict, width: int = 400) -> None:
    where = f"[{step['rule']}" + (f" #{step['attempt']}" if step.get("attempt") else "") + "] " if step.get("rule") else ""
    detail = step["detail"] if len(step["detail"]) <= width else step["detail"][: width - 1] + "…"
    print(f"  {step['node']:8} {where}{detail}", flush=True)


def unified_diff(before: str, after: str, limit: int = 80) -> str:
    lines = list(difflib.unified_diff(before.splitlines(), after.splitlines(), "before", "after", lineterm="", n=1))
    if len(lines) > limit:
        lines = lines[:limit] + [f"... {len(lines) - limit} more diff lines"]
    return "\n".join(lines)


def ask_person(pending: dict, review_path: Path) -> dict:
    """Show what the graph paused for, and read the person's decision from the terminal.

    Without a terminal to ask (input redirected), the safe answer is given:
    stop, or keep the original page.
    """
    if pending.get("kind") == "screen":
        print("\n" + pending["question"])
        for f in pending["findings"][:5]:
            print(f"  {f['source']}: {f['reason']} in {f['where']}: \"{f['excerpt']}\"")
        if not sys.stdin.isatty():
            print("(no terminal to ask: stopping)")
            return {"decision": "stop"}
        return {"decision": "continue" if input("Send it anyway? [y/N] ").strip().lower() in ("y", "yes") else "stop"}

    print(f"\nReview the change before it is kept (violation score {pending['score']}):")
    if pending["flags"]:
        print("Things to check:")
        for f in pending["flags"]:
            print(f"  - {f['message']}")
    print("\n" + pending["diff"])
    if not sys.stdin.isatty():
        print("(no terminal to ask: keeping the original page)")
        return {"decision": "reject"}
    while True:
        choice = input("\n[a]pprove, [r]eject, or [e]dit it yourself? ").strip().lower()[:1]
        if choice == "a":
            return {"decision": "approve"}
        if choice == "r":
            return {"decision": "reject"}
        if choice == "e":
            review_path.write_text(pending["html"], encoding="utf-8")
            input(f"Edit {review_path}, save it, then press Enter here... ")
            return {"decision": "edit", "html": review_path.read_text(encoding="utf-8")}


async def drive(graph, graph_input, config, context, *, on_interrupt) -> None:
    """Run the graph, printing each step; when it pauses for a person, ask and resume.

    ``interrupt()`` inside a node stops the run and saves it in the checkpoint;
    running the graph again with ``Command(resume=answer)`` continues that node
    with the answer. ``on_interrupt(payload) -> answer``.
    """
    from langgraph.types import Command
    while True:
        pending = None
        async for update in graph.astream(graph_input, config, context=context, stream_mode="updates"):
            if "__interrupt__" in update:
                pending = update["__interrupt__"][0].value
                continue
            for node_update in update.values():
                for step in (node_update or {}).get("steps", []):
                    _print_step(step)
        if pending is None:
            return
        graph_input = Command(resume=await asyncio.to_thread(on_interrupt, pending))


async def _run_cli(args) -> int:
    from .model_client import ResponseCache, StopRun

    path = Path(args.file)
    html = path.read_text(encoding="utf-8")
    out = Path(args.out) if args.out else path.with_name(path.stem + ".fixed.html")
    cache = ResponseCache(settings.runs_dir / "cache.sqlite")
    model = ModelClient(args.model, cache=cache)
    guardrails = None
    if args.guard or args.approve:
        from .guardrails import Guardrails, PromptGuard
        prompt_guard = PromptGuard.from_settings(cache)
        guardrails = Guardrails(prompt_guard=prompt_guard, approval="ask" if args.approve else "auto")
        print("Guardrails on: input screen (" + (f"rules + {prompt_guard.spec}" if prompt_guard else "rules only")
              + "), output checks, " + ("a person approves the result" if args.approve else "automatic approval"))
    config = run_config(thread_id=args.thread or "cli", max_attempts=args.max_attempts,
                        run_name=f"repair:{path.name}", metadata={"file": str(path)})

    if args.thread:
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
        settings.runs_dir.mkdir(parents=True, exist_ok=True)
        saver_cm = AsyncSqliteSaver.from_conn_string(str(settings.runs_dir / "checkpoints.sqlite"))
    else:
        from langgraph.checkpoint.memory import InMemorySaver

        @contextlib.asynccontextmanager
        async def _memory():
            yield InMemorySaver()
        saver_cm = _memory()

    async with contextlib.AsyncExitStack() as stack:
        saver = await stack.enter_async_context(saver_cm)
        retriever = None
        if args.tools or args.rag:                # Phase 3: scans and tools come from the MCP server
            from .mcp_client import MODEL_TOOLS, RAG_TOOLS, connect
            server = await stack.enter_async_context(connect())
            scanner, tools = server.scanner, server.model_tools(RAG_TOOLS if args.rag else MODEL_TOOLS)
            retriever = server.retriever if args.rag else None
            print(f"MCP server started; the model may call: {', '.join(t.name for t in tools)}")
        else:
            scanner, tools = await stack.enter_async_context(Scanner()), None
        graph = build_graph(checkpointer=saver)
        context = AgentContext(scanner=scanner, model=model, max_attempts=args.max_attempts, tools=tools,
                               retriever=retriever, guard=guardrails)
        snapshot = await graph.aget_state(config)
        review_path = out.with_name(out.stem + ".review.html")
        if args.thread and snapshot.next:
            print(f"Resuming thread '{args.thread}' (it stopped before: {', '.join(snapshot.next)})")
            graph_input = None
            if snapshot.interrupts:               # it was waiting for a person: ask again
                from langgraph.types import Command
                graph_input = Command(resume=ask_person(snapshot.interrupts[0].value, review_path))
        else:
            if snapshot.values:                   # a finished run on this thread: start it over
                await saver.adelete_thread(config["configurable"]["thread_id"])
            graph_input = {"html": html, "targets": args.rules or []}
        print(f"Repairing {path} with {model.spec} (up to {args.max_attempts} attempts per rule)")
        try:
            await drive(graph, graph_input, config, context,
                        on_interrupt=lambda pending: ask_person(pending, review_path))
        except StopRun as e:
            print(f"\nStopped: {e}")
            if args.thread:
                print(f"Progress is saved. Run the same command (with --thread {args.thread}) later to continue.")
            else:
                print("Add --thread NAME next time to save progress after every step.")
            return 2

        state = (await graph.aget_state(config)).values
        if args.history:
            print("\nCheckpoints saved by LangGraph (newest first):")
            async for snap in graph.aget_state_history(config):
                v = snap.values
                print(f"  step {snap.metadata.get('step', '?'):>3}  next={','.join(snap.next) or 'END':10} "
                      f"rule={v.get('rule') or '-'} attempt={v.get('attempt', 0)}")

    s = state["summary"]
    out.write_text(state["current_html"], encoding="utf-8")
    print(f"\nViolation score {s['initial_score']} -> {s['final_score']} "
          f"({s['model_calls']} model calls, about {s['tokens']} tokens)")
    for status, rules in s["by_status"].items():
        print(f"  {status:22} {', '.join(rules)}")
    if s.get("guardrails"):
        g = s["guardrails"]
        rejected = ", ".join(f"{k} x{v}" for k, v in g["rejected"].items()) or "none"
        print(f"  {'guardrail rejections':22} {rejected}" + ("; page blocked at the input screen" if g["blocked"] else ""))
        if g["approval"]:
            print(f"  {'approval':22} {g['approval']}")
    if s["remaining_rules"]:
        print(f"  {'still failing':22} {', '.join(s['remaining_rules'])}")
    diff = unified_diff(html, state["current_html"])
    if diff:
        print("\nChanges:\n" + diff)
    print(f"\nWrote {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fix the accessibility violations in an HTML file.")
    parser.add_argument("file", nargs="?", help="the .html file to repair")
    parser.add_argument("--rules", nargs="+", help="only these axe rule ids (default: every violated rule)")
    parser.add_argument("--out", help="where to write the result (default: <file>.fixed.html)")
    parser.add_argument("--model", help="provider:model (default: MODEL from .env)")
    parser.add_argument("--max-attempts", type=int, default=MAX_ATTEMPTS, help="tries per rule")
    parser.add_argument("--thread", help="save checkpoints under this name, so a stopped run can resume")
    parser.add_argument("--history", action="store_true", help="list the checkpoints LangGraph saved")
    parser.add_argument("--tools", action="store_true",
                        help="run as an MCP client: scan on the MCP server and let the model call its tools")
    parser.add_argument("--rag", action="store_true",
                        help="like --tools, plus W3C guidance retrieved for each rule (Phase 4)")
    parser.add_argument("--guard", action="store_true",
                        help="guardrails: screen the page for prompt injection and check every fix (Phase 5)")
    parser.add_argument("--approve", action="store_true",
                        help="like --guard, and pause so you approve, edit or reject the final change")
    parser.add_argument("--graph", action="store_true", help="print the graph as a Mermaid diagram and exit")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    if args.graph:
        print(mermaid())
        return 0
    if not args.file:
        parser.error("give an HTML file, or --graph")
    return asyncio.run(_run_cli(args))


if __name__ == "__main__":
    sys.exit(main())
