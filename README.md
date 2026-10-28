# Accessibility Repair Agent

An AI agent that finds web accessibility (WCAG) violations in HTML, fixes them,
and **proves each fix worked by re-scanning the page**. Built with LangGraph,
an MCP tool server, retrieval over the official WCAG documentation, and
guardrails. Runs on free, open-weight models.

> **Status:** all six phases are built and evaluated: the evaluation harness
> and baselines, the LangGraph agent, an MCP tool server the agent uses as a
> client, retrieval over the WCAG documents, guardrails with human approval,
> the final runs on 238 held-out test items from two benchmarks, and a
> before/after on real Indian home pages (results below). The project is
> complete. See the [build plan](docs/PLAN.md).

## The problem

A 2025 WebAIM study found that **94.8% of the top one million home pages fail
WCAG accessibility guidelines**, which shuts out people who rely on screen
readers, keyboards or magnification. Checkers such as axe-core *report* these
violations, but someone still has to fix every one by hand.

**Task.** Given a page's HTML, produce corrected HTML that removes its
machine-detectable WCAG violations without changing what the page says or how
it is structured, and verify every fix by scanning again.

**How success is measured.**

- *Main metric:* violation score decrease (from the AccessGuru paper, ASSETS
  2025) on the AccessGuru benchmark. We use the **178 of 250** syntax and
  layout violations that reproduce from their stored HTML with axe-core
  4.12.1, so every result is checked by a scanner, not judged by a model.
  They are split once into a 40-item dev set and a 138-item test set
  ([splits/](splits/)); ACCESS (Huang et al. 2024) adds 100 more test items.
- *Comparison:* a zero-shot prompt and AccessGuru's prompting method, re-run
  with the same model and scorer, plus the paper's published numbers for context.
- *Side effects:* fixes that break another rule, and fixes that "work" by
  deleting content, are counted separately.
- *Second metric:* how often the guardrails reject a proposed fix, and why.
- *Constraint:* free open-weight models: OpenAI's gpt-oss-120b on Groq, and
  Mistral Small 4 on Mistral's API. Methods are compared on the same model.

## Results (held-out test sets, Mistral Small 4)

Every method runs on the same open-weight model and the same items. The test
items were never looked at while the agent was built, and the code was frozen
before they were run (tag `test-freeze`).

| Method | AccessGuru test, 138 items | ACCESS, 100 items | Plain score (AccessGuru test) | Unsafe fixes (both sets) | Cost per 100 pages |
|---|---|---|---|---|---|
| Zero-shot prompt | 0.47 (0.38–0.57) | 0.62 (0.51–0.72) | 0.55 | 30 | $0.08 |
| AccessGuru prompting (ASSETS 2025) | 0.29 (0.21–0.37) | 0.42 (0.32–0.52) | 0.75 | **111** | $0.78 |
| Our agent: verify-and-retry loop | 0.69 (0.61–0.76) | – | 0.73 | 8 | $0.13 |
| + MCP tools | 0.75 (0.68–0.82) | 0.77 (0.69–0.85) | **0.81** | 16 | $0.22 |
| + tools + WCAG retrieval | 0.73 (0.66–0.80) | – | 0.77 | 6 | $0.27 |
| **+ tools + guardrails** | **0.78 (0.70–0.84)** | **0.77 (0.69–0.85)** | 0.78 | **0** | $0.22 |

**How to read the table**

- *Scores.* The first two columns are the **safe score** with its 95%
  bootstrap interval. The score is the violation score decrease from the
  AccessGuru paper (1.0 means every violation fixed and nothing broken),
  counting a fix as no fix if it deleted or hid content, added a script or a
  link, or left invalid HTML, whichever method made it. The scanner accepts
  such "fixes", so the *plain score* rewards them.
- *Unsafe fixes.* The number of items, across both test sets, whose final
  page did any of those things. The rows with "–" were run on AccessGuru test
  only, to save quota, so their count covers that set alone.
- *Cost.* What Mistral's paid plan would charge for 100 AccessGuru test pages.
  The runs themselves used the free tier.

**What we found**

- **The guarded agent is the best method on both test sets, and none of its
  changes to the 238 test pages is unsafe.** It beats the published AccessGuru method by +0.49
  (+0.39 to +0.58) on AccessGuru test and by +0.35 (+0.24 to +0.47) on ACCESS.
  It beats a zero-shot prompt by +0.30 and +0.15. All of these are paired on
  the same items.
- **Most of the published method's score comes from unsafe fixes.** On the plain score
  it looks level with the agent (0.75 against 0.78, and 0.81 against 0.77 on
  ACCESS). But in nearly half the items (68 of 138, and 43 of 100) its
  rewrite removed, hid or broke something, and the scanner counted that as a
  fix.
- **Verification beats knowledge.** The verify-and-retry loop added +0.21
  (+0.11 to +0.31), the largest gain of any part. MCP tools added +0.06, most
  clearly on AAA colour contrast: 25 of 25 items fixed, against 19 without the
  contrast tool. WCAG retrieval added nothing (−0.02, −0.08 to +0.05), on dev
  as on test.
- **Safety cost nothing.** The guardrails changed the score by +0.03 (+0.01
  to +0.05) on AccessGuru test and by 0.00 on ACCESS. Their rejections
  steered the model to valid fixes on 4 items and never cost one. Without
  them, the agent with tools scores higher on the plain score (0.81) only
  because of 11 fixes that are invalid HTML.
- **Cheap and fast.** At $0.22 per 100 pages and a median of about 4 seconds
  of model time per page, it is 3.5 times cheaper and about 7 times faster
  than the published method.
- **It held up from dev to test.** The order of the methods is the same on
  all three sets, and the guarded agent scored 0.76 on dev, 0.78 and 0.77 on
  test.
- **It carries over to real pages, at a lower rate.** The agent ran on five
  whole Indian home pages: mygov.in, aiims.edu, iitb.ac.in, du.ac.in and
  snapdeal.com. It cut their violation score from 172 to 59 (0.66) and fixed
  30 of the 45 failing rules, with no false alarm from the input screen.
  Seven other sites refused the automated browser, or (two) hit a scanner
  bug that is now fixed. Details:
  [results/public-pages](results/public-pages/README.md).

**Where it still fails.** Page-level rules (`region`, `landmark-one-main`,
`page-has-heading-one`) score 0.61 against 0.84 for element-level rules. Two
causes stand out:

- to wrap a page in `<main>`, the model writes a whole `<html><body>`
  skeleton. The guardrails reject the second `<html>`, but the model often
  repeats it;
- a new `<h1>` placed outside a landmark breaks another rule.

On long pages, the model sometimes miscopies the text it wants to replace.
On colour contrast, it often picks the lowest colour that passes AA, which
then fails the stricter AAA check.

**Limitations.**

- The benchmarks are snippets, not whole sites.
- axe-core checks only what can be automated, and nobody judged the quality
  of the new alt text: the agent flags those changes for a person instead.
- The results come from one model and one run per method.
- Real pages: only five sites, and on them the agent sends whole pages to the
  model, which costs up to $0.35 a page.

Full analysis: [docs/06-results.md §5](docs/06-results.md#5-the-results).
Tables, per-item outcomes and the agent's traces: [results/accessguru-test](results/accessguru-test/README.md),
[results/access-test](results/access-test/README.md) and the dev set,
[results/accessguru-dev](results/accessguru-dev/README.md).

<details>
<summary>Dev-set results (40 items), used to build and tune the agent</summary>

| Method | Score decrease | Safe score (95% interval) | Resolved | Unsafe fixes | Tokens per item |
|---|---|---|---|---|---|
| Zero-shot prompt | 0.61 | 0.56 (0.41–0.71) | 23/40 | 1 | 1,825 |
| AccessGuru prompting (ASSETS 2025) | 0.75 | 0.37 (0.23–0.52) | 29/40 | 11 | 25,510 |
| Our agent (Phase 2) | 0.68 | 0.61 (0.46–0.76) | 28/40 | 3 | 7,721 |
| Our agent with MCP tools (Phase 3) | 0.76 | 0.76 (0.62–0.89) | 30/40 | 0 | 15,710 |
| Agent + tools + WCAG retrieval (Phase 4) | 0.70 | 0.68 (0.53–0.82) | 28/40 | 1 | 18,084 |
| Agent + tools + guardrails (Phase 5) | 0.76 | 0.76 (0.62–0.89) | 30/40 | 0 | 15,584 |

The agent rows are after the dev-set error analyses
([docs/02-agent.md §14–15](docs/02-agent.md#14-error-analysis-round-1),
[docs/05-guardrails.md §10](docs/05-guardrails.md#10-dev-results-and-what-they-changed)).

</details>

## For a CV

- Built an AI agent that repairs WCAG accessibility violations in web pages
  (LangGraph, an MCP tool server, hybrid RAG over the W3C documents,
  guardrails with human approval) on an open-weight model. On 138 held-out
  benchmark pages it removed **78% of the violation score (95% CI 70–84%)
  with zero unsafe fixes**, against 29% for the published AccessGuru method
  (ASSETS 2025) on the same model. The result replicated on a second
  benchmark (77% against 42%).
- Designed the evaluation before the final runs:
  - a frozen dev/test split;
  - a metric that refuses "fixes" made by deleting, hiding or breaking
    content (the published method scored 0.75 on the plain metric, 0.29 on
    it);
  - bootstrap confidence intervals, paired tests and ablations;
  - error analysis from step traces.
- Showed by ablation that scanner-verified retries matter most (+21 points),
  that a contrast-solving MCP tool fixed all 25 AAA contrast cases (from
  19), and that retrieval added nothing. The guardrails cost no accuracy
  (+3 points, 0 unsafe fixes).
- Ran it on real Indian government, university, hospital and e-commerce home
  pages: it cut their violation score by 66% (172 to 59) and fixed 30 of 45
  failing rules, with every invented label flagged for a human to review.
- The agent costs $0.22 per 100 benchmark pages: 3.5 times cheaper and about
  7 times faster than the baseline. The project also handles prompt injection (rule
  screen plus Llama Prompt Guard 2), SSRF-safe URL scanning, and a
  Windows-specific MCP stdio deadlock, with 250+ tests on Linux and Windows.

## How it works

```mermaid
flowchart TD
    A["HTML page or URL"] --> B["Scan<br/>Chromium + axe-core"]
    B --> C["Input guardrails<br/>size limit, prompt-injection screen"]
    C --> D["Retrieve guidance<br/>WCAG docs (RAG)"]
    D --> E["Propose fix<br/>open-weight LLM"]
    E --> F["Output guardrails<br/>valid HTML, content kept, nothing hidden"]
    F --> G{"Re-scan<br/>violation gone?"}
    G -- "no, retries left" --> E
    G -- "yes, or out of retries" --> H["Human approval<br/>review the diff"]
    H --> I["Fixed HTML + audit report"]
```

The model only *proposes* changes. A deterministic scanner decides whether a
violation is gone, and a person approves the final diff.

Built so far: the scan → propose → verify → retry loop as a LangGraph state
machine ([docs/02-agent.md](docs/02-agent.md)). The model returns small JSON
edits rather than a rewritten page; every fix is re-scanned and checked for
deleted content, and failures are fed back for the next attempt. The scanner,
fix checker, rule explanations and a WCAG contrast calculator are served by an
MCP server; the agent uses it as a client, and the model can call tools while
it works ([docs/03-mcp-tools.md](docs/03-mcp-tools.md)). Before the first
attempt on a rule, hybrid search (BM25 + local embeddings) over the W3C's
WCAG documents puts the relevant techniques and failures in the prompt
([docs/04-retrieval.md](docs/04-retrieval.md)). Guardrails screen each page for
prompt injection, reject fixes that remove or hide content, add scripts or
break the markup (axe-core accepts `aria-hidden="true"` on an image as a fix
for missing alt text), and pause the graph so a person approves the final
diff ([docs/05-guardrails.md](docs/05-guardrails.md)).

## Quickstart

You need Python 3.11 or 3.12 and Git. Works on Windows 11, macOS 14+ and
recent Ubuntu; a basic laptop is enough because the model runs in the cloud.

```bash
git clone https://github.com/Harshit-rai-1506/accessibility-repair-agent.git
cd accessibility-repair-agent

python -m venv .venv
# Windows (PowerShell):  .venv\Scripts\Activate.ps1
# macOS / Linux:         source .venv/bin/activate

pip install -e ".[dev]"                     # project + all dependencies
python -m playwright install chromium       # headless browser (~150 MB)
python scripts/fetch_resources.py           # benchmark, axe-core, WCAG docs, embedding model (~65 MB)

cp .env.example .env                        # Windows: copy .env.example .env
# paste a free Groq key (https://console.groq.com/keys) into .env,
# or use Mistral: MODEL=mistralai:mistral-small-2603 and MISTRAL_API_KEY
# .env stays on your laptop: never add it through the GitHub website

python scripts/check_setup.py               # every line should say OK
python -m pytest                            # 256 tests (add -m "not slow" to skip the 1-2 minute one)
python -m a11y_agent.loaders                # resources as LangChain Documents
python -m a11y_agent.scanner examples/broken_page.html
```

Fix a page with the agent (Phase 2):

```bash
python -m a11y_agent.agent examples/broken_page.html         # writes examples/broken_page.fixed.html
python -m a11y_agent.agent page.html --thread my-page        # saves checkpoints; re-run to resume after a stop
python -m a11y_agent.agent --graph                           # the graph as a Mermaid diagram
```

Use the MCP tool server (Phase 3):

```bash
python scripts/try_mcp.py                                    # start the server, list and call its tools (no key needed)
python -m a11y_agent.mcp_server                              # stdio, for the MCP Inspector or Claude's desktop app
python -m a11y_agent.mcp_server --http --port 8765           # streamable HTTP at http://127.0.0.1:8765/mcp
python -m a11y_agent.agent examples/broken_page.html --tools # the agent as an MCP client, with model tools
```

Search the WCAG documents (Phase 4):

```bash
python -m a11y_agent.retrieval build                         # embed the corpus once (a few minutes on a laptop)
python -m a11y_agent.retrieval search "icon link has no accessible name" --sc 2.4.4
python -m a11y_agent.retrieval eval                          # hit@k and MRR: keywords vs embeddings vs hybrid
python -m a11y_agent.agent examples/broken_page.html --rag   # the agent with W3C guidance in its prompts
```

Guardrails and human approval (Phase 5):

```bash
python -m a11y_agent.guardrails screen examples/injection/attack-alt-text.html   # prompt-injection screen
python -m a11y_agent.guardrails check examples/broken_page.html examples/fixed_page.html
python -m a11y_agent.guardrails eval                          # attacks caught, false alarms on real pages
python -m a11y_agent.agent examples/broken_page.html --tools --approve    # you approve, edit or reject the result
```

Run the evaluation (Phases 1–5):

```bash
python -m a11y_agent.evaluate run --method noop              # harness check, no model, ~20 s
python -m a11y_agent.evaluate run --method zero_shot         # dev split, ~42K tokens
python -m a11y_agent.evaluate run --method accessguru        # dev split, ~350K tokens: spans 2 days of free quota
python -m a11y_agent.evaluate run --method agent             # dev split, roughly 60-120K tokens
python -m a11y_agent.evaluate run --method agent_tools       # dev split, roughly 100-180K tokens
python -m a11y_agent.evaluate run --method agent_rag         # dev split, agent_tools + retrieval
python -m a11y_agent.evaluate run --method agent_guarded     # dev split, agent_tools + guardrails
python -m a11y_agent.evaluate report                         # compare everything run so far
python -m a11y_agent.evaluate compare --save                 # 95% intervals, paired tests, cost and speed
python -m a11y_agent.evaluate failures --method agent_guarded --save   # why items were not fixed
```

The guarded agent on real public home pages (the list is in `scripts/public_pages.txt`):

```bash
python scripts/public_pages.py                               # writes results/public-pages/README.md and one report per site
```

The final runs on the test sets (Phase 6), their analysis, and a before/after on real public pages:
[docs/06-results.md](docs/06-results.md).

Runs resume where they stopped, and every model reply is cached, so
re-running never pays twice. You can also run evaluations on GitHub instead
of your laptop: see [docs/01-evaluation.md](docs/01-evaluation.md#9-running-it).

Step-by-step explanations: [docs/00-setup.md](docs/00-setup.md) (setup),
[docs/01-evaluation.md](docs/01-evaluation.md) (evaluation),
[docs/02-agent.md](docs/02-agent.md) (the LangGraph agent),
[docs/03-mcp-tools.md](docs/03-mcp-tools.md) (MCP tools),
[docs/04-retrieval.md](docs/04-retrieval.md) (retrieval) and
[docs/05-guardrails.md](docs/05-guardrails.md) (guardrails and human approval) and
[docs/06-results.md](docs/06-results.md) (final runs and how they are analysed).

## Roadmap

| Phase | What gets built | Agent concepts | Status |
|---|---|---|---|
| 0 | Environment, pinned resources, LangChain document loaders, scanner, scoring, model client, CI | Ground truth, reproducibility, Documents | ✅ Done |
| 1 | Fixed dev/test split, evaluation harness, zero-shot and AccessGuru baselines | Evals before agents, caching, rate limits | ✅ Done (dev numbers in) |
| 2 | LangGraph agent: scan → fix → verify → retry, with state and checkpoints | Graphs, state, reducers, conditional edges, structured output, checkpoints | ✅ Done (0.68 vs zero-shot 0.60) |
| 3 | MCP server exposing the scanner, fix checker, rule explanations and a contrast calculator; agent calls them as a client | MCP (tools, resources, prompts, stdio/HTTP), function calling | ✅ Done (0.78, best so far) |
| 4 | Retrieval over WCAG Understanding docs and Techniques, as a graph node and an MCP tool | Chunking, embeddings, hybrid search, metadata filters, retrieval evaluation | ✅ Done (did not help on dev; kept as method D) |
| 5 | Guardrails on input, output and actions, plus human approval | Prompt injection, validation, human-in-the-loop | ✅ Done (no cost on dev: 0.76 = agent with tools; 0/40 false alarms) |
| 6 | Full benchmark run, ablations, cost and latency, write-up | Experiment design, confidence intervals, error analysis, reporting | ✅ Done (test: 0.78 safe, 0 unsafe fixes, vs AccessGuru 0.29; 5 real pages: 0.66) |

Details, acceptance criteria and budget for each phase: [docs/PLAN.md](docs/PLAN.md).

## Project layout

```
src/a11y_agent/
    config.py      settings from .env (model, axe version, paths)
    scanner.py     Chromium + axe-core: the ground truth for every fix
    scoring.py     violation score and fix verification (AccessGuru metric)
    llm.py         one function that returns the configured chat model
    loaders.py     LangChain document loaders (BaseLoader): benchmark rows, WCAG pages, axe rules
    benchmark.py   reproducible rows and the fixed dev/test split
    prompts.py     zero-shot and AccessGuru prompts; pulling HTML out of replies
    methods.py     repair methods behind one interface (noop, zero_shot, accessguru, agent, ..., agent_guarded)
    model_client.py  cached, rate-limited model calls; JSON-schema output; clean stops at the daily quota
    evaluate.py    the harness: scan, fix, re-scan, score, report
    agent.py       the LangGraph agent: state, nodes, conditional edges, checkpoints, CLI
    edits.py       the model's JSON edits: schema, validation, all-or-nothing apply, excerpts of long pages
    content.py     did a fix keep the page's words and elements?
    verification.py  one judgement of a fix, shared by the agent and the MCP tool
    contrast.py    WCAG contrast ratio and the closest colours that pass
    mcp_server.py  the MCP server (FastMCP): scan, verify, explain, contrast, search, safety tools; a resource; a prompt
    mcp_client.py  starts the server and connects: a remote scanner and LangChain tools for the model
    retrieval.py   chunking, BM25 + dense hybrid search, criteria filters, retrieval evaluation
    embeddings.py  bge-small embeddings on the CPU (ONNX), cached; a test stand-in
    guardrails.py  prompt-injection screen (rules + Llama Prompt Guard 2), checks on every fix, review flags
    stats.py       bootstrap confidence intervals, paired differences, sign tests
scripts/
    fetch_resources.py   downloads pinned, checksum-verified resources into data/
    check_setup.py       tells you exactly what is missing
    try_mcp.py           a tiny MCP client: see the server's tools in action
    public_pages.py      before/after: the guarded agent on real public pages
splits/            the dev/test split (row ids), made once and committed
results/           evaluation summaries, committed after each run (runs/ holds the full, ignored, output)
examples/          a broken page (12 violated rules), its hand-fixed version (0), and injection/ test pages
tests/             pytest suite, run on Linux and Windows by GitHub Actions
docs/              build plan and one explanatory guide per phase
```

## Credits

The benchmark, the scoring metric and the baseline prompting methods come from
**AccessGuru** (Fathallah, Hernández and Staab, ASSETS 2025). axe-core is by
Deque Systems; WCAG documents are by the W3C. Details and licences:
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Code in this repository is MIT
licensed.
