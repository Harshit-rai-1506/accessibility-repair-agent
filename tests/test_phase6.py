"""Phase 6 tooling: statistics, comparison and failure reports, several methods per run, public pages."""

import asyncio
import importlib.util
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from a11y_agent import stats
from a11y_agent.evaluate import (
    code_version,
    compare_table,
    cost_table,
    failure_kind,
    failures_report,
    main as evaluate_main,
    summarize_rows,
)

ROOT = Path(__file__).resolve().parents[1]


def row(i, old=5, new=0, *, dropped=False, resolved=None, status="ok", response="", introduced=(), target_fixed=None,
        problems=()):
    return {"id": f"t-{i:03}", "rule_id": "image-alt", "category": "Syntax", "page_level": False, "status": status,
            "old_score": old, "new_score": new, "target_fixed": new == 0 if target_fixed is None else target_fixed,
            "resolved": (new == 0) if resolved is None else resolved, "introduced": list(introduced),
            "dropped_content": dropped, "text_kept": 1.0, "elements_kept": 1.0, "passes": 1, "calls": 2,
            "cached_calls": 1, "input_tokens": 1000, "output_tokens": 100, "model_seconds": 2.0, "seconds": 5.0,
            "note": "", "response": response, "guard": None, "code": "abc123", "guard_problems": list(problems)}


def test_scores_count_deletions_as_failures_like_the_report():
    rows = [row(1), row(2, new=5), row(3, dropped=True)]
    assert stats.score_decrease(rows) == pytest.approx(1 - 10 / 15)
    assert stats.score_decrease(rows, strict=False) == pytest.approx(1 - 5 / 15)
    assert summarize_rows(rows, method="m", model=None, dataset="t", split="dev",
                          total_items=3)["deletions_fail"]["score_decrease"] == pytest.approx(1 - 10 / 15, abs=1e-4)


def test_the_safe_score_counts_fixes_the_guardrails_reject_as_no_fix():
    """agent_tools' dev fixes that put <main> before <html> passed axe-core; the safe score does not count them."""
    rows = [row(1), row(2, problems=["broken_markup"]), row(3, dropped=True)]
    summary = summarize_rows(rows, method="m", model=None, dataset="t", split="dev", total_items=3)
    assert summary["all"]["score_decrease"] == 1.0
    assert summary["safe"]["score_decrease"] == pytest.approx(1 / 3, abs=1e-4) and summary["unsafe_fixes"] == 1
    assert stats.score_decrease(rows) == pytest.approx(1 / 3) and not stats.resolved(rows[1])
    assert failure_kind(rows[1]) == "unsafe"


def test_guard_problems_are_recorded_for_every_method():
    from a11y_agent.evaluate import guard_problems
    page = '<html lang="en">'
    assert guard_problems(page, "<main>" + page + "</main>") == ["broken_markup"]
    assert guard_problems(page, page + "<main></main>") == []
    assert guard_problems(page, None) == [] and guard_problems(page, page) == []


def test_bootstrap_intervals_contain_the_score_and_shrink_with_more_items():
    small = [row(i, new=0 if i % 3 else 5) for i in range(30)]
    large = [row(i, new=0 if i % 3 else 5) for i in range(300)]
    s_low, s_high = stats.bootstrap_interval(small)
    l_low, l_high = stats.bootstrap_interval(large)
    assert s_low <= stats.score_decrease(small) <= s_high
    assert (l_high - l_low) < (s_high - s_low)
    assert stats.bootstrap_interval(small) == (s_low, s_high)                 # fixed seed: reproducible


def test_paired_difference_and_sign_test():
    a = [row(i) for i in range(20)]
    b = [row(i, new=5 if i < 8 else 0) for i in range(20)]
    diff, low, high = stats.paired_difference(a, b)
    assert diff == pytest.approx(0.4) and 0 < low <= diff <= high
    assert stats.paired_flips(a, b) == (8, 0)
    assert stats.sign_test(8, 0) == pytest.approx(2 / 256)
    assert stats.sign_test(7, 2) == pytest.approx(0.1797, abs=1e-3)          # the dev set's retrieval split
    assert stats.sign_test(0, 0) == 1.0
    same = stats.paired_difference(a, a)
    assert same == (0.0, 0.0, 0.0)


def test_compare_and_cost_tables_use_common_items_and_a_baseline():
    base = ({"method": "agent_tools", "model": "mistralai:mistral-small-2603"}, [row(i) for i in range(10)])
    other = ({"method": "zero_shot", "model": "mistralai:mistral-small-2603"},
             [row(i, new=5 if i < 4 else 0) for i in range(10)] + [row(99)])          # one extra item: ignored
    table = compare_table([base, other], "agent_tools")
    assert "| zero_shot | mistral-small-2603 | 10 | 0.60 |" in table and "| -0.40 |" in table and "| 0 / 4 |" in table
    cost = cost_table([base])
    assert "| agent_tools | mistral-small-2603 | 2.0 | 1,100 | 2.0 s | 5.0 s / 5.0 s | 10/20 | $0.02 |" in cost


@pytest.mark.parametrize("kwargs, kind", [
    ({"status": "blocked", "new": 5}, "blocked"),
    ({"status": "too_large", "new": 5}, "too_large"),
    ({"dropped": True, "resolved": True}, "deleted"),
    ({"new": 3, "introduced": ["region"], "target_fixed": True, "resolved": False}, "broke"),
    ({"new": 5, "response": "propose: 1 edit(s)\nguard: rejected (broken_markup): x -> give_up"}, "guard"),
    ({"new": 5, "response": "propose: edits could not be applied: Edit 1: the find text was not found"}, "no_match"),
    ({"new": 5, "response": "propose: 1 edit(s)\nverify: The rule still fails -> give_up"}, "still_fails"),
    ({"old": 10, "new": 4, "target_fixed": False, "resolved": False}, "partial"),
])
def test_failures_are_sorted_by_why(kwargs, kind):
    old = kwargs.pop("old", 5)
    assert failure_kind(row(1, old=old, **kwargs)) == kind


def test_failures_report_counts_and_lists():
    rows = [row(1), row(2, new=5, response="propose: edits could not be applied: Edit 1"), row(3, dropped=True)]
    text = failures_report(rows, "agent", limit=5)
    assert text.startswith("2 of 3 items not resolved by agent.")
    assert "| edits did not match the page | 1 |" in text and "| t-003 | image-alt | no | deleted content |" in text


def test_every_result_row_records_the_code_version():
    version = code_version()
    assert version == "unknown" or len(version.split("+")[0]) >= 7


@pytest.mark.browser
@pytest.mark.data
def test_several_methods_in_one_run_and_the_compare_command(tmp_path, monkeypatch, capsys):
    from a11y_agent import evaluate
    from a11y_agent.benchmark import BenchmarkItem
    items = [BenchmarkItem(id="t-001", dataset="t", rule_id="image-alt", category="Syntax", impact="critical",
                           url="https://example.com", description="", html='<img src="cat.png">')]
    monkeypatch.setattr(evaluate, "load_split", lambda dataset, split: items)
    monkeypatch.setattr(evaluate.settings.__class__, "runs_dir", property(lambda self: tmp_path / "runs"))
    monkeypatch.setattr(evaluate, "RESULTS_DIR", tmp_path / "results")
    assert evaluate_main(["run", "--method", "noop", "noop", "--split", "dev"]) == 0
    assert evaluate_main(["compare", "--split", "dev", "--baseline", "noop", "--save"]) == 0
    out = capsys.readouterr().out
    assert "| noop | - | 1 | 0.00 |" in out and (tmp_path / "results" / "accessguru-dev" / "comparison.md").exists()


def _load_public_pages():
    spec = importlib.util.spec_from_file_location("public_pages", ROOT / "scripts" / "public_pages.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.browser
@pytest.mark.data
def test_public_page_before_and_after(tmp_path):
    from fakes import ToolModel, submit

    page = (b'<!doctype html><html lang="en"><head><title>Shop</title></head><body><main><h1>Shop</h1>'
            b'<img src="cat.png"><p>Welcome to our shop.</p></main></body></html>')

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(page if self.path == "/" else b"")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    public_pages = _load_public_pages()
    llm = ToolModel(replies=[submit(('<img src="cat.png">', '<img src="cat.png" alt="A cat">'))])
    try:
        code = asyncio.run(public_pages.repair([f"http://127.0.0.1:{server.server_port}/"], "fake:model", 5,
                                               results_dir=tmp_path / "results", runs_dir=tmp_path / "runs", llm=llm))
    finally:
        server.shutdown()
    assert code == 0
    report = next((tmp_path / "results").glob("*.md")).read_text(encoding="utf-8")
    assert "**Violation score: 5 -> 0**" in report and "| image-alt | fixed | 1 |" in report
    assert 'alt="A cat"' in report                                           # the diff
    assert "added_text_alternative" not in report and "screen readers hear" in report   # the flag, in words
    assert next((tmp_path / "runs" / "public").glob("*.fixed.html")).read_text(encoding="utf-8").count('alt="A cat"') == 1
