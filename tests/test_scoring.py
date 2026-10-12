"""Scoring logic, tested without a browser using hand-made scan results."""

import pytest

from a11y_agent.scanner import ScanResult, Violation
from a11y_agent.scoring import evaluate_fix, impact_score, summarize, violation_score


def scan(*rules: tuple[str, str]) -> ScanResult:
    return ScanResult(
        violations=[Violation(rule_id=r, impact=i, help="", help_url="", tags=[]) for r, i in rules],
        axe_version="test",
    )


def test_impact_scores_match_accessguru():
    assert [impact_score(i) for i in ("critical", "serious", "moderate", "minor")] == [5, 4, 3, 2]
    assert impact_score(None) == 0
    assert impact_score("Serious") == 4  # case-insensitive, the dataset uses "Serious"


def test_violation_score_counts_each_rule_once():
    assert violation_score(scan(("image-alt", "critical"), ("region", "moderate"))) == 8


def test_clean_fix_scores_zero():
    before = scan(("image-alt", "critical"), ("region", "moderate"))
    after = scan(("region", "moderate"))  # page-level rule still there, but not new
    outcome = evaluate_fix(before, after, "image-alt")
    assert outcome.target_fixed and outcome.resolved
    assert (outcome.old_score, outcome.new_score) == (5, 0)


def test_fix_that_breaks_something_else_is_penalised():
    before = scan(("image-alt", "critical"))
    after = scan(("link-name", "serious"))
    outcome = evaluate_fix(before, after, "image-alt")
    assert outcome.target_fixed and not outcome.resolved
    assert outcome.new_score == 4 and outcome.introduced == ["link-name"]


def test_failed_fix_keeps_old_score_plus_new_damage():
    before = scan(("image-alt", "critical"))
    after = scan(("image-alt", "critical"), ("empty-heading", "minor"))
    outcome = evaluate_fix(before, after, "image-alt")
    assert not outcome.target_fixed
    assert outcome.new_score == 5 + 2


def test_unreproducible_target_is_rejected():
    with pytest.raises(ValueError):
        evaluate_fix(scan(), scan(), "image-alt")


def test_summary_uses_total_score_decrease():
    before = scan(("image-alt", "critical"))
    outcomes = [
        evaluate_fix(before, scan(), "image-alt"),                          # 5 -> 0
        evaluate_fix(before, scan(("image-alt", "critical")), "image-alt"),  # 5 -> 5
    ]
    s = summarize(outcomes)
    assert s["score_decrease"] == 0.5
    assert s["resolved"] == 1 and s["target_fixed"] == 1
