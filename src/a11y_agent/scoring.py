"""Turning scan results into numbers.

We reuse the metric from the AccessGuru paper (ASSETS 2025) so our results
can be compared with theirs:

* Each violated rule scores by its impact: critical 5, serious 4,
  moderate 3, minor 2.
* For one fix attempt on one target violation:
    - target gone   -> new score = impact of any rules the fix newly broke
    - target remains -> new score = old score + impact of newly broken rules
* Over a benchmark, "violation score decrease" = 1 - (sum of new) / (sum of old).
  1.0 means every target fixed with nothing broken; 0 means no progress.

Penalising newly introduced violations matters: a model that deletes the
whole element "fixes" the target but often breaks something else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .scanner import ScanResult

IMPACT_SCORE = {"critical": 5, "serious": 4, "moderate": 3, "minor": 2}


def impact_score(impact: str | None) -> int:
    return IMPACT_SCORE.get((impact or "").lower(), 0)


def violation_score(result: ScanResult) -> int:
    """Total score of a scan: one entry per violated rule, weighted by impact."""
    return sum(impact_score(v.impact) for v in result.violations)


@dataclass
class FixOutcome:
    target_rule: str
    old_score: int
    new_score: int
    target_fixed: bool
    introduced: list[str] = field(default_factory=list)  # rules broken by the fix

    @property
    def resolved(self) -> bool:
        """Fixed cleanly: target gone and nothing new broken."""
        return self.new_score == 0


def evaluate_fix(before: ScanResult, after: ScanResult, target_rule: str) -> FixOutcome:
    """Score one fix of one target violation by comparing scans before and after."""
    target = before.get(target_rule)
    if target is None:
        raise ValueError(
            f"'{target_rule}' is not violated in the original HTML, so a fix cannot be verified"
        )
    old = impact_score(target.impact)
    introduced = sorted(after.rule_ids - before.rule_ids)
    introduced_score = sum(impact_score(after.get(r).impact) for r in introduced)
    fixed = target_rule not in after.rule_ids
    new = introduced_score if fixed else old + introduced_score
    return FixOutcome(target_rule, old, new, fixed, introduced)


def summarize(outcomes: Iterable[FixOutcome]) -> dict:
    """Aggregate fix outcomes into the headline benchmark numbers."""
    outcomes = list(outcomes)
    n = len(outcomes)
    old_total = sum(o.old_score for o in outcomes)
    new_total = sum(o.new_score for o in outcomes)
    return {
        "violations": n,
        "score_decrease": round(1 - new_total / old_total, 4) if old_total else 0.0,
        "resolved": sum(o.resolved for o in outcomes),
        "target_fixed": sum(o.target_fixed for o in outcomes),
        "introduced_new_violations": sum(bool(o.introduced) for o in outcomes),
        "avg_old_score": round(old_total / n, 3) if n else 0.0,
        "avg_new_score": round(new_total / n, 3) if n else 0.0,
    }
