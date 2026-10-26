"""Check that everything needed to build and run the agent is in place.

    python scripts/check_setup.py              # full check, including two tiny model calls
    python scripts/check_setup.py --skip-llm   # no API key needed (used in CI)

Each line prints OK, FAIL or SKIP with a hint on how to fix failures.
Exit code is 0 only when every required check passes.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import platform
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

PACKAGES = [
    ("playwright", "browser automation"),
    ("langchain", "model interface"),
    ("langgraph", "agent graph"),
    ("langgraph.checkpoint.sqlite", "saved agent checkpoints"),
    ("markdownify", "HTML to Markdown"),
    ("langchain_groq", "Groq models"),
    ("langchain_mistralai", "Mistral models"),
    ("langchain_ollama", "local models (optional)"),
    ("mcp", "Model Context Protocol"),
    ("langchain_mcp_adapters", "MCP tools in LangChain"),
    ("onnxruntime", "embedding model runtime"),
    ("tokenizers", "embedding model tokenizer"),
    ("rank_bm25", "keyword search"),
    ("bs4", "HTML parsing"),
    ("pandas", "benchmark tables"),
    ("dotenv", ".env loading"),
    ("a11y_agent", "this project (pip install -e .)"),
]

failures = 0


def report(step: str, status: str, detail: str, hint: str = "") -> None:
    global failures
    if status == "FAIL":
        failures += 1
    print(f"{step:<20} {status:<5} {detail}")
    if hint and status == "FAIL":
        print(f"{'':<26}-> {hint}")


def check_python() -> None:
    v = sys.version_info
    ok = v >= (3, 10)
    note = "" if (3, 11) <= v[:2] <= (3, 13) else " (3.11-3.13 recommended)"
    report("Python", "OK" if ok else "FAIL", f"{platform.python_version()}{note}",
           "install Python 3.11 or 3.12 from python.org")


def check_packages() -> None:
    missing = []
    for name, purpose in PACKAGES:
        try:
            importlib.import_module(name)
        except Exception:
            missing.append(f"{name} ({purpose})")
    if missing:
        report("Packages", "FAIL", f"{len(PACKAGES) - len(missing)}/{len(PACKAGES)} importable; missing: "
               + ", ".join(missing), 'run: pip install -e ".[dev]"')
    else:
        report("Packages", "OK", f"{len(PACKAGES)}/{len(PACKAGES)} importable")


def check_resources() -> None:
    try:
        import fetch_resources as fr
    except Exception as e:
        report("Resources", "FAIL", f"cannot load scripts/fetch_resources.py ({e})")
        return
    problems, parts = [], []

    axe = fr.DATA / "vendor" / f"axe-core-{fr.AXE_VERSION}.min.js"
    expected = fr.AXE_SHA256.get(fr.AXE_VERSION)
    if not axe.exists():
        problems.append("axe-core missing")
    elif expected and fr.sha256_of(axe) != expected:
        problems.append("axe-core checksum mismatch")
    else:
        parts.append(f"axe-core {fr.AXE_VERSION}")

    bench = fr.DATA / "benchmark" / "accessguru"
    bad = [p for p, h in fr.BENCHMARK_FILES.items() if not fr.is_current(bench / p, h)]
    if bad:
        problems.append(f"{len(bad)} benchmark file(s) missing or changed")
    else:
        parts.append(f"benchmark {len(fr.BENCHMARK_FILES)} files")

    for label, folder, pattern in [("WCAG corpus", "wcag", "*.html"), ("axe rules", "axe-rules", "*.json")]:
        path = fr.DATA / "corpus" / folder
        n = sum(1 for _ in path.rglob(pattern)) if path.exists() else 0
        if n == 0:
            problems.append(f"{label} missing")
        else:
            parts.append(f"{label} {n} files")

    model = fr.DATA / "models" / "bge-small-en-v1.5"
    bad = [n for n, h in fr.EMBEDDING_FILES.items() if not fr.is_current(model / n.split("/")[-1], h)]
    if bad:
        problems.append("embedding model missing or changed")
    else:
        parts.append("embedding model")

    if problems:
        report("Resources", "FAIL", "; ".join(problems), "run: python scripts/fetch_resources.py")
    else:
        report("Resources", "OK", ", ".join(parts))


def check_scanner() -> None:
    try:
        from a11y_agent.scanner import Scanner
        from a11y_agent.scoring import violation_score
    except Exception as e:
        report("Browser + scanner", "FAIL", f"import failed ({e})")
        return

    async def run():
        async with Scanner() as scanner:
            broken = await scanner.scan_html((ROOT / "examples" / "broken_page.html").read_text(encoding="utf-8"))
            fixed = await scanner.scan_html((ROOT / "examples" / "fixed_page.html").read_text(encoding="utf-8"))
            return broken, fixed

    try:
        broken, fixed = asyncio.run(run())
    except Exception as e:
        msg = str(e).splitlines()[0]
        hint = "run: python -m playwright install chromium"
        if "axe-core not found" in msg:
            hint = "run: python scripts/fetch_resources.py --only axe"
        report("Browser + scanner", "FAIL", msg, hint)
        return
    ok = len(broken.violations) > 0 and len(fixed.violations) == 0
    report("Browser + scanner", "OK" if ok else "FAIL",
           f"broken example: {len(broken.violations)} rules violated (score {violation_score(broken)}); "
           f"fixed example: {len(fixed.violations)}",
           "the scanner ran but results look wrong; check AXE_VERSION in .env")


def check_embeddings() -> None:
    """Load the embedding model and embed two sentences (about a second)."""
    try:
        from a11y_agent.embeddings import OnnxEmbeddings
        model = OnnxEmbeddings()
        query = model.embed_query("images must have alternative text")
        near, far = (sum(a * b for a, b in zip(query, v)) for v in model.embed_documents([
            "H37: Using alt attributes on img elements. The objective of this technique is to provide a text alternative.",
            "G18: Ensuring that a contrast ratio of at least 4.5:1 exists between text and background."]))
    except Exception as e:
        report("Embeddings", "FAIL", str(e).splitlines()[0][:160],
               "run: python scripts/fetch_resources.py --only embedding-model")
        return
    report("Embeddings", "OK" if near > far else "FAIL",
           f"{len(query)} dimensions; related text scores higher ({near:.2f} vs {far:.2f})")


def check_mcp_server() -> None:
    """Start our MCP server as a separate process (as any MCP client would) and call one tool."""
    try:
        from a11y_agent.mcp_client import connect
    except Exception as e:
        report("MCP server", "FAIL", f"import failed ({e})", "run: pip install -e .")
        return

    async def run():
        async with connect() as server:
            result = await server.session.call_tool("contrast_fix", {"foreground": "#767676", "background": "#fff"})
            return [t.name for t in server.tools], result.structuredContent["ratio"]

    try:
        names, ratio = asyncio.run(asyncio.wait_for(run(), timeout=60))
    except Exception as e:
        report("MCP server", "FAIL", (str(e).splitlines() or [type(e).__name__])[0][:160],
               "try it directly: python -m a11y_agent.mcp_server   (errors are printed there)")
        return
    report("MCP server", "OK" if ratio == 4.54 else "FAIL", f"{len(names)} tools over stdio: {', '.join(names)}")


def check_documents() -> None:
    try:
        from a11y_agent.loaders import AxeRuleLoader, BenchmarkLoader, WCAGLoader
        benchmark = BenchmarkLoader("accessguru").load()
        access = BenchmarkLoader("access").load()
        wcag = WCAGLoader()
        pages = wcag.load()
        rules = AxeRuleLoader(index=wcag.index).load()
    except Exception as e:
        report("Documents", "FAIL", str(e).splitlines()[0][:160],
               "run: python scripts/fetch_resources.py   (then check the error above)")
        return
    report("Documents", "OK", f"benchmark {len(benchmark)} + {len(access)} rows, "
           f"WCAG {len(pages)} pages, axe {len(rules)} rules")


def check_split() -> None:
    try:
        from a11y_agent.benchmark import load_split
        dev, test = load_split("accessguru", "dev"), load_split("accessguru", "test")
        access = load_split("access", "test")
    except Exception as e:
        report("Benchmark split", "FAIL", str(e).splitlines()[0][:160],
               "run: python scripts/fetch_resources.py --only benchmark --force")
        return
    report("Benchmark split", "OK", f"AccessGuru dev {len(dev)} / test {len(test)}, ACCESS test {len(access)}")


def check_model_config() -> str | None:
    try:
        from a11y_agent.config import ROOT as root, settings
        from a11y_agent.llm import missing_key, parse_model_spec
        parse_model_spec(settings.model)
    except Exception as e:
        report("Model config", "FAIL", str(e),
               "set MODEL in .env, e.g. groq:openai/gpt-oss-120b or mistralai:mistral-small-2603")
        return None
    env_note = "" if (root / ".env").exists() else " (no .env file; using defaults)"
    key = missing_key()
    if key:
        where = {"GROQ_API_KEY": "https://console.groq.com/keys",
                 "MISTRAL_API_KEY": "https://console.mistral.ai/api-keys"}.get(key, "the provider's console")
        report("Model config", "FAIL", f"MODEL={settings.model}, but {key} is not set{env_note}",
               f"copy .env.example to .env and paste your key (free at {where})")
        return None
    lim = settings.limits()
    pace = ", ".join(f"{v:,} {unit}" for v, unit in [(lim.requests_per_minute, "requests/min"),
                                                      (lim.tokens_per_minute, "tokens/min"),
                                                      (lim.max_request_tokens, "tokens/request")] if v) or "no limits"
    report("Model config", "OK", f"MODEL={settings.model}{env_note}; pacing: {pace}")
    return settings.model


def check_model_call(spec: str) -> None:
    try:
        from a11y_agent.llm import ping
        reply, seconds = ping(spec)
        report("Model call", "OK", f'replied "{reply[:40]}" in {seconds:.1f} s')
    except Exception as e:
        report("Model call", "FAIL", str(e).splitlines()[0][:160],
               "check the key, the model name, and your internet connection")


def check_agent_reply(spec: str) -> None:
    """One real request in the agent's format: does the model return usable JSON edits?"""
    try:
        from a11y_agent.edits import apply_edits, parse_proposal, proposal_format
        from a11y_agent.model_client import ModelClient
        from a11y_agent.prompts import agent_messages

        html = '<img src="logo.png">'
        violation = {"rule_id": "image-alt", "impact": "critical", "help": "Images must have alternative text",
                     "criteria": ["1.1.1"], "node_count": 1,
                     "nodes": [{"html": html, "summary": "Element does not have an alt attribute"}]}
        client = ModelClient(spec)
        reply, record = asyncio.run(client.chat(agent_messages(violation, html), max_tokens=1024,
                                                response_format=proposal_format()))
        fixed = apply_edits(html, parse_proposal(reply).edits)
        how = "schema enforced by the provider" if client.native_schema else "no schema support; prompt only"
        report("Agent reply", "OK", f"usable JSON edits ({how}): {fixed[:60]}")
    except Exception as e:
        report("Agent reply", "FAIL", str(e).splitlines()[0][:160],
               "the model did not return usable edits; try again, or try FALLBACK_MODEL in .env")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--skip-llm", action="store_true", help="skip the model key and model call checks")
    args = parser.parse_args()

    print("Accessibility repair agent: setup check\n")
    check_python()
    check_packages()
    check_resources()
    check_documents()
    check_split()
    check_scanner()
    check_embeddings()
    check_mcp_server()
    if args.skip_llm:
        report("Model config", "SKIP", "--skip-llm")
        report("Model call", "SKIP", "--skip-llm")
        report("Agent reply", "SKIP", "--skip-llm")
    else:
        spec = check_model_config()
        if spec:
            check_model_call(spec)
            check_agent_reply(spec)
        else:
            report("Model call", "SKIP", "fix the model config first")
            report("Agent reply", "SKIP", "fix the model config first")

    print()
    if failures:
        print(f"{failures} check(s) failed. Fix them in order; later checks depend on earlier ones.")
        return 1
    print("All checks passed. Setup is complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
