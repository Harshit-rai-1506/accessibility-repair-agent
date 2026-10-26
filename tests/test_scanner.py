"""The real scanner: headless Chromium + axe-core on the example pages."""

import asyncio

import pytest

from a11y_agent.scanner import Scanner
from a11y_agent.scoring import evaluate_fix, violation_score

pytestmark = pytest.mark.browser


def scan_files(*paths):
    async def run():
        async with Scanner() as scanner:
            return [await scanner.scan_html(p.read_text(encoding="utf-8")) for p in paths]
    return asyncio.run(run())


def test_broken_page_has_expected_violations(examples_dir):
    (result,) = scan_files(examples_dir / "broken_page.html")
    expected = {"image-alt", "button-name", "link-name", "html-has-lang", "document-title",
                "color-contrast", "meta-viewport", "empty-heading", "landmark-one-main"}
    assert expected <= result.rule_ids
    assert violation_score(result) > 30


def test_fixed_page_is_clean(examples_dir):
    (result,) = scan_files(examples_dir / "fixed_page.html")
    assert result.violations == []


def test_violation_details_are_useful(examples_dir):
    (result,) = scan_files(examples_dir / "broken_page.html")
    img = result.get("image-alt")
    assert img.impact == "critical"
    assert "1.1.1" in img.wcag_criteria
    assert all(node.target and "<img" in node.html for node in img.nodes)


def test_fix_verification_end_to_end(examples_dir):
    broken, fixed = scan_files(examples_dir / "broken_page.html", examples_dir / "fixed_page.html")
    for rule in broken.rule_ids:
        outcome = evaluate_fix(broken, fixed, rule)
        assert outcome.resolved, rule


def test_snippet_scan_and_network_blocked():
    async def run():
        async with Scanner() as scanner:
            # The image URL would normally be fetched; with the network blocked it must not hang.
            return await scanner.scan_html('<img src="https://example.com/cat.png">')
    result = asyncio.run(run())
    assert "image-alt" in result.rule_ids
