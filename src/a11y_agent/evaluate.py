"""The evaluation harness: run a repair method over a benchmark split and score it.

For every item the harness does the same five things, whatever the method:

1. scan the original snippet (the "before" picture),
2. ask the method for a fix,
3. scan the fix (the "after" picture),
4. score it with the AccessGuru metric (``scoring.evaluate_fix``),
5. check how much of the original content the fix kept.

Results go to ``runs/<dataset>-<split>/<method>__<model>/``:

    results.jsonl   one line per item: scores, status, tokens, the fixed HTML, the raw reply
    summary.json    the headline numbers

Runs resume: items already in results.jsonl are skipped, and every model reply
is cached, so re-running a command after an interruption (or a quota stop)
continues where it left off and costs nothing for finished work.

Commands (from the repository root):

    python -m a11y_agent.evaluate run --method noop                     # harness check, no model
    python -m a11y_agent.evaluate run --method zero_shot --limit 5      # try 5 items
    python -m a11y_agent.evaluate run --method zero_shot                # whole dev split
    python -m a11y_agent.evaluate run --method accessguru --model groq:openai/gpt-oss-20b
    python -m a11y_agent.evaluate run --method agent --model mistralai:mistral-small-2603
    python -m a11y_agent.evaluate report                                # compare all runs, per model
    python -m a11y_agent.evaluate report --save                         # also copy summaries to results/
    python -m a11y_agent.evaluate run --method zero_shot agent_tools --split test    # several methods in turn
    python -m a11y_agent.evaluate compare --split test --save           # confidence intervals, paired tests, cost
    python -m a11y_agent.evaluate failures --method agent_guarded --split test --save   # error analysis
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import re
import shutil
import statistics
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

from .benchmark import BenchmarkItem, load_split
from .config import ROOT, settings
from .content import DROPPED_CONTENT, content_kept
from .methods import METHODS, FixContext, FixResult, get_method
from .model_client import ModelClient, RateLimiter, ResponseCache, StopRun
from .scanner import Scanner
from .scoring import FixOutcome, evaluate_fix, summarize

RESULTS_DIR = ROOT / "results"

# Paid prices per million tokens (input, output), October 2026.
# Used only to report what a run would cost on a paid plan.
PRICES = {
    "groq:openai/gpt-oss-120b": (0.15, 0.60),
    "groq:openai/gpt-oss-20b": (0.075, 0.30),
    "mistralai:mistral-small-2603": (0.15, 0.60),      # Mistral Small 4
    "mistralai:mistral-small-latest": (0.15, 0.60),
}


def code_version() -> str:
    """The git commit of the code that produced a result ("+changes" if src/ differs from it).

    Saved in every result row, so a run resumed after a code change shows up
    as a run with two versions instead of silently mixing them.
    """
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                                timeout=10).stdout.strip()
        changed = subprocess.run(["git", "diff", "--quiet", "HEAD", "--", "src"], cwd=ROOT, capture_output=True,
                                 timeout=10).returncode != 0
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return (commit or "unknown") + ("+changes" if commit and changed else "")


_CODE_VERSION: str | None = None


def model_slug(spec: str | None) -> str:
    return re.sub(r"[^A-Za-z0-9.-]+", "-", spec).strip("-") if spec else "none"


def run_dir(dataset: str, split: str, method: str, model: str | None) -> Path:
    return settings.runs_dir / f"{dataset}-{split}" / f"{method}__{model_slug(model)}"


# ---------------------------------------------------------------------------
# One item
# ---------------------------------------------------------------------------

async def evaluate_item(item: BenchmarkItem, method, scanner: Scanner, model: ModelClient | None) -> dict:
    before = await scanner.scan_html(item.html)
    started = time.perf_counter()
    if item.rule_id not in before.rule_ids:
        result = FixResult(html=None, status="not_reproduced",
                           note="the rule is not violated in the original snippet on this machine")
        outcome = None
    else:
        result = await method.fix(item, FixContext(model=model, scanner=scanner, before=before))
        if result.html is None:
            outcome = evaluate_fix(before, before, item.rule_id)        # no answer = not fixed
        else:
            after = await scanner.scan_html(result.html)
            outcome = evaluate_fix(before, after, item.rule_id)

    text_kept, elements_kept = content_kept(item.html, result.html) if result.html else (None, None)
    return {
        "id": item.id,
        "rule_id": item.rule_id,
        "category": item.category,
        "page_level": item.page_level,
        "status": result.status,
        "old_score": outcome.old_score if outcome else None,
        "new_score": outcome.new_score if outcome else None,
        "target_fixed": outcome.target_fixed if outcome else None,
        "resolved": outcome.resolved if outcome else None,
        "introduced": outcome.introduced if outcome else [],
        "text_kept": text_kept,
        "elements_kept": elements_kept,
        "dropped_content": bool(result.html is not None and result.html != item.html
                                and min(text_kept, elements_kept) < DROPPED_CONTENT),
        "passes": result.passes,
        "calls": len(result.calls),
        "cached_calls": sum(c.cached for c in result.calls),
        "input_tokens": sum(c.input_tokens for c in result.calls),
        "output_tokens": sum(c.output_tokens for c in result.calls),
        "model_seconds": round(sum(c.seconds for c in result.calls), 2),
        "seconds": round(time.perf_counter() - started, 2),
        "note": result.note,
        "guard": result.guard,
        "code": _CODE_VERSION,
        "fixed_html": result.html,
        "response": result.response,
    }


# ---------------------------------------------------------------------------
# A whole run
# ---------------------------------------------------------------------------

def _outcome(row: dict, *, deletions_fail: bool = False) -> FixOutcome:
    """The row's score; with ``deletions_fail``, a fix that removed content counts as no fix at all."""
    if deletions_fail and row.get("dropped_content"):
        return FixOutcome(row["rule_id"], row["old_score"], row["old_score"], False, row["introduced"])
    return FixOutcome(row["rule_id"], row["old_score"], row["new_score"], row["target_fixed"], row["introduced"])


def summarize_rows(rows: list[dict], *, method: str, model: str | None, dataset: str, split: str,
                   total_items: int) -> dict:
    scored = [r for r in rows if r["old_score"] is not None]
    element_level = [r for r in scored if not r["page_level"]]
    fresh_in = sum(r["input_tokens"] for r in rows if r["calls"] > r["cached_calls"])
    fresh_out = sum(r["output_tokens"] for r in rows if r["calls"] > r["cached_calls"])
    price = PRICES.get(model or "")
    summary = {
        "method": method,
        "model": model,
        "dataset": dataset,
        "split": split,
        "complete": len(rows) == total_items,
        "items_done": len(rows),
        "items_total": total_items,
        "all": summarize(_outcome(r) for r in scored),
        # The metric alone rewards deleting the broken element; here a deletion scores as no fix.
        "deletions_fail": summarize(_outcome(r, deletions_fail=True) for r in scored),
        "element_level": summarize(_outcome(r) for r in element_level),
        "by_category": {cat: summarize(_outcome(r) for r in scored if r["category"] == cat)
                        for cat in sorted({r["category"] for r in scored})},
        "status": dict(Counter(r["status"] for r in rows).most_common()),
        "dropped_content": sum(r["dropped_content"] for r in rows),
        "calls": sum(r["calls"] for r in rows),
        "input_tokens": sum(r["input_tokens"] for r in rows),
        "output_tokens": sum(r["output_tokens"] for r in rows),
        "model_seconds": round(sum(r["model_seconds"] for r in rows), 1),
        "paid_cost_usd": round((fresh_in * price[0] + fresh_out * price[1]) / 1e6, 4) if price else None,
        "axe_version": settings.axe_version,
        "code_versions": sorted({r["code"] for r in rows if r.get("code")}),
    }
    summary["tokens_per_item"] = round((summary["input_tokens"] + summary["output_tokens"]) / max(len(rows), 1))
    guarded = [r["guard"] for r in rows if r.get("guard")]
    if guarded:
        rejections, flags = Counter(), Counter()
        for g in guarded:
            rejections.update(g.get("rejected") or {})
            flags.update(g.get("flagged") or {})
        summary["guardrails"] = {
            "items": len(guarded),
            "blocked": sum(bool(g.get("blocked")) for g in guarded),
            "items_with_rejections": sum(bool(g.get("rejected")) for g in guarded),
            "rejections": dict(rejections.most_common()),
            "items_flagged": sum(bool(g.get("flagged")) for g in guarded),
            "flags": dict(flags.most_common()),
        }
    return summary


async def run_evaluation(method_name: str, items: list[BenchmarkItem], *, dataset: str, split: str,
                         model_spec: str | None = None, out_dir: Path | None = None, concurrency: int = 1,
                         fresh: bool = False, llm=None, cache: ResponseCache | None = None,
                         limiter: RateLimiter | None = None, quiet: bool = False) -> dict:
    """Evaluate one method on the given items; returns the summary.

    ``llm``, ``cache`` and ``limiter`` can be passed in (tests use a fake
    model); by default the configured model, ``runs/cache.sqlite`` and the
    free-tier pacing from .env are used.
    """
    global _CODE_VERSION
    if _CODE_VERSION is None:
        _CODE_VERSION = code_version()
    method = get_method(method_name)
    spec = (model_spec or settings.model) if method.uses_model else None
    out = Path(out_dir or run_dir(dataset, split, method_name, spec))
    if fresh and out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)
    results_path = out / "results.jsonl"

    done: dict[str, dict] = {}
    if results_path.exists():
        for line in results_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                done[row["id"]] = row
    todo = [i for i in items if i.id not in done]

    model = None
    if method.uses_model:
        model = ModelClient(spec, llm=llm, limiter=limiter,
                            cache=cache if cache is not None else ResponseCache(settings.runs_dir / "cache.sqlite"))

    def log(msg: str) -> None:
        if not quiet:
            print(msg, flush=True)

    log(f"{method_name} on {dataset}/{split} with {spec or 'no model'}: "
        f"{len(items)} items, {len(done)} already done, {len(todo)} to run")

    stop: StopRun | None = None
    write_lock = asyncio.Lock()
    semaphore = asyncio.Semaphore(max(1, concurrency))

    # A method that needs setup (agent_tools starts the MCP server) is an async context manager.
    lifecycle = method if hasattr(method, "__aenter__") else contextlib.nullcontext()
    async with lifecycle, Scanner() as scanner:
        async def one(item: BenchmarkItem) -> None:
            nonlocal stop
            async with semaphore:
                if stop is not None:
                    return
                try:
                    row = await evaluate_item(item, method, scanner, model)
                except StopRun as e:
                    stop = stop or e
                    return
                except Exception as e:      # network trouble etc.: not saved, so the next run retries it
                    log(f"  ! {item.id}: {type(e).__name__}: {str(e).splitlines()[0][:160]} (will retry next run)")
                    return
                async with write_lock:
                    with open(results_path, "a", encoding="utf-8") as f:
                        f.write(json.dumps(row, ensure_ascii=False) + "\n")
                    done[item.id] = row
                    score = "-" if row["old_score"] is None else f"{row['old_score']}->{row['new_score']}"
                    cached = " (cached)" if row["calls"] and row["calls"] == row["cached_calls"] else ""
                    log(f"  [{len(done):3}/{len(items)}] {item.id:16} {item.rule_id:28} {score:6} "
                        f"{row['status']}{cached}")

        await asyncio.gather(*(one(i) for i in todo))

    rows = [done[i.id] for i in items if i.id in done]
    summary = summarize_rows(rows, method=method_name, model=spec, dataset=dataset, split=split,
                             total_items=len(items))
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    if stop is not None:
        summary["stopped"] = str(stop)
        log(f"\nStopped: {stop}\n{len(rows)}/{len(items)} items are saved. Run the same command again later "
            "to continue; finished items and cached replies are reused.")
    return summary


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def _fmt(x, pct=False) -> str:
    if x is None:
        return "-"
    return f"{x:.2f}" if not pct else f"{100 * x:.0f}%"


def report_table(summaries: list[dict]) -> str:
    """Markdown table comparing runs."""
    head = ("| Method | Model | Items | Score decrease | Deletions count as failures | Element-level only | Resolved "
            "| Broke something | Dropped content | Too large | Tokens per item | Paid cost |")
    lines = [head, "|" + "---|" * 12]
    for s in sorted(summaries, key=lambda s: (s["method"] != "noop", s["method"], s["model"] or "")):
        a, e = s["all"], s["element_level"]
        strict = (s.get("deletions_fail") or {}).get("score_decrease")
        done = f"{s['items_done']}/{s['items_total']}" + ("" if s["complete"] else " (partial)")
        cost = "-" if s.get("paid_cost_usd") is None else f"${s['paid_cost_usd']:.2f}"
        lines.append(
            f"| {s['method']} | {(s['model'] or '-').split(':')[-1]} | {done} | {_fmt(a['score_decrease'])} "
            f"| {_fmt(strict)} | {_fmt(e['score_decrease'])} | {a['resolved']}/{a['violations']} | {a['introduced_new_violations']} "
            f"| {s['dropped_content']} | {s['status'].get('too_large', 0)} | {s['tokens_per_item']} | {cost} |"
        )
    return "\n".join(lines)


def guard_table(summaries: list[dict]) -> str:
    """Guardrail statistics for the runs that used them: what was stopped, rejected and flagged."""
    lines = ["| Method | Model | Pages blocked at input | Items with a rejected attempt | Rejected attempts by reason "
             "| Changes flagged for review | Flags by kind |", "|" + "---|" * 7]
    for s in summaries:
        g = s.get("guardrails")
        if not g:
            continue
        reasons = ", ".join(f"{k} {v}" for k, v in g["rejections"].items()) or "none"
        flags = ", ".join(f"{k} {v}" for k, v in g["flags"].items()) or "none"
        lines.append(f"| {s['method']} | {(s['model'] or '-').split(':')[-1]} | {g['blocked']}/{g['items']} "
                     f"| {g['items_with_rejections']}/{g['items']} | {reasons} | {g['items_flagged']}/{g['items']} "
                     f"| {flags} |")
    return "\n".join(lines) if len(lines) > 2 else ""


def collect_runs(dataset: str, split: str) -> list[tuple[dict, list[dict]]]:
    """(summary, rows) for every run of a split."""
    base = settings.runs_dir / f"{dataset}-{split}"
    runs = []
    for summary_path in sorted(base.glob("*/summary.json")):
        results = summary_path.with_name("results.jsonl")
        rows = [json.loads(l) for l in results.read_text(encoding="utf-8").splitlines() if l.strip()] \
            if results.exists() else []
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        if rows:      # re-score from the saved rows, so reports always use the current scoring code
            summary = summarize_rows(rows, method=summary["method"], model=summary["model"],
                                     dataset=summary["dataset"], split=summary["split"],
                                     total_items=summary["items_total"])
        runs.append((summary, rows))
    return runs


ITEM_COLUMNS = ["id", "rule_id", "category", "page_level", "status", "old_score", "new_score", "target_fixed",
                "resolved", "introduced", "dropped_content", "text_kept", "elements_kept", "passes", "calls",
                "input_tokens", "output_tokens", "note", "guard", "trace"]


def _guard_text(guard: dict | None) -> str:
    """One readable cell: "blocked; rejected removes_content 1; flagged added_visible_text 1"."""
    if not guard:
        return ""
    parts = ["blocked"] if guard.get("blocked") else []
    if guard.get("rejected"):
        parts.append("rejected " + ", ".join(f"{k} {v}" for k, v in guard["rejected"].items()))
    if guard.get("flagged"):
        parts.append("flagged " + ", ".join(f"{k} {v}" for k, v in guard["flagged"].items()))
    return "; ".join(parts) or "no objections"


def write_items_csv(path: Path, rows: list[dict], method: str) -> None:
    """One line per item, for error analysis: outcome, cost, and (for the agent) its step trace.

    No snippets or fixed pages: those stay in runs/ with the downloaded data.
    """
    import csv
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=ITEM_COLUMNS)
        writer.writeheader()
        for row in rows:
            out = {k: row.get(k) for k in ITEM_COLUMNS if k not in ("introduced", "trace", "guard")}
            out["introduced"] = ";".join(row.get("introduced") or [])
            out["guard"] = _guard_text(row.get("guard"))
            out["trace"] = (row.get("response") or "")[:3000] if method.startswith("agent") else ""
            writer.writerow(out)


def _aligned(runs: list[tuple[dict, list[dict]]]) -> tuple[list[str], dict[str, dict[str, dict]]]:
    """Item ids scored by every run (in a fixed order), and each run's rows by id."""
    by_run = {f"{s['method']}|{s['model']}": {r["id"]: r for r in rows if r["old_score"] is not None}
              for s, rows in runs}
    common = set.intersection(*(set(v) for v in by_run.values())) if by_run else set()
    return sorted(common), by_run


def compare_table(runs: list[tuple[dict, list[dict]]], baseline: str) -> str:
    """Each method's score with a 95% interval, and a paired comparison with ``baseline``.

    Only items every run scored are used, in the same order for every run, so
    each comparison is paired. Scores count deletions as failures.
    """
    from . import stats
    ids, by_run = _aligned(runs)
    keys = sorted(by_run, key=lambda k: (k.split("|")[0] != "noop", k))
    base_key = next((k for k in keys if k.split("|")[0] == baseline), None)
    head = (f"| Method | Model | Items | Score (deletions = failures) | 95% interval | vs {baseline} "
            "| 95% interval of the difference | Resolved here, not there / there, not here | Sign test p |")
    lines = [head, "|" + "---|" * 9]
    for key in keys:
        method, model = key.split("|")
        rows = [by_run[key][i] for i in ids]
        score = stats.score_decrease(rows)
        low, high = stats.bootstrap_interval(rows)
        if base_key is None or key == base_key:
            rest = "| - | - | - | - |"
        else:
            base = [by_run[base_key][i] for i in ids]
            diff, dlow, dhigh = stats.paired_difference(rows, base)
            wins, losses = stats.paired_flips(rows, base)
            rest = f"| {diff:+.2f} | {dlow:+.2f} to {dhigh:+.2f} | {wins} / {losses} | {stats.sign_test(wins, losses):.2f} |"
        lines.append(f"| {method} | {model.split(':')[-1] if model != 'None' else '-'} | {len(rows)} | {score:.2f} "
                     f"| {low:.2f} to {high:.2f} {rest}")
    return "\n".join(lines)


def cost_table(runs: list[tuple[dict, list[dict]]]) -> str:
    """What each method costs per page: model calls, tokens, time, and money on a paid plan."""
    lines = ["| Method | Model | Model calls per item | Tokens per item | Model time per item (median) "
             "| Wall time per item (median / 90th percentile) | Replies from the cache | Paid plan, per 100 pages |",
             "|" + "---|" * 8]
    for s, rows in sorted(runs, key=lambda r: (r[0]["method"] != "noop", r[0]["method"], r[0]["model"] or "")):
        if not rows:
            continue
        n = len(rows)
        calls = sum(r["calls"] for r in rows)
        cached = sum(r.get("cached_calls", 0) for r in rows)
        walls = sorted(r["seconds"] for r in rows)
        p90 = walls[min(n - 1, int(0.9 * n))]
        price = PRICES.get(s["model"] or "")
        cost = "-" if not price else \
            f"${100 * sum(r['input_tokens'] * price[0] + r['output_tokens'] * price[1] for r in rows) / 1e6 / n:.2f}"
        lines.append(f"| {s['method']} | {(s['model'] or '-').split(':')[-1]} | {calls / n:.1f} "
                     f"| {round(sum(r['input_tokens'] + r['output_tokens'] for r in rows) / n):,} "
                     f"| {statistics.median(r['model_seconds'] for r in rows):.1f} s "
                     f"| {statistics.median(walls):.1f} s / {p90:.1f} s | {cached}/{calls} | {cost} |")
    return "\n".join(lines)


FAILURE_KINDS = {
    "blocked": "blocked at the input screen",
    "too_large": "request too large to send",
    "deleted": "deleted content",
    "broke": "fixed the target but broke another rule",
    "guard": "every attempt rejected by the guardrails",
    "no_match": "edits did not match the page",
    "unusable": "no usable reply",
    "still_fails": "the target still fails",
    "partial": "fixed some of the failing elements",
}


def failure_kind(row: dict) -> str:
    """Why an item was not resolved, from its outcome and (for the agent) the last steps of its trace."""
    steps = [line for line in (row.get("response") or "").splitlines()
             if line.startswith(("propose:", "guard:", "verify:"))]
    last = steps[-1] if steps else ""
    if row["status"] == "blocked":
        return "blocked"
    if row["status"] == "too_large":
        return "too_large"
    if row.get("dropped_content"):
        return "deleted"
    if row.get("target_fixed") and row.get("introduced"):
        return "broke"
    if row["new_score"] is not None and row["old_score"] and row["new_score"] < row["old_score"]:
        return "partial"
    if last.startswith("guard:"):
        return "guard"
    if "could not be applied" in last or "did not change the HTML" in last:
        return "no_match"
    if "required format" in last or "unusable reply" in last or row["status"] in ("no_html", "truncated"):
        return "unusable"
    if "made another rule fail" in last:
        return "broke"
    return "still_fails"


def failures_report(rows: list[dict], method: str, limit: int = 20) -> str:
    """The failures of one run: how many of each kind, then the first ``limit`` with their last problem."""
    failed = [r for r in rows if r["old_score"] is not None and not (r["resolved"] and not r.get("dropped_content"))]
    kinds = Counter(failure_kind(r) for r in failed)
    lines = [f"{len(failed)} of {sum(r['old_score'] is not None for r in rows)} items not resolved by {method}.", "",
             "| Why | Items |", "|---|---|"]
    lines += [f"| {FAILURE_KINDS[k]} | {n} |" for k, n in kinds.most_common()]
    lines += ["", "| Item | Rule | Page-level | Why | Last step | Guardrails |", "|---|---|---|---|---|---|"]
    for r in failed[:limit]:
        steps = [l for l in (r.get("response") or "").splitlines() if l.startswith(("propose:", "guard:", "verify:"))]
        last = re.sub(r"; tools: .*?…: ", ": ", steps[-1] if steps else r.get("note") or "")   # tool calls aside
        last = re.sub(r"\s+", " ", last)[:220].replace("|", "/")
        lines.append(f"| {r['id']} | {r['rule_id']} | {'yes' if r['page_level'] else 'no'} | "
                     f"{FAILURE_KINDS[failure_kind(r)]} | {last} | {_guard_text(r.get('guard')) or '-'} |")
    return "\n".join(lines)


def common_subset(runs: list[tuple[dict, list[dict]]]) -> tuple[set[str], list[dict]]:
    """Re-score every run on the items that *all* runs attempted.

    On the free tier some requests are too large to send (long snippets, or
    AccessGuru's growing conversation). Counting those as failures would
    penalise a method for our quota, not its quality, so the report also
    compares methods on the items every run could actually attempt.
    """
    attempted = [{r["id"] for r in rows if r["status"] not in ("too_large", "not_reproduced")}
                 for _, rows in runs]
    common = set.intersection(*attempted) if attempted else set()
    summaries = []
    for summary, rows in runs:
        kept = [r for r in rows if r["id"] in common]
        summaries.append(summarize_rows(kept, method=summary["method"], model=summary["model"],
                                        dataset=summary["dataset"], split=summary["split"],
                                        total_items=len(common)))
    return common, summaries


def report_by_model(runs: list[tuple[dict, list[dict]]]) -> str:
    """One section per model: methods are only compared on the same model.

    The model-free ``noop`` run appears in every section as the floor.
    """
    models = sorted({s["model"] for s, _ in runs if s["model"]})
    floor = [r for r in runs if not r[0]["model"]]
    sections = []
    for model in models or [None]:
        group = floor + [r for r in runs if r[0]["model"] == model] if model else floor
        summaries = [s for s, _ in group]
        text = (f"### {model.split(':', 1)[-1]} ({model.split(':', 1)[0]})\n\n" if model else "") + \
            "All items:\n\n" + report_table(summaries)
        if any(s["status"].get("too_large") for s in summaries):
            common, common_summaries = common_subset(group)
            text += (f"\n\nOnly the {len(common)} items every run could send "
                     "(the fairest comparison when some requests were too large):\n\n" + report_table(common_summaries))
        guards = guard_table(summaries)
        if guards:
            text += "\n\nGuardrails (Phase 5): what they stopped, rejected and flagged:\n\n" + guards
        sections.append(text)
    return "\n\n".join(sections)


def _shown(path: Path) -> str:
    """A path relative to the repository when it is inside it."""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate repair methods on the benchmark.")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run one method over a split")
    run.add_argument("--method", required=True, nargs="+", choices=sorted(METHODS),
                     help="one or more methods, run one after another")
    run.add_argument("--dataset", default="accessguru", choices=["accessguru", "access"])
    run.add_argument("--split", default="dev", choices=["dev", "test"])
    run.add_argument("--model", help="provider:model (default: MODEL from .env)")
    run.add_argument("--limit", type=int, help="only the first N items (quick trial)")
    run.add_argument("--concurrency", type=int, default=1, help="items in parallel (keep 1 on the free tier)")
    run.add_argument("--fresh", action="store_true", help="discard this run's saved results first")

    rep = sub.add_parser("report", help="compare all runs for a split")
    rep.add_argument("--dataset", default="accessguru", choices=["accessguru", "access"])
    rep.add_argument("--split", default="dev", choices=["dev", "test"])
    rep.add_argument("--save", action="store_true", help="copy summaries to results/ (to commit)")
    rep.add_argument("--model", help="only runs with this provider:model")

    cmp = sub.add_parser("compare", help="confidence intervals, paired tests against a baseline, cost and latency")
    cmp.add_argument("--dataset", default="accessguru", choices=["accessguru", "access"])
    cmp.add_argument("--split", default="dev", choices=["dev", "test"])
    cmp.add_argument("--model", help="only runs with this provider:model (default: MODEL from .env)")
    cmp.add_argument("--baseline", default="agent_tools", help="the method every other method is compared with")
    cmp.add_argument("--save", action="store_true", help="write results/<dataset>-<split>/comparison.md")

    fail = sub.add_parser("failures", help="why a method's items were not resolved (error analysis)")
    fail.add_argument("--method", required=True, choices=sorted(METHODS))
    fail.add_argument("--dataset", default="accessguru", choices=["accessguru", "access"])
    fail.add_argument("--split", default="dev", choices=["dev", "test"])
    fail.add_argument("--model", help="provider:model (default: MODEL from .env)")
    fail.add_argument("-n", type=int, default=20, help="how many failures to list")
    fail.add_argument("--save", action="store_true", help="write results/<dataset>-<split>/failures__<method>.md")

    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    if args.command == "run":
        if args.split == "test" and args.dataset == "accessguru":
            print("Note: this is the TEST split. Use it for final numbers only, never for tuning.")
        items = load_split(args.dataset, args.split)
        if args.limit:
            items = items[: args.limit]
        summaries = []
        for method in args.method:
            summary = asyncio.run(run_evaluation(method, items, dataset=args.dataset, split=args.split,
                                                 model_spec=args.model, concurrency=args.concurrency,
                                                 fresh=args.fresh))
            summaries.append(summary)
            print()
            if "stopped" in summary:
                print(report_table(summaries))
                return 2
        print(report_table(summaries))
        if not all(s["complete"] for s in summaries):
            print("\nSome items failed (see the ! lines above). Run the same command again to retry them.")
            return 1
        return 0

    if args.command in ("compare", "failures"):
        model = args.model or settings.model
        runs = [r for r in collect_runs(args.dataset, args.split) if r[0]["model"] in (model, None) and r[1]]
        target = RESULTS_DIR / f"{args.dataset}-{args.split}"
        if args.command == "failures":
            run = next((r for r in runs if r[0]["method"] == args.method and r[0]["model"]), None) or \
                next((r for r in runs if r[0]["method"] == args.method), None)
            if run is None:
                print(f"No {args.method} run with {model} on {args.dataset}/{args.split}.")
                return 1
            text = failures_report(run[1], args.method, args.n)
            print(text)
            if args.save:
                target.mkdir(parents=True, exist_ok=True)
                path = target / f"failures__{args.method}__{model_slug(run[0]['model'])}.md"
                path.write_text(f"# Failures: {args.method} on {args.dataset} {args.split} ({model})\n\n"
                                "Made with `python -m a11y_agent.evaluate failures --save`.\n\n" + text + "\n",
                                encoding="utf-8")
                print(f"\nSaved to {_shown(path)}")
            return 0
        if not runs:
            print(f"No runs with {model} on {args.dataset}/{args.split}.")
            return 1
        versions = sorted({v for s, _ in runs for v in s.get("code_versions", [])})
        mixed = [s["method"] for s, _ in runs if len(s.get("code_versions", [])) > 1]
        text = (compare_table(runs, args.baseline) + "\n\nCost and speed:\n\n" + cost_table(runs)
                + f"\n\nCode versions: {', '.join(versions) or 'not recorded'}"
                + (f". Runs that mix versions (resumed after a code change): {', '.join(mixed)}" if mixed else "")
                + ".")
        print(text)
        if args.save:
            target.mkdir(parents=True, exist_ok=True)
            (target / "comparison.md").write_text(
                f"# Comparison: {args.dataset} {args.split} ({model})\n\n"
                "Made with `python -m a11y_agent.evaluate compare --save`. Items every run scored; deletions count "
                f"as failures; 95% intervals from {10000:,} bootstrap resamples of the items (paired for differences); "
                "sign test on items one method resolved and the other did not.\n\n" + text + "\n", encoding="utf-8")
            print(f"\nSaved to {_shown(target / 'comparison.md')}")
        return 0

    all_runs = collect_runs(args.dataset, args.split)
    runs = [r for r in all_runs if r[0]["model"] in (args.model, None)] if args.model else all_runs
    if not runs:
        print(f"No runs found in {settings.runs_dir / f'{args.dataset}-{args.split}'}")
        return 1
    print(report_by_model(runs))
    if args.save:                                  # results/ always gets every model
        table = report_by_model(all_runs)
        target = RESULTS_DIR / f"{args.dataset}-{args.split}"
        target.mkdir(parents=True, exist_ok=True)
        for s, rows in all_runs:
            name = f"{s['method']}__{model_slug(s['model'])}"
            (target / f"{name}.json").write_text(json.dumps(s, indent=2) + "\n", encoding="utf-8")
            if rows:
                write_items_csv(target / f"items__{name}.csv", rows, s["method"])
        (target / "README.md").write_text(
            f"# Results: {args.dataset} {args.split} split\n\n"
            "Made with `python -m a11y_agent.evaluate report --save`. One JSON summary per run, and "
            "`items__*.csv` with one line per item (outcome, cost, and the agent's step trace).\n\n"
            "*Deletions count as failures*: the same score, but a fix that removed more than a tenth of the "
            "content scores as no fix.\n\n"
            + table + "\n", encoding="utf-8")
        print(f"\nSaved to {_shown(target)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
