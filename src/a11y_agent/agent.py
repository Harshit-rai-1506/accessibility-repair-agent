"""The repair agent: a LangGraph state machine that fixes accessibility violations
and proves every fix by scanning again.

The graph (``python -m a11y_agent.agent --graph`` prints it as Mermaid):

    START -> scan -> pick
    pick     --next rule-->      propose    or  --all done--> report -> END
    propose  --edits applied-->  verify     or  --retry--> propose   or  --give up--> give_up
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

Usage:

    python -m a11y_agent.agent examples/broken_page.html                 # fix every violation
    python -m a11y_agent.agent page.html --rules image-alt link-name     # only these rules
    python -m a11y_agent.agent page.html --thread my-page                # saved checkpoints: resume after a stop
    python -m a11y_agent.agent page.html --tools                         # as an MCP client, with model tools
    python -m a11y_agent.agent --graph                                   # print the graph (Mermaid)
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import difflib
import operator
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.runtime import Runtime

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
from .prompts import agent_messages
from .scanner import ScanResult, Scanner, Violation, ViolationNode
from .scoring import impact_score
from .verification import judge_fix

MAX_ATTEMPTS = 3          # tries per rule before giving up
MAX_TOOL_ROUNDS = 3       # tool-calling rounds per attempt before the model must submit (Phase 3)
TOOL_RESULTS_RESERVE = 800    # tokens kept free for tool results when tools are offered
MAX_RULES = 25            # rules per run, a hard bound on cost
NODES_SHOWN = 5           # failing elements described to the model per rule
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
    initial_score: int
    queue: list[str]              # rules still to fix, most severe first
    done_rules: list[str]         # rules already handled (fixed or not)
    rule: str | None              # the rule being fixed now
    attempt: int                  # attempts made on the current rule
    candidate: dict | None        # the latest proposed fix and, after verify, its result
    verdict: str                  # verify's decision: accept | retry | give_up
    stop_reason: str              # why the current rule cannot continue, e.g. "too_large"
    feedback: Annotated[list[dict], per_rule_log]     # what went wrong, shown to the model on retry
    candidates: Annotated[list[dict], per_rule_log]   # verified fixes for the current rule
    # Results (grow for the whole run)
    outcomes: Annotated[list[dict], operator.add]     # one entry per rule
    usage: Annotated[list[dict], operator.add]        # one entry per model call (tokens, seconds)
    steps: Annotated[list[dict], operator.add]        # the trace: one entry per node run
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
        "current_html": state["html"], "scan": current, "initial_score": score, "queue": queue,
        "done_rules": [], "outcomes": outcomes,
        "steps": _step("scan", f"{detail}: {len(present)} rule(s) violated, score {score}; "
                               f"to fix: {', '.join(queue) or 'nothing'}"),
    }


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
        "queue": queue, "rule": rule, "attempt": 0, "candidate": None, "verdict": "", "stop_reason": "",
        "feedback": None, "candidates": None,            # None = clear (see per_rule_log)
        "outcomes": outcomes,
        "steps": _step("pick", f"next rule: {rule}", rule=rule),
    }


def build_request(state: AgentState, violation: dict, model: ModelClient,
                  tool_specs: list[dict] | None = None) -> tuple[list, int, bool]:
    """The messages, the reply budget, and whether the HTML had to be excerpted.

    The free tier limits each request (prompt + tool definitions + reply
    budget) to about 8,000 tokens. If the whole page does not fit next to the
    prompt and a minimum reply budget, only the parts around the failing
    elements are shown. With tools, room is also kept for a few tool results.
    """
    html, feedback = state["current_html"], state.get("feedback") or []
    tools = bool(tool_specs)
    low, high = OUTPUT_TOKENS[0], reply_budget(violation, model)
    limit = int(model.max_request_tokens * BUDGET_MARGIN) if model.max_request_tokens else 0
    shown, excerpted = html, False
    if limit:
        reserve = tools_tokens(tool_specs or []) + (TOOL_RESULTS_RESERVE if tools else 0)
        overhead = messages_tokens(agent_messages(violation, "", feedback=feedback, excerpted=True, tools=tools))
        html_tokens = max(limit - overhead - reserve - low, 300)
        if estimate_tokens(html) > html_tokens:
            shown, excerpted = focus_html(html, [n["html"] for n in violation["nodes"]], html_tokens * 4)
    messages = agent_messages(violation, shown, feedback=feedback, excerpted=excerpted, tools=tools)
    used = messages_tokens(messages) + tools_tokens(tool_specs or [])
    max_tokens = high if not limit else max(low, min(high, limit - used))
    return messages, max_tokens, excerpted


def reply_budget(violation: dict, model: ModelClient) -> int:
    """Largest reply to allow: enough for one edit per failing element, within the provider's output limit.

    A fixed 3,072 tokens cut off replies that had to fix 55 images (dev item
    accessguru-175). About 150 tokens per element covers a find, a replace and
    some reasoning.
    """
    wanted = 1024 + 150 * violation.get("node_count", 1)
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
    out_of_tries = attempt >= ctx.max_attempts

    def failed(problem: str, edits_text: str = "", usage: list | None = None, detail: str = "") -> dict:
        problem += repeated(state.get("feedback") or [], edits_text)
        return {"attempt": attempt, "candidate": None, "verdict": "give_up" if out_of_tries else "retry",
                "usage": usage or [], "feedback": [{"attempt": attempt, "edits": edits_text, "problem": problem}],
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
            except ProposalError as e:
                if record.finish_reason == "length":
                    raise ProposalError("Your reply was cut off before it finished. Use fewer, shorter edits.") from e
                raise
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

    if judgement.passed:
        verdict = "accept"
    elif state["attempt"] < runtime.context.max_attempts:
        verdict = "retry"
    else:
        verdict = "give_up"
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
    update = {"candidate": result, "candidates": [result], "verdict": verdict, "feedback": feedback,
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


def after_verify(state: AgentState) -> str:
    return {"accept": "passed", "retry": "retry"}.get(state["verdict"], "give up")


def build_graph(checkpointer=None):
    """Assemble and compile the graph. Pass a checkpointer to save state after every step."""
    graph = StateGraph(AgentState, context_schema=AgentContext)
    for name, fn in [("scan", scan), ("pick", pick), ("propose", propose), ("verify", verify),
                     ("accept", accept), ("give_up", give_up), ("report", report)]:
        graph.add_node(name, fn)
    graph.add_edge(START, "scan")
    graph.add_edge("scan", "pick")
    graph.add_conditional_edges("pick", after_pick, {"next rule": "propose", "all done": "report"})
    graph.add_conditional_edges("propose", after_propose,
                                {"edits applied": "verify", "retry": "propose", "give up": "give_up"})
    graph.add_conditional_edges("verify", after_verify,
                                {"passed": "accept", "retry": "propose", "give up": "give_up"})
    graph.add_edge("accept", "pick")
    graph.add_edge("give_up", "pick")
    graph.add_edge("report", END)
    return graph.compile(checkpointer=checkpointer, name="accessibility-repair-agent")


def recursion_limit(max_attempts: int = MAX_ATTEMPTS, max_rules: int = MAX_RULES) -> int:
    """LangGraph stops any run after this many steps: a second safety net behind max_attempts.

    Per rule: pick, up to max_attempts x (propose + verify), accept or give_up.
    """
    return 4 + max_rules * (2 * max_attempts + 2)


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


async def _run_cli(args) -> int:
    from .model_client import ResponseCache, StopRun

    path = Path(args.file)
    html = path.read_text(encoding="utf-8")
    out = Path(args.out) if args.out else path.with_name(path.stem + ".fixed.html")
    model = ModelClient(args.model, cache=ResponseCache(settings.runs_dir / "cache.sqlite"))
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
        if args.tools:                            # Phase 3: scans and tools come from the MCP server
            from .mcp_client import connect
            server = await stack.enter_async_context(connect())
            scanner, tools = server.scanner, server.model_tools()
            print(f"MCP server started; the model may call: {', '.join(t.name for t in tools)}")
        else:
            scanner, tools = await stack.enter_async_context(Scanner()), None
        graph = build_graph(checkpointer=saver)
        context = AgentContext(scanner=scanner, model=model, max_attempts=args.max_attempts, tools=tools)
        snapshot = await graph.aget_state(config)
        if args.thread and snapshot.next:
            print(f"Resuming thread '{args.thread}' (it stopped before: {', '.join(snapshot.next)})")
            graph_input = None
        else:
            if snapshot.values:                   # a finished run on this thread: start it over
                await saver.adelete_thread(config["configurable"]["thread_id"])
            graph_input = {"html": html, "targets": args.rules or []}
        print(f"Repairing {path} with {model.spec} (up to {args.max_attempts} attempts per rule)")
        try:
            async for update in graph.astream(graph_input, config, context=context, stream_mode="updates"):
                for node_update in update.values():
                    for step in (node_update or {}).get("steps", []):
                        _print_step(step)
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
