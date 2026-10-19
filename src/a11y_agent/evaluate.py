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
    python -m a11y_agent.evaluate report                                # compare all runs
    python -m a11y_agent.evaluate report --save                         # also copy summaries to results/
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import sys
import time
from collections import Counter
from pathlib import Path

from bs4 import BeautifulSoup

from .benchmark import BenchmarkItem, load_split
from .config import ROOT, settings
from .methods import FixContext, FixResult, get_method
from .model_client import ModelClient, RateLimiter, ResponseCache, StopRun
from .scanner import Scanner
from .scoring import FixOutcome, evaluate_fix, summarize

RESULTS_DIR = ROOT / "results"

# Paid prices per million tokens (input, output) on Groq, October 2026.
# Used only to report what a run would cost on a paid plan.
PRICES = {
    "groq:openai/gpt-oss-120b": (0.15, 0.60),
    "groq:openai/gpt-oss-20b": (0.075, 0.30),
}


def model_slug(spec: str | None) -> str:
    return re.sub(r"[^A-Za-z0-9.-]+", "-", spec).strip("-") if spec else "none"


def run_dir(dataset: str, split: str, method: str, model: str | None) -> Path:
    return settings.runs_dir / f"{dataset}-{split}" / f"{method}__{model_slug(model)}"


# ---------------------------------------------------------------------------
# Did the fix keep the content?
# ---------------------------------------------------------------------------

def content_kept(original: str, fixed: str) -> tuple[float, float]:
    """(share of the original's words still present, share of its elements still present).

    The metric only asks "is the violation gone?". Deleting the offending
    elements makes it go, so we also measure how much of the original survives.
    Low values flag fixes that "work" by removing content.
    """
    def words(html: str) -> Counter:
        return Counter(BeautifulSoup(html, "html.parser").get_text(" ").lower().split())

    def tags(html: str) -> Counter:
        return Counter(t.name for t in BeautifulSoup(html, "html.parser").find_all(True))

    w0, t0 = words(original), tags(original)
    w1, t1 = words(fixed), tags(fixed)
    text = sum((w0 & w1).values()) / sum(w0.values()) if w0 else 1.0
    elements = sum((t0 & t1).values()) / sum(t0.values()) if t0 else 1.0
    return round(text, 3), round(elements, 3)


DROPPED_CONTENT = 0.9   # below this share of words or elements kept, a fix is flagged


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
        "fixed_html": result.html,
        "response": result.response,
    }


# ---------------------------------------------------------------------------
# A whole run
# ---------------------------------------------------------------------------

def _outcome(row: dict) -> FixOutcome:
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
    }
    summary["tokens_per_item"] = round((summary["input_tokens"] + summary["output_tokens"]) / max(len(rows), 1))
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

    async with Scanner() as scanner:
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
    head = ("| Method | Model | Items | Score decrease | Element-level only | Resolved | Broke something "
            "| Dropped content | Too large | Tokens per item | Paid cost |")
    lines = [head, "|" + "---|" * 11]
    for s in sorted(summaries, key=lambda s: (s["method"] != "noop", s["method"], s["model"] or "")):
        a, e = s["all"], s["element_level"]
        done = f"{s['items_done']}/{s['items_total']}" + ("" if s["complete"] else " (partial)")
        cost = "-" if s.get("paid_cost_usd") is None else f"${s['paid_cost_usd']:.2f}"
        lines.append(
            f"| {s['method']} | {(s['model'] or '-').split(':')[-1]} | {done} | {_fmt(a['score_decrease'])} "
            f"| {_fmt(e['score_decrease'])} | {a['resolved']}/{a['violations']} | {a['introduced_new_violations']} "
            f"| {s['dropped_content']} | {s['status'].get('too_large', 0)} | {s['tokens_per_item']} | {cost} |"
        )
    return "\n".join(lines)


def collect_runs(dataset: str, split: str) -> list[tuple[dict, list[dict]]]:
    """(summary, rows) for every run of a split."""
    base = settings.runs_dir / f"{dataset}-{split}"
    runs = []
    for summary_path in sorted(base.glob("*/summary.json")):
        results = summary_path.with_name("results.jsonl")
        rows = [json.loads(l) for l in results.read_text(encoding="utf-8").splitlines() if l.strip()] \
            if results.exists() else []
        runs.append((json.loads(summary_path.read_text(encoding="utf-8")), rows))
    return runs


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate repair methods on the benchmark.")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run one method over a split")
    run.add_argument("--method", required=True, choices=["noop", "zero_shot", "accessguru"])
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

    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    if args.command == "run":
        if args.split == "test" and args.dataset == "accessguru":
            print("Note: this is the TEST split. Use it for final numbers only, never for tuning.")
        items = load_split(args.dataset, args.split)
        if args.limit:
            items = items[: args.limit]
        summary = asyncio.run(run_evaluation(args.method, items, dataset=args.dataset, split=args.split,
                                             model_spec=args.model, concurrency=args.concurrency,
                                             fresh=args.fresh))
        print()
        print(report_table([summary]))
        if "stopped" in summary:
            return 2
        if not summary["complete"]:
            print("\nSome items failed (see the ! lines above). Run the same command again to retry them.")
            return 1
        return 0

    runs = collect_runs(args.dataset, args.split)
    if not runs:
        print(f"No runs found in {settings.runs_dir / f'{args.dataset}-{args.split}'}")
        return 1
    summaries = [s for s, _ in runs]
    table = "All items:\n\n" + report_table(summaries)
    common, common_summaries = common_subset(runs)
    if any(s["status"].get("too_large") for s in summaries):
        table += (f"\n\nOnly the {len(common)} items every run could send "
                  "(the fairest comparison on the free tier):\n\n" + report_table(common_summaries))
    print(table)
    if args.save:
        target = RESULTS_DIR / f"{args.dataset}-{args.split}"
        target.mkdir(parents=True, exist_ok=True)
        for s in summaries:
            name = f"{s['method']}__{model_slug(s['model'])}.json"
            (target / name).write_text(json.dumps(s, indent=2) + "\n", encoding="utf-8")
        (target / "README.md").write_text(
            f"# Results: {args.dataset} {args.split} split\n\n"
            "Made with `python -m a11y_agent.evaluate report --save`. One JSON summary per run.\n\n"
            + table + "\n", encoding="utf-8")
        print(f"\nSaved to {target.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
