"""Prompt rendering and pulling HTML out of replies."""

import pytest

from a11y_agent.benchmark import BenchmarkItem
from a11y_agent.prompts import (
    ACCESSGURU_MARKERS,
    accessguru_step1,
    accessguru_step2,
    extract_html,
    zero_shot_messages,
)

ITEM = BenchmarkItem(id="t-1", dataset="t", rule_id="image-alt", category="Syntax", impact="critical",
                     url="https://example.com", description="Ensures <img> elements have alternate text",
                     html='<img src="cat.png">')


@pytest.mark.parametrize("reply, expected", [
    ("Here:\n```html\n<img alt=\"x\">\n```", '<img alt="x">'),
    ("```html\n<p>first</p>\n```\nbetter:\n```\n<p>second</p>\n```", "<p>second</p>"),
    ("###albidaya###\n<img alt=\"x\">\n###alnihaya###", '<img alt="x">'),
    ("###albidaya###\n```html\n<img alt=\"x\">\n```\n###alnihaya###", '<img alt="x">'),
    ("No code here, sorry.", None),
    ("", None),
])
def test_extract_html(reply, expected):
    assert extract_html(reply, ACCESSGURU_MARKERS) == expected


def test_markers_win_over_earlier_code_blocks():
    reply = "Draft:\n```html\n<p>draft</p>\n```\n###albidaya###\n<p>final</p>\n###alnihaya###"
    assert extract_html(reply, ACCESSGURU_MARKERS) == "<p>final</p>"


@pytest.mark.data
def test_zero_shot_prompt_uses_axe_help_text():
    (_, system), (_, user) = zero_shot_messages("image-alt", ITEM.html)
    assert "accessibility" in system
    assert '"image-alt": Images must have alternative text' in user
    assert ITEM.html in user and "```html" in user


@pytest.mark.data
def test_accessguru_prompts():
    first = accessguru_step1(ITEM)
    assert 'Violation type: "image-alt"' in first and 'Impact: "Critical"' in first
    assert "expertise in HTML coding is required" in first          # the Syntax category description
    assert "ABSOLUTE RULES" in accessguru_step1(ITEM, reprompt=True)
    assert "1.1.1 Non-text Content:" in accessguru_step2("image-alt")
