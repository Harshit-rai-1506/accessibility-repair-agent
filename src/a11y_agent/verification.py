"""Judging one fix: did it remove the violation without breaking or deleting anything?

Used in two places, so both always agree:

* the agent's ``verify`` node, which decides whether to accept a fix or retry;
* the MCP tool ``verify_fix``, which lets any MCP client (Claude Desktop, an
  IDE, another agent) check a fix the same way.

The problems are written as sentences addressed to whoever made the fix,
because the agent sends them straight back to the model as feedback.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .content import DROPPED_CONTENT, content_kept
from .scanner import ScanResult, Violation
from .scoring import evaluate_fix


@dataclass
class Judgement:
    passed: bool                     # target gone, nothing newly broken, content kept
    target_fixed: bool
    introduced: list[str]            # rules that fail now but did not before
    old_score: int                   # AccessGuru score of the target before
    new_score: int                   # and after (0 = resolved cleanly)
    text_kept: float                 # share of the original words still present
    elements_kept: float             # share of the original elements still present
    content_ok: bool
    problems: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def describe_failures(violation: Violation, limit: int = 3) -> str:
    """The failing elements of a rule, with axe-core's explanation for each."""
    parts = []
    for node in violation.nodes[:limit]:
        summary = " ".join(node.failure_summary.split())
        parts.append(f"{node.html[:200]} ({summary[:300]})" if summary else node.html[:200])
    more = len(violation.nodes) - min(limit, len(violation.nodes))
    return "; ".join(parts) + (f"; and {more} more" if more > 0 else "")


def judge_fix(before: ScanResult, after: ScanResult, rule_id: str, original_html: str, fixed_html: str) -> Judgement:
    """Compare scans before and after a fix of ``rule_id``; check the content survived."""
    outcome = evaluate_fix(before, after, rule_id)
    text, elements = content_kept(original_html, fixed_html)
    content_ok = min(text, elements) >= DROPPED_CONTENT
    problems = []
    if not outcome.target_fixed:
        problems.append(f'The rule "{rule_id}" still fails: {describe_failures(after.get(rule_id))}.')
    for rule in outcome.introduced:
        problems.append(f'Your change made another rule fail, "{rule}": {describe_failures(after.get(rule))}.')
    if not content_ok:
        problems.append(f"Your change removed content (kept {text:.0%} of the words and {elements:.0%} "
                        "of the elements). Keep all existing text and elements.")
    return Judgement(passed=not problems, target_fixed=outcome.target_fixed, introduced=outcome.introduced,
                     old_score=outcome.old_score, new_score=outcome.new_score, text_kept=text,
                     elements_kept=elements, content_ok=content_ok, problems=problems)
