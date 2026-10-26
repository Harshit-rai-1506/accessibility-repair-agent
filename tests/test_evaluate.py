"""The evaluation harness, end to end, with a scripted fake model (no API key needed)."""

import asyncio
import json
import time

import pytest

from a11y_agent.benchmark import BenchmarkItem
from a11y_agent.evaluate import content_kept, report_table, run_evaluation
from a11y_agent.model_client import (
    ModelClient,
    QuotaExhausted,
    RateLimiter,
    RequestTooLarge,
    ResponseCache,
    _error_kind,
)
from fakes import RateLimitError, ScriptedModel, delete_everything, make_it_worse, no_change

pytestmark = [pytest.mark.browser, pytest.mark.data]


def item(n, rule, html, category="Syntax"):
    return BenchmarkItem(id=f"t-{n:03d}", dataset="t", rule_id=rule, category=category, impact="critical",
                         url="https://example.com", description="", html=html)


ITEMS = [
    item(1, "image-alt", '<img src="cat.png">'),
    item(2, "button-name", "<button></button>"),
]


def run(method, items, tmp_path, llm=None, **kw):
    return asyncio.run(run_evaluation(
        method, items, dataset="t", split="dev", model_spec="fake:model", out_dir=tmp_path / method,
        llm=llm, cache=ResponseCache(tmp_path / "cache.sqlite"), limiter=RateLimiter(), quiet=True, **kw))


def rows(tmp_path, method):
    return [json.loads(l) for l in (tmp_path / method / "results.jsonl").read_text().splitlines()]


def test_noop_scores_zero(tmp_path):
    s = run("noop", ITEMS, tmp_path)
    assert s["all"]["score_decrease"] == 0.0 and s["all"]["resolved"] == 0
    assert s["complete"] and s["calls"] == 0


def test_zero_shot_good_fixes(tmp_path):
    s = run("zero_shot", ITEMS, tmp_path, llm=ScriptedModel())
    assert s["all"]["score_decrease"] == 1.0 and s["all"]["resolved"] == 2
    assert s["calls"] == 2 and s["input_tokens"] == 200
    r = rows(tmp_path, "zero_shot")[0]
    assert r["fixed_html"] == '<img alt="A cat" src="cat.png">' and r["text_kept"] == 1.0


def test_replies_are_cached(tmp_path):
    fake = ScriptedModel()
    run("zero_shot", ITEMS, tmp_path, llm=fake)
    (tmp_path / "zero_shot" / "results.jsonl").unlink()          # force a re-run of every item
    s = run("zero_shot", ITEMS, tmp_path, llm=fake)
    assert fake.calls == 2                                        # second run never reached the model
    assert all(r["cached_calls"] == r["calls"] for r in rows(tmp_path, "zero_shot"))
    assert s["all"]["resolved"] == 2


def test_runs_resume(tmp_path):
    run("zero_shot", ITEMS[:1], tmp_path, llm=ScriptedModel())
    fake = ScriptedModel()
    s = run("zero_shot", ITEMS, tmp_path, llm=fake)
    assert fake.calls == 1 and s["items_done"] == 2               # only the new item ran


def test_unparseable_reply_counts_as_not_fixed(tmp_path):
    s = run("zero_shot", ITEMS[:1], tmp_path, llm=ScriptedModel(reply_without_code=True))
    assert s["status"] == {"no_html": 1}
    assert s["all"]["score_decrease"] == 0.0


def test_deleting_content_is_flagged(tmp_path):
    s = run("zero_shot", ITEMS[:1], tmp_path, llm=ScriptedModel(first=delete_everything))
    r = rows(tmp_path, "zero_shot")[0]
    assert r["target_fixed"] and r["dropped_content"] and r["elements_kept"] == 0.0
    assert s["dropped_content"] == 1


def test_accessguru_reprompts_and_keeps_the_best(tmp_path):
    fake = ScriptedModel(first=no_change, reprompt=ScriptedModel().first)
    s = run("accessguru", ITEMS[:1], tmp_path, llm=fake)
    r = rows(tmp_path, "accessguru")[0]
    assert r["passes"] == 2 and r["calls"] == 8 and r["resolved"]
    assert "pass 2" in r["note"]
    assert s["all"]["score_decrease"] == 1.0


def test_accessguru_stops_after_a_good_first_pass(tmp_path):
    run("accessguru", ITEMS[:1], tmp_path, llm=ScriptedModel())
    r = rows(tmp_path, "accessguru")[0]
    assert r["passes"] == 1 and r["calls"] == 4 and r["resolved"]


def test_accessguru_never_returns_worse_than_the_input(tmp_path):
    run("accessguru", ITEMS[:1], tmp_path, llm=ScriptedModel(first=make_it_worse))
    r = rows(tmp_path, "accessguru")[0]
    assert r["passes"] == 2 and r["status"] == "no_improvement"
    assert r["fixed_html"] == ITEMS[0].html and r["new_score"] == r["old_score"]


def test_zero_shot_can_make_things_worse(tmp_path):
    run("zero_shot", ITEMS[:1], tmp_path, llm=ScriptedModel(first=make_it_worse))
    r = rows(tmp_path, "zero_shot")[0]
    assert r["introduced"] == ["link-name"] and r["new_score"] > r["old_score"]


def test_daily_quota_stops_the_run_and_keeps_nothing_partial(tmp_path):
    fake = ScriptedModel(raise_error=RateLimitError(
        "Rate limit reached on tokens per day (TPD): Limit 200000, Used 199999. Please try again in 7m30s."))
    s = run("zero_shot", ITEMS, tmp_path, llm=fake)
    assert "daily limit" in s["stopped"] and s["items_done"] == 0
    s = run("zero_shot", ITEMS, tmp_path, llm=ScriptedModel())    # "tomorrow": resumes
    assert s["complete"] and s["all"]["resolved"] == 2


def test_report_table(tmp_path):
    s = run("zero_shot", ITEMS, tmp_path, llm=ScriptedModel())
    table = report_table([s])
    assert "| zero_shot | model | 2/2 | 1.00 |" in table


# --- Pieces without a browser ----------------------------------------------------

def test_content_kept():
    assert content_kept("<p>Hello world</p>", "<p>Hello world</p>") == (1.0, 1.0)
    assert content_kept("<p>a b c d</p><p>e</p>", "<p>a b</p>") == (0.4, 0.5)


@pytest.mark.parametrize("message, status, kind", [
    ("Rate limit reached on tokens per minute (TPM): try again in 7.5s", 429, "minute"),
    ("Rate limit reached on tokens per day (TPD): Please try again in 1h2m3s", 429, "daily"),
    ("Request too large for model: Limit 8000, Requested 9000", 413, "too_large"),
    ("Internal server error", 500, "other"),
    ("Invalid API Key", 401, "auth"),
])
def test_error_kinds(message, status, kind):
    error = Exception(message)
    error.status_code = status
    assert _error_kind(error)[0] == kind


def test_daily_wait_is_parsed():
    error = Exception("tokens per day (TPD): Please try again in 1h2m3s")
    error.status_code = 429
    assert _error_kind(error) == ("daily", 3723.0)


def test_too_large_requests_are_refused_before_calling(tmp_path):
    client = ModelClient("fake:model", llm=ScriptedModel(), max_request_tokens=100, limiter=RateLimiter())
    with pytest.raises(RequestTooLarge):
        asyncio.run(client.chat([("human", "x" * 1000)], max_tokens=50))


def test_rate_limiter_waits_when_the_window_is_full():
    async def go():
        limiter = RateLimiter(tokens_per_minute=100, window=0.3)
        start = time.monotonic()
        await limiter.acquire(80)
        await limiter.acquire(80)       # does not fit until the first expires
        return time.monotonic() - start
    assert asyncio.run(go()) >= 0.25


def test_quota_error_type():
    client = ModelClient("fake:model", limiter=RateLimiter(), llm=ScriptedModel(
        raise_error=RateLimitError("Rate limit reached on requests per day (RPD)")))
    with pytest.raises(QuotaExhausted):
        asyncio.run(client.chat([("human", "hi")], max_tokens=10))


def test_common_subset_excludes_items_any_run_could_not_send():
    from a11y_agent.evaluate import common_subset

    def row(i, status, new=0):
        return {"id": i, "rule_id": "image-alt", "category": "Syntax", "page_level": False, "status": status,
                "old_score": 5, "new_score": new, "target_fixed": new == 0, "introduced": [],
                "dropped_content": False, "calls": 1, "cached_calls": 0, "input_tokens": 10,
                "output_tokens": 5, "model_seconds": 0.1}
    meta = {"model": "m", "dataset": "t", "split": "dev"}
    a = ({"method": "zero_shot", **meta}, [row("1", "ok"), row("2", "ok")])
    b = ({"method": "accessguru", **meta}, [row("1", "ok"), row("2", "too_large", new=5)])
    common, summaries = common_subset([a, b])
    assert common == {"1"}
    assert [s["all"]["score_decrease"] for s in summaries] == [1.0, 1.0]


def test_bad_key_stops_the_run(tmp_path):
    error = Exception("Error code: 401 - Invalid API Key")
    error.status_code = 401
    s = run("zero_shot", ITEMS, tmp_path, llm=ScriptedModel(raise_error=error))
    assert "API key" in s["stopped"] and s["items_done"] == 0


def test_report_compares_methods_only_within_a_model():
    from a11y_agent.evaluate import report_by_model, summarize_rows

    def run_of(method, model, new_score):
        row = {"id": "t-1", "rule_id": "image-alt", "category": "Syntax", "page_level": False, "status": "ok",
               "old_score": 5, "new_score": new_score, "target_fixed": new_score == 0, "resolved": new_score == 0,
               "introduced": [], "dropped_content": False, "calls": 1, "cached_calls": 0, "input_tokens": 10,
               "output_tokens": 5, "model_seconds": 0.1}
        rows = [row]
        return summarize_rows(rows, method=method, model=model, dataset="t", split="dev", total_items=1), rows

    runs = [run_of("noop", None, 5), run_of("zero_shot", "groq:openai/gpt-oss-120b", 5),
            run_of("agent", "groq:openai/gpt-oss-120b", 0), run_of("agent", "mistralai:mistral-small-2603", 0)]
    text = report_by_model(runs)
    groq, mistral = text.split("### mistral-small-2603")
    assert "### openai/gpt-oss-120b (groq)" in groq and "| zero_shot |" in groq and "| noop |" in groq
    assert "| zero_shot |" not in mistral and "| agent |" in mistral and "| noop |" in mistral
