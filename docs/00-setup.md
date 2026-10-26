# Phase 0: Setup — what we built and why

**Goal of this phase:** build everything the agent will stand on *before*
writing any agent code: a reproducible environment, pinned data, a scanner
that is the single source of truth, a scoring metric, and a model connection.

**The one idea to take away:** an agent is only as trustworthy as the thing
that checks its work. Here that is the scanner, so we built and tested it first.

---

## Step 1. The Python environment

```bash
python -m venv .venv
.venv\Scripts\Activate.ps1          # Windows PowerShell
source .venv/bin/activate           # macOS / Linux
pip install -e ".[dev]"
```

- **Virtual environment (`.venv`).** A private copy of Python for this project,
  so its packages cannot clash with other projects. Activate it in every new
  terminal; your prompt shows `(.venv)` when it is active.
  On Windows, if activation is blocked, run
  `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once.
- **`pyproject.toml`.** The single list of dependencies and project metadata.
  We install packages for *all* phases now (LangGraph, MCP, retrieval), so later
  phases only add code.
- **`-e` (editable install).** Installs our `src/a11y_agent` package by
  reference, so code changes take effect without reinstalling, and
  `import a11y_agent` works from scripts, tests and the MCP server alike.
- **`[dev]`** adds tools needed only for development (pytest).
- **Version ranges** like `langgraph>=1.2,<2` accept bug-fix updates but block
  the next major version, which may break our code. CI records the exact
  versions it installed as a downloadable artifact on each run.

---

## Step 2. The browser and the rule engine

```bash
python -m playwright install chromium
```

- **Playwright** drives a real browser from Python. We need a real browser
  because accessibility depends on the rendered page: colour contrast,
  element sizes, what is visible.
- **axe-core** (by Deque) is the industry-standard accessibility rule engine.
  It is one JavaScript file that we inject into the page; it checks about 100
  rules and reports each violation with its impact (minor → critical), the
  WCAG success criteria it maps to, and the exact elements that fail.

How `src/a11y_agent/scanner.py` works:

1. Start Chromium **once** and keep it open. Starting a browser takes about a
   second; opening a fresh isolated *context* (like a private window) takes
   milliseconds. Over hundreds of benchmark rows on a laptop this matters.
2. For each scan, open a new context, **block all network requests**, load the
   HTML, inject axe-core from the local file, run it, close the context.
3. Return typed Python objects (`ScanResult` → `Violation` → `ViolationNode`).

Why block the network? The HTML may come from an untrusted page. Nothing in
it should be able to call out to the internet while we scan it, and offline
scans are faster and give the same result every time. This is our first
guardrail.

Why async? The agent (LangGraph) and the MCP server both run inside Python's
event loop. Playwright's synchronous API refuses to run there, so the scanner
is async from the start, with `scan_once()` as a simple wrapper for scripts.

Try it:

```bash
python -m a11y_agent.scanner examples/broken_page.html
```

```
axe-core 4.12.1: 12 rule(s) violated, violation score 44
  [critical] button-name                  1 element(s)  WCAG 4.1.2
  [serious ] color-contrast               1 element(s)  WCAG 1.4.3
  [serious ] document-title               1 element(s)  WCAG 2.4.2
  [minor   ] empty-heading                1 element(s)  WCAG best practice
  [serious ] html-has-lang                1 element(s)  WCAG 3.1.1
  [critical] image-alt                    2 element(s)  WCAG 1.1.1
  [moderate] landmark-one-main            1 element(s)  WCAG best practice
  [serious ] link-name                    1 element(s)  WCAG 2.4.4, 4.1.2
  [moderate] meta-viewport                1 element(s)  WCAG 1.4.4
  [moderate] page-has-heading-one         1 element(s)  WCAG best practice
  [moderate] region                       5 element(s)  WCAG best practice
  [serious ] target-size                  2 element(s)  WCAG 2.5.8
```

Then scan `examples/fixed_page.html`, which I fixed by hand: 0 violations.
Compare the two files to see what each fix looks like. Note that the fixed
page needed a `target-size` fix too (clickable areas at least 24 × 24 pixels,
new in WCAG 2.2), which was easy to miss by eye and the scanner caught.

---

## Step 3. Scoring: turning scans into a number

`src/a11y_agent/scoring.py` implements the metric from the AccessGuru paper.

| Impact | Score |
|---|---|
| critical | 5 |
| serious | 4 |
| moderate | 3 |
| minor | 2 |

For one attempt to fix one target violation:

| What happened | New score |
|---|---|
| Target gone, nothing else broken | 0 (resolved) |
| Target gone, but the fix broke another rule | impact of the newly broken rule(s) |
| Target still there | original score + any newly broken rules |

Over a benchmark: **violation score decrease = 1 − total new ÷ total old.**

Why penalise newly broken rules? Because the easiest way to make a violation
disappear is to delete or hide the element, and that usually breaks something
else. A metric that ignored side effects would reward exactly the behaviour we
want to prevent. (Phase 5 adds explicit guardrails for the cases the scanner
cannot see.)

We checked the formula against the paper's own spreadsheet of Mistral-7B
results: 1 − 160 ÷ 892 = 0.8206, matching their published 0.82.

---

## Step 4. Downloading resources, pinned and verified

```bash
python scripts/fetch_resources.py
```

| Resource | Size | Used in |
|---|---|---|
| axe-core 4.12.1 | 0.6 MB | Every scan (Phase 0 onwards) |
| AccessGuru + ACCESS benchmark CSVs | 2 MB | Evaluation (Phase 1) |
| W3C WCAG 2.2 Understanding docs, criteria, Techniques (~860 pages) | ~12 MB | Retrieval (Phase 4) |
| axe-core rule metadata (rule → WCAG criterion, help text) | 0.7 MB | Retrieval and the `explain_rule` tool (Phases 3–4) |

Three habits worth copying into any ML project:

- **Pin versions.** Every source is fixed to an exact release or commit. If
  axe-core updates a rule next month, our benchmark numbers do not silently
  change.
- **Verify checksums.** The axe-core file and the benchmark files each have a
  known SHA-256. A corrupted or changed download stops the script instead of
  quietly changing the data. (The W3C pages are pinned by Git commit instead.)
- **Download, don't commit.** The data stays under its owners' licences and the
  repository stays small. `data/MANIFEST.json` records what was fetched, from
  where, and when.

The script tries the fast route first (direct download) and falls back to a
sparse Git checkout, so it works on networks that block one or the other.

**Why downloading does not use LangChain.** LangChain's document loaders
*read* data into memory as text; they do not save files. Its web and Git
loaders cannot pin a version, check a checksum, or fetch the axe-core
JavaScript file the scanner injects into pages. So downloading stays a plain
script, and LangChain takes over at the next step: reading.

---

## Step 5. Reading the resources as LangChain Documents

```bash
python -m a11y_agent.loaders                       # how many Documents each loader makes
python -m a11y_agent.loaders --show wcag --limit 1 # look at one
```

A LangChain **Document** is a piece of text (`page_content`) plus a dictionary
of facts about it (`metadata`). Every later LangChain component takes
Documents as input: text splitters cut them into chunks, vector stores index
them, retrievers return them. So turning each resource into Documents is the
first step of any LangChain retrieval (RAG) pipeline.

`src/a11y_agent/loaders.py` has one loader per resource:

| Loader | One Document per | `page_content` | Key metadata |
|---|---|---|---|
| `BenchmarkLoader` | benchmark row (305 + 171) | the broken HTML snippet | `rule_id`, `impact`, `category`, `url` |
| `WCAGLoader` (uses `WCAGPageLoader` per file, then `MarkdownTransformer`) | W3C page (581) | the page as Markdown | `sc` (e.g. `2.5.8`), `doc_type`, `level`, `technique_id`, `url` |
| `AxeRuleLoader` | axe-core rule (105) | what the rule checks and how to pass it | `rule_id`, `impact`, `sc` |

Things worth noticing, because they come up in any real RAG project:

- **A loader is a small class.** Subclass LangChain's `BaseLoader` and write
  `lazy_load()`, a generator that yields Documents one at a time. You get
  `.load()` (all at once) for free. A transformer subclasses
  `BaseDocumentTransformer` and maps Documents to new Documents.
- **Why not LangChain's ready-made `CSVLoader` and `DirectoryLoader`?** They
  live in `langchain-community`, which LangChain sunset in 2026 and no longer
  maintains. Our first version used them, and CI flagged the package as
  retired. We kept the standard interfaces from `langchain-core` (maintained)
  and wrote the few lines of CSV reading and file finding ourselves. The
  result plugs into every LangChain splitter, vector store and retriever
  exactly the same way.
- **Real data is messy.** The three benchmark files name their columns
  differently, so a small table (`BENCHMARKS`) maps them onto the same
  metadata keys. The ACCESS dataset stores each snippet as a printed Python
  list (`[['<img ...>']]`), which the loader unwraps. Without
  `encoding="utf-8"`, Windows would misread the files.
- **Metadata is what makes retrieval precise.** The W3C source files never say
  "this is success criterion 2.5.8": the number comes from the page's position
  in `guidelines/index.html`, and which techniques belong to which criterion
  lives in a separate data file. `WCAGIndex` reads both once and labels every
  page. When axe reports `target-size`, retrieval can then filter to
  `sc == "2.5.8"` and search only those pages.
- **Transformers clean content.** `MarkdownTransformer` turns HTML into
  Markdown. Headings become `#`/`##` lines (Phase 4 splits on these, so each
  chunk stays within one section) and code examples become fenced blocks
  the model can copy from.
- **Vector stores are picky about metadata.** Chroma accepts only strings,
  numbers and booleans, so lists such as "related success criteria" are stored
  as `"2.4.4,4.1.2"`. A test checks this for every Document.

---

## Step 6. What is in the benchmark, and what we found

The AccessGuru benchmark has 305 rows: real violations found on real websites,
each with the failing HTML snippet. Here is one row:

| Field | Example |
|---|---|
| `violationnumberID` | `image-alt` |
| `category` | Syntax |
| `impact` | Critical |
| `affectedHTMLElement(s)` | `<img src="/images/logo.png">` |
| `webURL` | the page it came from |

Before planning the evaluation, we scanned every snippet to check the
violation can actually be reproduced from the stored HTML. **178 of the 250
syntax and layout rows reproduce.** The rest mostly need the full page (for
example, "landmarks must be unique" needs to see the other landmarks) or use a
rule axe has since retired (`duplicate-id`).

This matters because it decides what we can claim. On those 178, every fix
can be checked by a machine, so the final numbers need no human judgement and
anyone can reproduce them. The full breakdown is in
[PLAN.md §4](PLAN.md#4-evaluation-design).

---

## Step 7. The model

```bash
cp .env.example .env      # Windows: copy .env.example .env
```

Then paste a free key from https://console.groq.com/keys into `.env`.
(Mistral works too: set `MODEL=mistralai:mistral-small-2603` and
`MISTRAL_API_KEY` from https://console.mistral.ai/api-keys; see the table
below.)

- **Which model.** `openai/gpt-oss-120b`: an open-weight model (Apache 2.0
  licence) that OpenAI released, hosted for free by Groq. "Open-weight" means
  anyone can download and run it; Groq simply runs it fast for us.
- **`provider:model` strings.** `MODEL=groq:openai/gpt-oss-120b` in `.env`. The
  part before the first colon picks the provider; LangChain's
  `init_chat_model` builds the right client. Switching provider is one line:

  | MODEL | What it is | Key | Free tier |
  |---|---|---|---|
  | `groq:openai/gpt-oss-120b` | OpenAI's open-weight gpt-oss (120B) on Groq | `GROQ_API_KEY` | 30 requests and 8,000 tokens per minute, 200,000 tokens per day |
  | `mistralai:mistral-small-2603` | Mistral Small 4 (119B, open weights, Apache 2.0) on Mistral's API | `MISTRAL_API_KEY` | 1 request per second, 500,000 tokens per minute |
  | `ollama:qwen2.5-coder:3b` | any model running locally in Ollama | none | your laptop's speed |

  Each provider gets its own pacing (`PROVIDER_LIMITS` in `config.py`), and
  `llm.py` is the only file that knows provider details.
- **Free-tier limits shape the design.** 30 requests and 8,000 tokens per
  minute, 200,000 tokens per day, per model. So: `reasoning_effort=low`
  (gpt-oss thinks before answering, and thinking costs tokens), automatic
  retries with backoff when we hit the per-minute limit, a fallback model with
  its own quota, and (in Phase 1) caching every response on disk.
- **Temperature 0** makes outputs as repeatable as possible, which an
  evaluation needs. (LangChain's Groq client silently sends 1e-8 instead of an
  exact 0; CI caught this when our first test expected exactly 0.)
- **Never commit keys.** `.env` is in `.gitignore`, so `git` on your laptop
  will never upload it; only `.env.example`, with no key in it, is in the
  repository. Do not create `.env` through the GitHub website: the website
  ignores `.gitignore`, and in a public repository the key is visible to
  everyone, permanently, in the file history. If that happens, revoke the key
  at console.groq.com/keys and make a new one.

---

## Step 8. Checking everything: setup check, tests, CI

```bash
python scripts/check_setup.py
python -m pytest
```

`check_setup.py` checks, in order, the Python version, that all packages
import, that every resource is present with the right checksum, that the
loaders turn them into Documents, that the browser and scanner work on both
example pages, that a model and key are configured, and finally makes one tiny
model call. Each failure prints the
command that fixes it. A healthy run looks like:

```
Python               OK    3.12.x
Packages             OK    17/17 importable
Resources            OK    axe-core 4.12.1, benchmark 7 files, WCAG corpus 625 files, axe rules 107 files, embedding model
Documents            OK    benchmark 305 + 171 rows, WCAG 581 pages, axe 105 rules
Browser + scanner    OK    broken example: 12 rules violated (score 44); fixed example: 0
Embeddings           OK    384 dimensions; related text scores higher (0.77 vs 0.69)
MCP server           OK    6 tools over stdio: scan_html, scan_url, verify_fix, explain_rule, contrast_fix, search_wcag
Model config         OK    MODEL=groq:openai/gpt-oss-120b; pacing: 25 requests/min, 7,000 tokens/min, 8,000 tokens/request
Model call           OK    replied "ready" in 0.6 s
Agent reply          OK    usable JSON edits (schema enforced by the provider): <img src="logo.png" alt="...">
```

(The last two lines need your key; "Agent reply" was added in Phase 2.)

The **41 tests** cover the scoring rules (with hand-made scan results, no
browser needed), the real scanner on both example pages, end-to-end fix
verification, the model configuration (without calling the API), and the
document loaders on the real downloaded data.

**Continuous integration** (`.github/workflows/ci.yml`): on every push,
GitHub starts a clean Linux machine and a clean Windows machine, follows the
Quickstart from scratch, and runs the tests and the setup check. If it passes,
the instructions are known to work on a machine that is not mine.

---

## What you can now explain

- **Why the scanner comes before the agent.** The model proposes; a
  deterministic tool decides. Without a ground truth there is nothing to
  verify fixes against, and no way to measure improvement.
- **Why the benchmark was filtered to 178 rows.** We only score fixes a
  machine can verify, so results are reproducible without human judgement.
- **Why the metric penalises side effects.** To avoid rewarding fixes that
  delete or hide content.
- **How the model is swappable.** One `provider:model` string, one factory
  function; nothing else imports a provider SDK.
- **Why versions are pinned and checksummed.** So the same command gives the
  same numbers next month and on someone else's machine.
- **What a LangChain Document is, and why loaders come first.** Text plus
  metadata; every splitter, vector store and retriever consumes it. Good
  metadata (here, WCAG numbers) is what lets retrieval filter precisely.

## Next: Phase 1

Load the benchmark into typed objects, split it into dev and test sets, write
an evaluation harness that caches every model call, and measure the two
baselines (zero-shot and AccessGuru-style prompting). That gives the numbers
the agent has to beat.
