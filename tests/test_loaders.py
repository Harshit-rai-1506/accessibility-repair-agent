"""The LangChain document loaders, run on the real downloaded resources.

These tests are skipped until `python scripts/fetch_resources.py` has run.
"""

import re

import pytest

from a11y_agent.loaders import (
    AxeRuleLoader,
    BenchmarkLoader,
    WCAGIndex,
    WCAGLoader,
    WCAG_METADATA_KEYS,
    _join_python_list,
    _parse_associated_techniques,
    _split_front_matter,
)
from a11y_agent.config import settings

pytestmark = pytest.mark.data


@pytest.fixture(scope="module")
def index():
    return WCAGIndex.build(settings.corpus_dir / "wcag")


@pytest.fixture(scope="module")
def wcag_docs(index):
    return WCAGLoader(index=index).load()


def by_id(docs, doc_id):
    return next(d for d in docs if d.id == doc_id)


def assert_vector_store_safe(docs):
    """Chroma accepts only str, int, float and bool metadata values."""
    for doc in docs:
        for key, value in doc.metadata.items():
            assert isinstance(value, (str, int, float, bool)), (doc.id, key, type(value))


# --- Benchmark ------------------------------------------------------------------

def test_accessguru_rows_become_documents():
    docs = BenchmarkLoader("accessguru").load()
    assert len(docs) == 305
    assert len({d.id for d in docs}) == 305               # stable, unique ids
    first = docs[0]
    assert first.page_content.startswith("<")             # raw HTML, no "column: " label
    assert first.metadata["rule_id"] == "color-contrast-enhanced"
    assert first.metadata["impact_score"] == 4
    assert_vector_store_safe(docs)


def test_category_filter():
    docs = BenchmarkLoader("accessguru", categories=["Syntax", "Layout"]).load()
    assert len(docs) == 250
    assert {d.metadata["category"] for d in docs} == {"Syntax", "Layout"}


def test_access_snippets_are_unwrapped_from_python_lists():
    docs = BenchmarkLoader("access").load()
    assert len(docs) == 171
    assert not any(d.page_content.startswith("[") for d in docs)
    assert docs[0].page_content.startswith("<select")
    assert all(d.metadata["impact_score"] in (2, 3, 4, 5) for d in docs)


def test_unknown_dataset_is_rejected():
    with pytest.raises(ValueError):
        BenchmarkLoader("nope")


def test_join_python_list():
    assert _join_python_list("[['<a>', '<b>']]") == "<a>\n<b>"
    assert _join_python_list("<a>not a list</a>") == "<a>not a list</a>"


# --- WCAG index -------------------------------------------------------------------

@pytest.mark.parametrize("slug, number, level, version", [
    ("non-text-content", "1.1.1", "A", "2.0"),
    ("contrast-minimum", "1.4.3", "AA", "2.0"),
    ("contrast-enhanced", "1.4.6", "AAA", "2.0"),
    ("reflow", "1.4.10", "AA", "2.1"),
    ("target-size-minimum", "2.5.8", "AA", "2.2"),
    ("name-role-value", "4.1.2", "A", "2.0"),
])
def test_success_criterion_numbers(index, slug, number, level, version):
    c = index.criteria[slug]
    assert (c.number, c.level, c.version) == (number, level, version)


def test_index_covers_all_criteria(index):
    assert len(index.criteria) == 87                      # WCAG 2.2, plus removed 4.1.1
    assert index.criteria["parsing"].obsolete
    assert index.guidelines["text-alternatives"] == ("1.1", "Text Alternatives")


def test_technique_links(index):
    assert [(c.number, kind) for c, kind in index.criteria_for_technique("H37")] == [("1.1.1", "sufficient")]
    assert ("1.1.1", "failure") in [(c.number, kind) for c, kind in index.criteria_for_technique("F65")]


def test_parse_associated_techniques(tmp_path):
    js = tmp_path / "data.js"
    js.write_text(
        'export default {\n    associatedTechniques: {\n'
        '      "non-text-content": {\n        sufficient: [ { techniques: ["G94"] } ],\n'
        '        failure: ["F3", "F13"],\n      },\n'
        '      "captions-live": {\n        advisory: ["G93"],\n      },\n'
    )
    assert _parse_associated_techniques(js) == {
        "non-text-content": {"sufficient": ["G94"], "failure": ["F3", "F13"]},
        "captions-live": {"advisory": ["G93"]},
    }


def test_front_matter():
    keys, rest = _split_front_matter("---\nobsoleteSince: 20\n---\n<h1>x</h1>")
    assert keys == {"obsoleteSince": "20"} and rest.strip() == "<h1>x</h1>"
    assert _split_front_matter("<h1>x</h1>") == ({}, "<h1>x</h1>")


# --- WCAG documents ---------------------------------------------------------------

def test_wcag_document_counts(wcag_docs):
    counts = {}
    for d in wcag_docs:
        counts[d.metadata["doc_type"]] = counts.get(d.metadata["doc_type"], 0) + 1
    assert counts["success_criterion"] == 86               # obsolete 4.1.1 excluded
    assert counts["understanding"] >= 86                   # criteria + guideline pages
    assert counts["technique"] > 250 and counts["failure"] > 80
    assert len({d.id for d in wcag_docs}) == len(wcag_docs)


def test_wcag_metadata_shape(wcag_docs):
    assert all(set(d.metadata) == set(WCAG_METADATA_KEYS) for d in wcag_docs)
    assert_vector_store_safe(wcag_docs)


def test_wcag_pages_are_clean_markdown(wcag_docs):
    for d in wcag_docs:
        assert d.page_content.startswith("# "), d.id      # every page opens with its title
        assert "{%" not in d.page_content and "{{" not in d.page_content, d.id
        # No leftover page markup. HTML inside code examples is expected, so
        # remove fenced blocks and `inline code` before checking.
        prose = re.sub(r"`[^`]*`", "", re.sub(r"```.*?```", "", d.page_content, flags=re.S))
        assert not re.search(r"</?section\b|\bclass=\"|data-include", prose), d.id


def test_understanding_page(wcag_docs):
    doc = by_id(wcag_docs, "understanding/22/target-size-minimum.html")
    assert doc.metadata["sc"] == "2.5.8" and doc.metadata["level"] == "AA"
    assert doc.metadata["url"] == "https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum"
    assert "Success criterion 2.5.8 Target Size (Minimum), Level AA" in doc.page_content
    assert "## Techniques" in doc.page_content and "C42:" in doc.page_content


def test_technique_page_keeps_code_examples(wcag_docs):
    doc = by_id(wcag_docs, "techniques/html/H37.html")
    assert doc.metadata["technique_id"] == "H37" and doc.metadata["sc"] == "1.1.1"
    assert "```html" in doc.page_content
    assert "Used for success criterion 1.1.1 Non-text Content" in doc.page_content


def test_doc_type_filter(index):
    docs = WCAGLoader(index=index, doc_types=["success_criterion"]).load()
    assert {d.metadata["doc_type"] for d in docs} == {"success_criterion"}
    with pytest.raises(ValueError):
        WCAGLoader(doc_types=["blog"])


# --- axe rules ------------------------------------------------------------------

def test_axe_rules(index):
    docs = AxeRuleLoader(index=index).load()
    assert len(docs) >= 90
    image_alt = by_id(docs, "axe:image-alt")
    assert image_alt.metadata["sc"] == "1.1.1"
    assert "1.1.1 Non-text Content (Level A)" in image_alt.page_content
    assert "Element has an alt attribute" in image_alt.page_content
    assert by_id(docs, "axe:region").metadata["best_practice"] is True
    assert not any("${" in d.page_content for d in docs)
    assert_vector_store_safe(docs)
