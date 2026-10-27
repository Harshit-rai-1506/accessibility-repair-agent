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
| Second provider | Mistral Small 4 (`mistral-small-2603`) on Mistral's API | Open weights (Apache 2.0), same price as gpt-oss-120b, a free tier without Groq's 8,000-token request limit, so full dev runs fit in one sitting. |
| Offline option | Any Ollama model, e.g. `qwen2.5-coder:3b` | Works without internet, but slow on a laptop CPU, so not the default. |
| Ground truth | axe-core **4.12.1**, pinned and checksum-verified | The industry-standard rule engine; pinning keeps results reproducible. |
| Rules checked | WCAG 2.0/2.1/2.2 A, AA, AAA + best practice | Matches the benchmark, which includes AAA contrast and best-practice rules. |
| Benchmark | AccessGuru (ASSETS 2025), plus ACCESS (Huang et al. 2024) | Real violations from real sites, a published metric and published baselines. |
| Framework | LangGraph | Explicit state, loops with limits, checkpoints, built-in pause for human approval. |
| Tool protocol | MCP Python SDK 1.x + `langchain-mcp-adapters` 0.3 | The adapter requires `mcp < 2`, so we pin the 1.x line. |
| Vector store | LangChain `InMemoryVectorStore` + BM25 keyword search (planned: Chroma) | About 2,000 chunks: an in-memory search takes milliseconds; one line swaps in Chroma or pgvector if the corpus grows. |
| Embeddings | bge-small-en-v1.5 (MIT), int8 ONNX on the CPU | 34 MB, free, offline after one download; pinned and checksum-verified. |
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
| ACCESS (Huang et al.) | 171 | **100** (26 rules) | Second test set |

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
| E | Our agent + tools + guardrails (no retrieval: it did not help on dev) | Cost of safety, in score and in rejected fixes |

Main model: gpt-oss-120b. Repeat A and E on gpt-oss-20b to see how much the
larger model matters. The paper's published numbers (0.84 GPT-4, 0.82
Mistral-7B) are shown for context only: they used axe 4.4.1 and scored
non-reproducible rows with hand-written rules, so they are not directly
comparable.

---

## 5. Phases

### Phase 0: Setup and resources ✅

**Built:** project packaging, pinned and checksum-verified downloads,
LangChain document loaders for every resource, the scanner, the scoring
metric, the model client, a setup checker, 41 tests, and CI on Linux and
Windows.
**Guide:** [docs/00-setup.md](00-setup.md)
**Done when:** `check_setup.py` passes on your laptop and CI is green.

### Phase 1: Benchmark and baseline ✅

**Built**
- `benchmark.py`: reproducibility scan and a seeded, rule-stratified split,
  committed as `splits/accessguru.json` (dev 40 / test 138) and
  `splits/access.json` (test 100).
- `prompts.py`, `methods.py`: `noop` (harness check), `zero_shot` (method A)
  and `accessguru` (method B, prompts ported verbatim from the authors' code).
- `model_client.py`: SQLite reply cache, token- and request-aware pacing,
  `too_large` detection, clean stops at the daily quota or a rejected key.
- `evaluate.py`: resumable runs, per-item JSONL, summaries, a like-for-like
  table on items every method could send, and a content-kept check.
- `.github/workflows/evaluate.yml`: run evaluations on GitHub with the key
  stored as a repository secret.
- Guide: [docs/01-evaluation.md](01-evaluation.md). 82 tests, using a
  scripted fake model so no key is needed.

**Measured while building:** ACCESS reproduces 100 of 171 rows. On the free
tier's 8,000-token request limit, 3 dev items are too large for zero-shot and
about 9 for AccessGuru, whose multi-turn conversation costs about 8 times the
tokens (~42K vs ~350K for the dev set).

**Measured on the dev set** (gpt-oss-120b, free tier; `results/accessguru-dev/`):

| Method | Items | Score decrease | Resolved | Broke another rule | Dropped content | Too large | Tokens per item |
|---|---|---|---|---|---|---|---|
| A zero-shot | 40/40 | 0.51 | 21 | 11 | 5 | 3 | 930 |
| B AccessGuru | 14/40 (quota) | 0.40 | 6 | 0 | 3 | 7 | 12,647 |

On the 7 items both could send: A 0.83, B 0.87 at about 40 times the tokens.
The failure modes (broken rules, deleted content, requests too large) shaped
Phase 2. B continues from item 15 on its next run.

**Concepts:** evaluation harnesses, caching, rate limits, parsing model output,
dev/test discipline.
**Done when:** a table with methods A and B on the dev set. ✅

### Phase 2: The LangGraph agent ✅ built

**Built**
- `agent.py`: a `StateGraph` with typed, serializable state and reducers
  (`operator.add` for results, a custom per-rule reducer for feedback); nodes
  `scan` → `pick` → `propose` → `verify` → `accept` / `give_up` → `report`;
  conditional edges for the retry loop, bounded by `max_attempts` (3) and
  `recursion_limit`; scanner and model passed as runtime context; streaming
  steps; CLI that fixes a whole page.
- `edits.py`: structured output as JSON edits (Pydantic `FixProposal`, sent as a
  strict JSON schema), all-or-nothing application, excerpts of long pages.
- `verify` passes a fix only if the target rule is gone, nothing else newly
  fails, and 90% of words and elements remain (`content.py`); failures are fed
  back with axe-core's own explanation.
- Checkpoints: `AsyncSqliteSaver` with `--thread` resumes a stopped run;
  `InMemorySaver` and `--history` show the state after each step.
- `model_client.py`: `response_format` support, fallback when a provider
  rejects schemas, `BadReply` for schema failures.
- `methods.py`: method C (`agent`); `check_setup.py` checks the model returns
  usable edits. Guide: [docs/02-agent.md](02-agent.md). 116 tests.

**Measured while building:** the agent's first-attempt prompt averages about
1,060 estimated tokens on dev (zero-shot: about 690), but its replies are short
edits, and every dev item fits the free-tier request limit (the largest
benchmark snippet, about 10,800 tokens, becomes a 4,700-token request).

**Measured on dev (Mistral Small 4, 26 Oct):** agent 0.59 (0 deletions, 2
broke another rule) against zero-shot 0.61 and AccessGuru 0.75 (0.47 when
deletions count as failures). Error analysis found five mechanical causes
(duplicate text, fragments, new content tripping `region`, many failing
elements, repeated attempts); fixed with `replace_all`, start/end anchors,
pass conditions in feedback, a scaled reply budget and kept progress
([02-agent.md §14](02-agent.md#14-error-analysis-round-1)). Re-run pending.

**Concepts:** graphs versus chains, typed state and reducers, conditional
edges, bounded loops, structured outputs, context budgets, runtime context,
checkpointing, streaming and tracing, testing with fake models, error analysis.
**Done when:** method C beats method A on the dev set (graph diagram:
[docs/02-agent.md §3](02-agent.md#3-the-graph)). *Remaining:* the first dev
run with a real key (`evaluate run --method agent`).

### Phase 3: MCP tools ✅ built

**Built**
- `mcp_server.py` (FastMCP, MCP SDK 1.x): tools `scan_html`, `scan_url`
  (public addresses only), `verify_fix`, `explain_rule`, `contrast_fix`, with
  typed input and output schemas and read-only annotations; resource
  `axe-rule://{rule_id}`; prompt `repair_violation`; one browser per server
  (lifespan); errors returned as error results; stdio and streamable HTTP.
- `contrast.py`: the WCAG contrast formula and the closest passing colours,
  checked against axe-core in a test. `verification.py`: one judgement of a
  fix, shared by the agent's `verify` node and the `verify_fix` tool.
- `mcp_client.py`: one stdio session per run; `McpScanner` for the graph's
  scans; `explain_rule` and `contrast_fix` loaded with `langchain-mcp-adapters`
  as tools the model may call.
- Agent: a bounded tool loop inside `propose` (final answer = `submit_fix`
  tool, `tool_choice` "required" then forced, 3 rounds, budget-aware, cached);
  fallbacks for providers without forced tool choice; CLI `--tools`; method
  `agent_tools`. The graph itself is unchanged.
- `scripts/try_mcp.py`, an MCP check in `check_setup.py`, guide
  [docs/03-mcp-tools.md](03-mcp-tools.md) (MCP Inspector and Claude desktop
  app setup). 139 tests.

**Measured while building:** the agent through MCP gives exactly the same
results as in-process (test); tool definitions add about 500 tokens per
request; for accessguru-004 the contrast tool suggests #00538f (7.06:1), which
axe-core accepts.

**Concepts:** tool schemas, function calling, MCP client and server, tools /
resources / prompts, stdio versus HTTP transport, returning errors to the
model, tool-server security.
**Done when:** the agent reaches the same dev-set score through MCP tools
(plumbing: ✅ identical results in tests); `agent_tools` measured on dev.
*Remaining:* the dev runs of `agent` and `agent_tools`.

### Phase 4: Retrieval over WCAG ✅ done

**Built**
- `retrieval.py`: the Phase 0 Documents (581 WCAG pages, 105 axe rules) cut
  into 2,357 chunks: split at headings, short sections merged, long ones cut
  (about 1,200 characters, 150 overlap), link lists dropped, every chunk
  prefixed with its page title and section.
- Two indexes: BM25 keywords and dense vectors (`embeddings.py`: bge-small
  on the CPU, cached in SQLite), fused with reciprocal rank fusion; filters
  by success criterion (from each axe rule's tags) and page type; a
  LangChain retriever.
- Retrieval measured on its own: one query per axe rule with a criterion
  (75), hit@1/3/10 and MRR@10 per search mode, in CI with the real model
  (numbers in [04-retrieval.md](04-retrieval.md#7-measuring-retrieval-on-its-own)).
- The agent: a `retrieve` node (once per rule) puts three W3C passages in the
  prompt; MCP tool `search_wcag`; method D `agent_rag` (agent_tools + retrieval).
- CI fails if hybrid search ranks worse than keywords alone
  (`retrieval eval --check`, Linux and Windows).
- Found on Windows: a stdio MCP server deadlocked on its first numpy import
  while waiting for the next message; fixed by giving the protocol its own
  stdin handle (`own_stdin`, [03-mcp-tools.md](03-mcp-tools.md#2-the-server)).
- Guide: [docs/04-retrieval.md](04-retrieval.md). 177 tests.

**Concepts:** chunking, embeddings, vector stores (and when not to use a
database), metadata filtering, hybrid search, retrieval evaluation, context
budgets, RAG as a graph node and as a tool.
**Done when:** retrieval scores are reported (✅ in CI) and method D is
measured (✅ 27 Oct, dev).

**Result (dev, Mistral Small 4):** agent 0.68, agent + tools 0.78, agent +
tools + retrieval 0.64 at 26% more tokens. The passages were on topic; the
losses are the agent's mechanical failures taking a different path, and a
7-to-2 item split is within chance on 40 items
([04-retrieval.md §11](04-retrieval.md#11-what-retrieval-did-in-the-agent-dev-results)).
**Decision:** method E builds on agent + tools without retrieval; D stays in
the final comparison. Before the test run: one passage per page in the
guidance (duplicates seen), not tuned on dev.

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

Estimated from the real prompts and snippets (Phase 1; see
[01-evaluation.md §8](01-evaluation.md#8-budget-what-a-run-costs)):

| Split | zero_shot | accessguru |
|---|---|---|
| dev (40) | ~42K tokens, one sitting | ~350K tokens, 2 days free |
| test (138) | ~156K tokens, one day | ~1.35M tokens, 7 days free |

- **Free:** all development on the dev set; zero-shot fits in a day, AccessGuru
  needs two (runs resume automatically).
- **Paid (optional, final runs):** at $0.15 / $0.60 per million input/output
  tokens for gpt-oss-120b, the whole test-set comparison costs well under $1,
  and removes the `too_large` limit.
- **Measured:** zero-shot used 37K tokens for the 40 dev items; AccessGuru
  used 177K for its first 14 (one day's quota). The agent is estimated at
  60–120K for the dev set.

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
