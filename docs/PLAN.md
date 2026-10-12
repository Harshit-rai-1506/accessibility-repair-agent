# Build plan

This is the full plan for the project, from an empty repository to measured
results. Each phase ends with something that runs, a test or number that
proves it works, and a guide in `docs/` explaining what was built and why.

**Contents:** [1. Goal](#1-goal) · [2. How an AI agent is put together](#2-how-an-ai-agent-is-put-together) ·
[3. Key decisions](#3-key-decisions) · [4. Evaluation design](#4-evaluation-design) ·
[5. Phases](#5-phases) · [6. Budget](#6-budget-and-free-tier-limits) ·
[7. Risks](#7-risks-and-how-we-handle-them) · [8. Timeline](#8-suggested-timeline)

---

## 1. Goal

Build an agent that takes the HTML of a web page, fixes its machine-detectable
WCAG accessibility violations without changing what the page says, and proves
each fix by re-scanning. Measure it honestly on a public benchmark against
simpler methods, using only free open-weight models.

**What "done" looks like:** a results table in the README (score decrease,
violations resolved, new violations introduced, guardrail rejections, tokens
and seconds per fix) for five methods, a demo on a real page, and an MCP server
other tools can use.

---

## 2. How an AI agent is put together

Every agent, whatever the framework, is built from the same parts. This
project builds each one explicitly, in an order where every part can be
tested before the next depends on it.

| Part | Question it answers | In this project | Phase |
|---|---|---|---|
| **Model** | Who proposes what to do? | An open-weight LLM behind one `get_chat_model()` function | 0 |
| **Ground truth** | How do we know if it worked? | axe-core scanner in headless Chromium | 0 |
| **Evaluation** | Is it better than something simpler? | Benchmark harness with a fixed metric | 1 |
| **Orchestration** | What happens next, and when do we stop? | A LangGraph state machine with a retry loop | 2 |
| **State and memory** | What does it remember during a task? | Typed graph state, checkpoints | 2 |
| **Tools** | What can it actually do? | Scan, verify and explain-rule tools served over MCP | 3 |
| **Knowledge** | What does it look up instead of guessing? | Retrieval over WCAG docs and techniques | 4 |
| **Guardrails** | What must it never do? | Input, output and action checks | 5 |
| **Human in the loop** | Who has the final say? | Approval step before changes are applied | 5 |
| **Observability** | What exactly happened on this run? | Logged traces (LangSmith optional) | 2 onwards |

**Workflow or agent?** A *workflow* follows a path you designed; an *agent*
lets the model choose its next step. This project is deliberately a workflow
with one loop (scan → fix → verify → retry), and the model's freedom is
limited to *how* it fixes each violation. That makes every run verifiable and
cheap enough for a free tier. Phase 3 adds tool calling, where the model
decides when to look up a rule, which is where agent-style behaviour enters.

**Order matters.** We build the scanner (ground truth) and the evaluation
harness *before* the agent. Without them there is no way to tell whether a
change to the agent helped, and agent projects without evals usually end as
demos that work on one example.

---

## 3. Key decisions

| Decision | Choice | Why |
|---|---|---|
| Model | `openai/gpt-oss-120b` on Groq's free tier | Open weights (Apache 2.0), a production model on Groq, no card needed, fast. |
| Second model | `openai/gpt-oss-20b` on Groq | Separate free daily quota; also lets us test whether a smaller model suffices. |
| Offline option | Any Ollama model, e.g. `qwen2.5-coder:3b` | Works without internet, but slow on a laptop CPU, so not the default. |
| Ground truth | axe-core **4.12.1**, pinned and checksum-verified | The industry-standard rule engine; pinning keeps results reproducible. |
| Rules checked | WCAG 2.0/2.1/2.2 A, AA, AAA + best practice | Matches the benchmark, which includes AAA contrast and best-practice rules. |
| Benchmark | AccessGuru (ASSETS 2025), plus ACCESS (Huang et al. 2024) | Real violations from real sites, a published metric and published baselines. |
| Framework | LangGraph | Explicit state, loops with limits, checkpoints, built-in pause for human approval. |
| Tool protocol | MCP Python SDK 1.x + `langchain-mcp-adapters` 0.3 | The adapter requires `mcp < 2`, so we pin the 1.x line. |
| Vector store | Chroma (embedded) + BM25 keyword search | Runs locally on a laptop; metadata filters let us search within one WCAG criterion. |
| Retrieval corpus | W3C WCAG 2.2 Understanding docs, success criteria and Techniques | The authoritative source, licensed for reuse with attribution. |

---

## 4. Evaluation design

### What we measured during setup

We scanned every benchmark row's stored HTML snippet with our scanner to see
which violations can actually be reproduced:

| Set | Rows | Reproduced | Notes |
|---|---|---|---|
| AccessGuru syntax + layout | 250 | **178** (128 syntax, 50 layout, 49 distinct rules) | Our main test set |
| AccessGuru semantic | 55 | not attempted | Needs images and human judgement; out of scope |
| ACCESS (Huang et al.) | 171 | measured in Phase 1 | Second test set |

The 72 rows that do not reproduce are mostly `duplicate-id` (20, a rule axe
has since retired), `landmark-unique` (14, needs the whole page) and
`color-contrast-enhanced` (5, needs the page's CSS). We exclude them because a
fix we cannot verify by re-scanning is a fix we cannot score honestly.

**Page-level rules.** 60 of the 178 are `region`, `landmark-one-main` or
`page-has-heading-one`. These fire on any snippet that is not wrapped in page
landmarks, so they are easy to "fix". We report results with and without them.

**Snippet size.** Median about 120 tokens, 90th percentile about 2,000, largest
about 10,800. Ten rows exceed 3,000 tokens; the free tier's 8,000 tokens per
minute limit means the largest need special handling (reported, not hidden).

### The metric

From the AccessGuru paper, implemented in `src/a11y_agent/scoring.py`:

- each violated rule scores by impact: critical 5, serious 4, moderate 3, minor 2;
- after a fix: target gone → score of any *newly broken* rules; target still
  there → old score plus newly broken rules;
- **violation score decrease** = 1 − (total new score ÷ total old score).

We checked our formula against the paper's own results file: it reproduces
their published 0.8206 for Mistral-7B exactly.

Alongside it we report: violations resolved cleanly, fixes that broke
something else, guardrail rejections by reason, and tokens, seconds and
dollars per fix.

### Splits

The 178 rows are split once, stratified by rule: a **dev set of 40** for
building and tuning, and a **test set of 138** that is run only at the end.
Tuning prompts on the test set would make the final numbers meaningless.

### Methods compared

| # | Method | What it isolates |
|---|---|---|
| A | Zero-shot prompt | The model alone |
| B | AccessGuru-style prompt with one re-prompt | The published method, on our model |
| C | Our agent, no retrieval | Value of the verify-and-retry loop |
| D | Our agent + retrieval | Value of WCAG knowledge |
| E | Our agent + retrieval + guardrails | Cost of safety, in score and in rejected fixes |

Main model: gpt-oss-120b. Repeat A and E on gpt-oss-20b to see how much the
larger model matters. The paper's published numbers (0.84 GPT-4, 0.82
Mistral-7B) are shown for context only: they used axe 4.4.1 and scored
non-reproducible rows with hand-written rules, so they are not directly
comparable.

---

## 5. Phases

### Phase 0: Setup and resources ✅

**Built:** project packaging, pinned and checksum-verified downloads, the
scanner, the scoring metric, the model client, a setup checker, 19 tests, and
CI on Linux and Windows.
**Guide:** [docs/00-setup.md](00-setup.md)
**Done when:** `check_setup.py` passes on your laptop and CI is green.

### Phase 1: Benchmark and baseline (about 3–4 hours)

**Build**
- `benchmark.py`: load the CSVs into typed `BenchmarkItem`s, keep the
  reproducible rows, save the dev/test split to a file.
- `evaluate.py`: run any *fixer* over a split with limited concurrency, cache
  every model response on disk (re-runs cost nothing), write per-row results
  (JSONL) and a summary.
- Method A (zero-shot) and method B (AccessGuru-style prompt).

**Concepts:** evaluation harnesses, caching, rate limits, parsing model output,
dev/test discipline.
**Done when:** a table with methods A and B on the dev set, and the ACCESS set's
reproducible count.

### Phase 2: The LangGraph agent (about 4–5 hours)

**Build**
- Typed state: the HTML, the violations, the one being fixed, attempts,
  candidate fix, scan results, feedback from failed attempts.
- Nodes: `scan` → `pick_violation` → `propose_fix` (structured output:
  fixed HTML + explanation) → `verify` (re-scan, score) → conditional edge:
  accept, retry with the scanner's feedback, or give up after N tries →
  `report`.
- A checkpointer so a long run can resume, and traces of every step.

**Concepts:** graphs versus chains, typed state and reducers, conditional
edges, bounded loops, checkpointing, structured outputs, tracing.
**Done when:** method C beats method A on the dev set; graph diagram saved.

### Phase 3: MCP tools (about 3 hours)

**Build**
- `mcp_server.py` (FastMCP): tools `scan_html`, `scan_url`,
  `verify_fix(original, candidate, rule_id)`, `explain_rule(rule_id)`.
- The agent loads these tools through `langchain-mcp-adapters` instead of
  importing Python functions; `propose_fix` may call `explain_rule` itself.
- Demo: the same server used from an MCP client such as Claude Desktop or the
  MCP Inspector.

**Concepts:** tool schemas, function calling, MCP client and server, stdio
versus HTTP transport, returning errors to the model.
**Done when:** the agent reaches the same dev-set score through MCP tools.

### Phase 4: Retrieval over WCAG (about 4 hours)

**Build**
- Parse the W3C pages into clean sections; chunk by heading (roughly 300–500
  tokens) with metadata: success criterion, document type (understanding,
  technique, failure), technique ID, title, URL.
- Map each axe rule to its success criteria from the rule's tags.
- Index in Chroma (local CPU embeddings) and BM25; merge with reciprocal rank
  fusion; filter to the violated rule's criteria.
- Evaluate retrieval on its own (hit@3, MRR) before plugging it in.

**Concepts:** chunking, embeddings, vector databases, metadata filtering,
hybrid search, retrieval evaluation, context budgets.
**Done when:** retrieval scores are reported and method D is measured.

### Phase 5: Guardrails and human approval (about 3–4 hours)

**Build**
- *Input:* token budget per page; screen page text for prompt injection with
  Meta's Llama Prompt Guard 2 (free on Groq); always pass page content as data,
  never as instructions.
- *Output:* fix must parse as HTML; visible text must be preserved (only
  labels and alt text may be added); reject "cheating" fixes that delete the
  element, hide it (`aria-hidden`, `display:none`), mark meaningful images as
  decorative, or add scripts and event handlers.
- *Verification:* a fix that raises the violation score is never accepted.
- *Action:* LangGraph pauses before applying changes; a person sees the diff
  and approves, edits or rejects.
- Count every rejection by reason; confirm the clean example page is never
  wrongly rejected.

**Concepts:** defence in depth, rule-based versus model-based checks,
prompt injection, reward hacking (fixing the metric instead of the page),
human-in-the-loop.
**Done when:** method E is measured with a rejection table.

### Phase 6: Results and write-up (about 3 hours + run time)

**Build:** run all methods on the test set; error analysis of 20 failures;
cost and latency table; before/after report on a few public pages; README
results and resume bullets.
**Done when:** someone reading only the README can see what the agent does,
how well, at what cost, and where it fails.

### Optional extensions

- Semantic violations (alt text that exists but says nothing) with a vision model.
- A GitHub Action that comments suggested fixes on pull requests.
- Repository-level repair (multi-file projects), as in A11YBench.
- A small web UI for the approval step.

---

## 6. Budget and free-tier limits

Groq free tier, per model, per organisation (October 2026):

| Model | Requests/min | Requests/day | Tokens/min | Tokens/day |
|---|---|---|---|---|
| openai/gpt-oss-120b | 30 | 1,000 | 8,000 | 200,000 |
| openai/gpt-oss-20b | 30 | 1,000 | 8,000 | 200,000 |
| Llama Prompt Guard 2 (86M) | 30 | 14,400 | 15,000 | 500,000 |

Rough cost of one method on all 178 rows: about 1.5k tokens per attempt
(including the model's reasoning) × about 1.3 attempts × 178 ≈ **350k tokens**.

- **Free:** about two days of one model's quota per method; the 40-row dev set
  fits comfortably in a day. All iteration happens here.
- **Paid (optional, final runs):** at $0.15 / $0.60 per million input/output
  tokens for gpt-oss-120b, one method costs roughly $0.10–0.20, and the whole
  study (five methods, two models) under $2.

These are estimates; Phase 1 replaces them with measured numbers.

---

## 7. Risks and how we handle them

| Risk | Mitigation |
|---|---|
| Free-tier limits slow the runs | Cache responses, iterate on the dev set, use both models' quotas, pay ~$1–2 for final runs if needed |
| Easy page-level rules inflate the score | Report results with and without them |
| Our numbers are not directly comparable with the paper | Re-run their methods ourselves on the same rows, model and scorer |
| The model "fixes" by hiding or deleting content | Output guardrails; report how often it tries |
| Very large snippets exceed per-minute limits | Truncate around the failing element or skip, and report the count |
| Similar tools exist (GitHub's Accessibility Scanner, AccessGuru) | Our contribution is measured: open models, verified loop, guardrail statistics, MCP packaging, ablations |
| MCP SDK 2.x is out but the adapter needs 1.x | Pinned `mcp>=1.24,<2` |
| Windows-specific breakage | CI runs on Windows as well as Linux |

---

## 8. Suggested timeline

| Session | Phases | Outcome |
|---|---|---|
| 1 | 0 | Setup passes on your laptop (about 30 minutes) |
| 2 | 1 | Baseline numbers on the dev set |
| 3 | 2 | Working agent beating the baseline |
| 4 | 3 + 4 | MCP tools and retrieval |
| 5 | 5 | Guardrails and approval |
| 6 | 6 | Final runs, README results, resume bullets |
