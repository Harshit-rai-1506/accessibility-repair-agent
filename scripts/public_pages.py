"""Before and after: the guarded agent on real public pages (Phase 6).

    python scripts/public_pages.py                          # every page in scripts/public_pages.txt
    python scripts/public_pages.py --list my_pages.txt      # another list
    python scripts/public_pages.py https://www.example.com https://another.example.org
    python scripts/public_pages.py --fresh                  # redo pages that already have a report
    python scripts/public_pages.py https://uidai.gov.in/ --fresh    # redo one page

For each address:

1. load the page in Chromium and keep the HTML as the browser built it (after
   its scripts ran), in runs/public/<name>.html. Slow sites get up to a minute;
   whatever has arrived by then is used;
2. skip it, with the reason, if the site answered with an error or a bot check
   instead of the page;
3. scan that snapshot, then let the agent fix it, with tools and guardrails
   (input screen, output checks, automatic approval with review flags);
4. write results/public-pages/<name>.md (the score before and after, every
   rule with what happened to it, the guardrails' rejections and flags, the
   cost, the start of the diff) and <name>.json (the same numbers, for the
   summary). The fixed page goes to runs/public/;
5. rewrite results/public-pages/README.md: one table over every page done so
   far. The run can be split over several sittings: a page with a report is
   skipped unless --fresh, and one that failed to load is tried again.

Scores are for the saved snapshot, scanned like the benchmark (network
blocked), before and after, so the two are comparable. Without the page's
stylesheets and images some results (colour contrast especially) differ from
the live site, whose own score is shown for reference.

Only the snapshot on your machine is changed: nothing is sent to the sites
beyond the one page load a browser would make.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import sys
import time
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from a11y_agent.agent import AgentContext, build_graph, compact_scan, run_config, unified_diff  # noqa: E402
from a11y_agent.config import settings  # noqa: E402
from a11y_agent.guardrails import Guardrails, PromptGuard  # noqa: E402
from a11y_agent.model_client import ModelClient, ResponseCache, StopRun  # noqa: E402
from a11y_agent.scanner import Scanner  # noqa: E402
from a11y_agent.scoring import violation_score  # noqa: E402

RESULTS_DIR = ROOT / "results" / "public-pages"
DEFAULT_LIST = ROOT / "scripts" / "public_pages.txt"
DIFF_LINES = 40
LOAD_TIMEOUT_MS = 60_000
MAX_RULES = 15

# What a site shows instead of the page when it turns automated browsers away.
BOT_CHECK = re.compile(
    r"access denied|captcha|are you (a )?(human|robot)|verify (that )?you are (a )?human|unusual traffic|"
    r"request (was |has been )?blocked|attention required|just a moment|pardon our interruption|robot check|"
    r"please enable (javascript|cookies)|security check", re.I)
FIXED = ("fixed", "fixed_by_earlier_fix")
RETRY = ("load_failed", "error")          # tried again on the next run; every other page is done


def page_name(url: str) -> str:
    parsed = urlparse(url)
    return re.sub(r"[^A-Za-z0-9.-]+", "-", f"{parsed.hostname or 'page'}{parsed.path}").strip("-")[:80] or "page"


def read_list(path: Path) -> list[tuple[str, str]]:
    """(url, category) pairs: one page per line, the URL then an optional category; # starts a comment line."""
    pages = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        url, *category = line.split(maxsplit=1)
        pages.append((url, category[0].strip() if category else "-"))
    return pages


def page_text(html: str) -> str:
    """Roughly the text a visitor sees: no scripts, styles or tags."""
    html = re.sub(r"(?is)<(script|style|noscript|template|svg)\b.*?</\1\s*>", " ", html)
    return " ".join(re.sub(r"(?s)<[^>]+>", " ", html).split())


def site_problem(html: str, status: int | None) -> str | None:
    """Why this snapshot is not the real page, or None. A short page that talks about robots is a bot check."""
    if status is not None and status >= 400:
        return f"the site answered HTTP {status}"
    text = page_text(html)
    if len(text) < 2000 and BOT_CHECK.search(text):
        return f'the site showed a bot check instead of the page ("{BOT_CHECK.search(text).group(0)}")'
    if len(text) < 20:
        return "the page had almost no text when it was loaded"
    return None


def report(url: str, live_score: int, state: dict, model: str) -> str:
    s, g = state["summary"], state["summary"].get("guardrails", {})
    lines = [f"# {url}", "",
             f"Checked {date.today().isoformat()} with {model}, agent with tools and guardrails "
             "(`scripts/public_pages.py`).", "",
             f"- **Violation score: {s['initial_score']} -> {s['final_score']}** on the saved snapshot "
             f"(the live page scored {live_score} with its stylesheets and images).",
             f"- Model calls: {s['model_calls']}, about {s['tokens']:,} tokens.",
             f"- Guardrails: input screen {state.get('screening', {}).get('suspicious') and 'flagged the page' or 'found nothing'}; "
             f"rejected attempts: {', '.join(f'{k} {v}' for k, v in g.get('rejected', {}).items()) or 'none'}.", "",
             "| Rule | What happened | Attempts |", "|---|---|---|"]
    for o in state.get("outcomes", []):
        lines.append(f"| {o['rule_id']} | {o['status'].replace('_', ' ')} | {o.get('attempts', 0)} |")
    flags = (state.get("approval") or {}).get("flags") or []
    if flags:
        lines += ["", "For a person to check before using the fix:", ""] + [f"- {f['message']}" for f in flags]
    if s["remaining_rules"]:
        lines += ["", f"Still failing: {', '.join(s['remaining_rules'])}."]
    diff = unified_diff(state["html"], state["current_html"], limit=DIFF_LINES)
    if diff:
        lines += ["", f"The first {DIFF_LINES} lines of the change:", "", "```diff", diff, "```"]
    return "\n".join(lines) + "\n"


def record(url: str, category: str, model: str, **fields) -> dict:
    """One page's numbers, saved as <name>.json for the summary table."""
    return {"url": url, "name": page_name(url), "category": category, "checked": date.today().isoformat(),
            "model": model, **fields}


def repaired_record(url: str, category: str, model: str, snapshot, scan, state: dict, seconds: float) -> dict:
    """``snapshot``: the live page; ``scan``: its saved HTML scanned offline, as the agent started from it."""
    s, g = state["summary"], state["summary"].get("guardrails", {})
    outcomes = state.get("outcomes", [])
    return record(url, category, model, status="blocked_at_input" if g.get("blocked") else "repaired",
                  http_status=snapshot.status, html_kb=round(len(snapshot.html) / 1024),
                  live_score=violation_score(snapshot.scan), live_rules=len(snapshot.scan.violations),
                  before_score=s["initial_score"], after_score=s["final_score"],
                  before_rules=len(scan.violations), after_rules=len(s["remaining_rules"]),
                  attempted=sum(o["status"] not in ("skipped_limit", "not_found") for o in outcomes),
                  fixed=sum(o["status"] in FIXED for o in outcomes),
                  improved=sum(o["status"] == "improved" for o in outcomes),
                  outcomes=[{"rule_id": o["rule_id"], "status": o["status"], "attempts": o.get("attempts", 0)}
                            for o in outcomes],
                  remaining_rules=s["remaining_rules"], model_calls=s["model_calls"], tokens=s["tokens"],
                  seconds=round(seconds), rejected=g.get("rejected", {}), flags=g.get("flagged", {}),
                  input_screen="flagged" if (state.get("screening") or {}).get("suspicious") else "nothing found")


def _decrease(r: dict) -> str:
    return f"{1 - r['after_score'] / r['before_score']:.2f}" if r.get("before_score") else "-"


def summary(records: list[dict]) -> str:
    """results/public-pages/README.md: every page in one table, then the ones that could not be tested."""
    done = [r for r in records if r["status"] in ("repaired", "blocked_at_input", "nothing_to_fix")]
    skipped = [r for r in records if r not in done]
    lines = ["# Real public pages: before and after", "",
             "Made by `python scripts/public_pages.py`: the agent with tools and guardrails (the `agent_guarded` "
             "method) on a snapshot of each home page. Scores are axe-core's violation score (impact-weighted, as "
             "in the benchmark) on the snapshot, scanned offline before and after the fix; the live score, with "
             "the site's stylesheets and images, is shown for reference. One page per site, so these are "
             "examples, not a sample.", "",
             "| Site | Category | Live score | Rules failing (snapshot) | Score before → after | Decrease | "
             "Rules fixed / tried | Still failing | Rejected attempts | Review flags | Tokens | Time |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(done, key=lambda r: (r["category"], r["url"])):
        rejected = sum(r.get("rejected", {}).values())
        flags = sum(r.get("flags", {}).values())
        if r["status"] == "nothing_to_fix":
            lines.append(f"| [{r['name']}]({r['name']}.md) | {r['category']} | {r['live_score']} | 0 | 0 → 0 | - | "
                         "- | - | - | - | 0 | - |")
            continue
        lines.append(
            f"| [{r['name']}]({r['name']}.md) | {r['category']} | {r['live_score']} | {r['before_rules']} | "
            f"{r['before_score']} → {r['after_score']} | {_decrease(r)} | {r['fixed']} / {r['attempted']} | "
            f"{r['after_rules']} | {rejected} | {flags} | {r['tokens']:,} | {r['seconds'] // 60}:{r['seconds'] % 60:02d} |")
    scored = [r for r in done if r.get("before_score")]
    if scored:
        before, after = sum(r["before_score"] for r in scored), sum(r["after_score"] for r in scored)
        lines += ["", f"Over {len(scored)} page(s) with violations: violation score {before} → {after} "
                  f"(decrease {1 - after / before:.2f}; median per page "
                  f"{statistics.median(1 - r['after_score'] / r['before_score'] for r in scored):.2f}). "
                  f"Rules fixed: {sum(r['fixed'] for r in scored)} of {sum(r['attempted'] for r in scored)} tried. "
                  f"Blocked at the input screen: {sum(r['status'] == 'blocked_at_input' for r in scored)}."]
        rules: dict[str, list[str]] = {}
        for r in scored:
            for o in r["outcomes"]:
                rules.setdefault(o["rule_id"], []).append(o["status"])
        lines += ["", "By rule, over all pages:", "", "| Rule | Pages | Fixed | Improved | Not fixed |",
                  "|---|---|---|---|---|"]
        for rule, statuses in sorted(rules.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            tried = [s for s in statuses if s not in ("skipped_limit", "not_found")]
            fixed = sum(s in FIXED for s in tried)
            improved = sum(s == "improved" for s in tried)
            lines.append(f"| {rule} | {len(statuses)} | {fixed} | {improved} | {len(tried) - fixed - improved} |")
    if skipped:
        lines += ["", "Not tested:", ""] + [f"- {r['url']} ({r['category']}): {r['note']}" for r in skipped]
    return "\n".join(lines) + "\n"


def write_summary(results_dir: Path) -> None:
    records = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(results_dir.glob("*.json"))]
    (results_dir / "README.md").write_text(summary(records), encoding="utf-8")


async def repair(urls, model_spec: str | None, max_rules: int = MAX_RULES, *, results_dir: Path = RESULTS_DIR,
                 runs_dir: Path | None = None, llm=None, fresh: bool = False) -> int:
    """Snapshot, fix and report each page. ``urls``: addresses, or (address, category) pairs.

    ``llm`` (a stand-in model) and the folders are for tests.
    """
    from a11y_agent.mcp_client import MODEL_TOOLS, connect
    from a11y_agent.model_client import RateLimiter
    pages = [(u, "-") if isinstance(u, str) else tuple(u) for u in urls]
    runs_dir = runs_dir or settings.runs_dir
    out = runs_dir / "public"
    out.mkdir(parents=True, exist_ok=True)
    results_dir.mkdir(parents=True, exist_ok=True)
    cache = ResponseCache(runs_dir / "cache.sqlite")
    model = ModelClient(model_spec, llm=llm, cache=cache, limiter=RateLimiter() if llm is not None else None)
    guard = Guardrails(prompt_guard=PromptGuard.from_settings(cache), approval="auto")
    graph = build_graph()
    code = 0
    async with Scanner(timeout_ms=LOAD_TIMEOUT_MS) as browser, connect() as server:
        for i, (url, category) in enumerate(pages, 1):
            name = page_name(url)
            saved = results_dir / f"{name}.json"
            print(f"\n[{i}/{len(pages)}] {url}")
            if saved.exists() and not fresh and \
                    json.loads(saved.read_text(encoding="utf-8"))["status"] not in RETRY:
                print("  already done (use --fresh to redo it)")
                continue
            started = time.monotonic()
            try:
                snapshot = await browser.snapshot_url(url, patient=True)
            except Exception as e:
                note = f"could not load it: {str(e).splitlines()[0][:160]}"
                print(f"  {note}")
                saved.write_text(json.dumps(record(url, category, model.spec, status="load_failed", note=note),
                                            indent=2), encoding="utf-8")
                continue
            problem = site_problem(snapshot.html, snapshot.status)
            if problem:
                print(f"  skipped: {problem}")
                saved.write_text(json.dumps(record(url, category, model.spec, status="site_blocked", note=problem,
                                                   http_status=snapshot.status), indent=2), encoding="utf-8")
                continue
            (out / f"{name}.html").write_text(snapshot.html, encoding="utf-8")
            scan = await server.scanner.scan_html(snapshot.html)
            print(f"  snapshot: {len(snapshot.html) // 1024} KB, {len(scan.violations)} rules violated "
                  f"(score {violation_score(scan)}); live page score {violation_score(snapshot.scan)}")
            if not scan.violations:
                saved.write_text(json.dumps(record(url, category, model.spec, status="nothing_to_fix",
                                                   live_score=violation_score(snapshot.scan)), indent=2),
                                 encoding="utf-8")
                continue
            context = AgentContext(scanner=server.scanner, model=model, tools=server.model_tools(MODEL_TOOLS),
                                   guard=guard, max_rules=max_rules)
            try:
                state = await graph.ainvoke({"html": snapshot.html, "targets": [], "scan": compact_scan(scan)},
                                            run_config(max_rules=max_rules, run_name=f"public:{name}"),
                                            context=context)
            except StopRun as e:
                print(f"  stopped: {e}\n  Run the same command again later: finished pages are kept.")
                code = 2
                break
            except Exception as e:
                note = f"the agent failed: {type(e).__name__}: {str(e).splitlines()[0][:160] if str(e) else ''}"
                print(f"  {note}")
                saved.write_text(json.dumps(record(url, category, model.spec, status="error", note=note),
                                            indent=2), encoding="utf-8")
                code = 1
                continue
            (out / f"{name}.fixed.html").write_text(state["current_html"], encoding="utf-8")
            (results_dir / f"{name}.md").write_text(report(url, violation_score(snapshot.scan), state, model.spec),
                                                     encoding="utf-8")
            data = repaired_record(url, category, model.spec, snapshot, scan, state, time.monotonic() - started)
            saved.write_text(json.dumps(data, indent=2), encoding="utf-8")
            s = state["summary"]
            print(f"  score {s['initial_score']} -> {s['final_score']}; {data['fixed']} of {data['attempted']} "
                  f"rules fixed; written to {results_dir / f'{name}.md'}")
    write_summary(results_dir)
    print(f"\nSummary of every page done so far: {results_dir / 'README.md'}")
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("urls", nargs="*", help="public pages (http or https); default: the pages in --list")
    parser.add_argument("--list", type=Path, default=DEFAULT_LIST,
                        help="a file with one page per line: URL, then a category (default: scripts/public_pages.txt)")
    parser.add_argument("--model", help="provider:model (default: MODEL from .env)")
    parser.add_argument("--max-rules", type=int, default=MAX_RULES, help="rules to fix per page, most severe first")
    parser.add_argument("--fresh", action="store_true", help="redo pages that already have a report")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    listed = read_list(args.list) if args.list.exists() else []
    known = dict(listed)
    pages = [(url, known.get(url, "-")) for url in args.urls] or listed      # a listed site keeps its category
    if not pages:
        print(f"No pages: give addresses, or list them in {args.list}.")
        return 1
    return asyncio.run(repair(pages, args.model, args.max_rules, fresh=args.fresh))


if __name__ == "__main__":
    sys.exit(main())
