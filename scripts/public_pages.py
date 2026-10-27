"""Before and after: the guarded agent on real public pages (Phase 6).

    python scripts/public_pages.py https://www.example.com https://another.example.org
    python scripts/public_pages.py https://www.example.com --max-rules 5 --model mistralai:mistral-small-2603

For each address:

1. load the page in Chromium and keep the HTML as the browser built it (after
   its scripts ran), in runs/public/<name>.html;
2. scan that snapshot, then let the agent fix it, with tools and guardrails
   (input screen, output checks, automatic approval with review flags);
3. write results/public-pages/<name>.md: the score before and after, every
   rule with what happened to it, the guardrails' rejections and flags, the
   cost, and the start of the diff. The fixed page goes to runs/public/.

Scores are for the saved snapshot, scanned like the benchmark (network
blocked), before and after, so the two are comparable. Without the page's
stylesheets and images some results (colour contrast especially) can differ
from the live site, whose own score is shown for reference.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from a11y_agent.agent import AgentContext, build_graph, compact_scan, run_config, unified_diff  # noqa: E402
from a11y_agent.config import settings  # noqa: E402
from a11y_agent.guardrails import Guardrails, PromptGuard  # noqa: E402
from a11y_agent.model_client import ModelClient, ResponseCache  # noqa: E402
from a11y_agent.scanner import Scanner  # noqa: E402
from a11y_agent.scoring import violation_score  # noqa: E402

RESULTS_DIR = ROOT / "results" / "public-pages"
DIFF_LINES = 40


def page_name(url: str) -> str:
    parsed = urlparse(url)
    return re.sub(r"[^A-Za-z0-9.-]+", "-", f"{parsed.hostname or 'page'}{parsed.path}").strip("-")[:80] or "page"


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


async def repair(urls: list[str], model_spec: str | None, max_rules: int, *, results_dir: Path = RESULTS_DIR,
                 runs_dir: Path | None = None, llm=None) -> int:
    """Snapshot, fix and report each page. ``llm`` (a stand-in model) and the folders are for tests."""
    from a11y_agent.mcp_client import MODEL_TOOLS, connect
    from a11y_agent.model_client import RateLimiter
    runs_dir = runs_dir or settings.runs_dir
    out = runs_dir / "public"
    out.mkdir(parents=True, exist_ok=True)
    results_dir.mkdir(parents=True, exist_ok=True)
    cache = ResponseCache(runs_dir / "cache.sqlite")
    model = ModelClient(model_spec, llm=llm, cache=cache, limiter=RateLimiter() if llm is not None else None)
    guard = Guardrails(prompt_guard=PromptGuard.from_settings(cache), approval="auto")
    graph = build_graph()
    async with Scanner() as browser, connect() as server:
        for url in urls:
            name = page_name(url)
            print(f"\n{url}")
            try:
                html, live = await browser.snapshot_url(url)
            except Exception as e:
                print(f"  could not load it: {str(e).splitlines()[0][:160]}")
                continue
            (out / f"{name}.html").write_text(html, encoding="utf-8")
            snapshot = await server.scanner.scan_html(html)
            print(f"  snapshot: {len(snapshot.violations)} rules violated (score {violation_score(snapshot)}); "
                  f"live page score {violation_score(live)}")
            context = AgentContext(scanner=server.scanner, model=model, tools=server.model_tools(MODEL_TOOLS),
                                   guard=guard, max_rules=max_rules)
            state = await graph.ainvoke({"html": html, "targets": [], "scan": compact_scan(snapshot)},
                                        run_config(max_rules=max_rules, run_name=f"public:{name}"), context=context)
            (out / f"{name}.fixed.html").write_text(state["current_html"], encoding="utf-8")
            text = report(url, violation_score(live), state, model.spec)
            (results_dir / f"{name}.md").write_text(text, encoding="utf-8")
            s = state["summary"]
            print(f"  score {s['initial_score']} -> {s['final_score']}; written to {results_dir / f'{name}.md'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("urls", nargs="+", help="public pages (http or https)")
    parser.add_argument("--model", help="provider:model (default: MODEL from .env)")
    parser.add_argument("--max-rules", type=int, default=10, help="rules to fix per page, most severe first")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    return asyncio.run(repair(args.urls, args.model, args.max_rules))


if __name__ == "__main__":
    sys.exit(main())
