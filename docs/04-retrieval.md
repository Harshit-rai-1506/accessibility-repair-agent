# Phase 4: Retrieval over WCAG — what we built and why

**Goal of this phase:** give the agent the W3C's own guidance for the rule
it is fixing, found by searching the official WCAG documents, and measure
that search on its own before trusting it inside the agent.

**The one idea to take away:** retrieval is a separate system with its own
quality, and it can be measured without a language model. If the right page
is not in the top three results, no prompt can make the model use it.

---

## 1. What retrieval-augmented generation (RAG) is

1. **Retrieve**: search a collection of documents for passages about the question.
2. **Augment**: put the best passages into the prompt.
3. **Generate**: the model answers with that text in front of it.

Why it can help here: the W3C publishes, for every success criterion, the
techniques that meet it ("H37: Using alt attributes on img elements") and the
failures that break it ("F65: omitting the alt attribute"). A model may not
remember the exact rule for a rare ARIA pattern, but it can read a passage.
Why it might not: the round-1 error analysis (docs/02-agent.md §14) found
the agent's failures were mechanical, not about knowledge. Retrieval has to
prove itself in the numbers, like every other phase.

---

## 2. From pages to chunks

```bash
python -m a11y_agent.retrieval build
```

The corpus is what the Phase 0 loaders already produce: 581 WCAG pages
(Understanding documents, success criteria, Techniques, Failures) as Markdown
Documents, plus axe-core's 105 rule descriptions. `chunk_documents` turns
them into **2,357 chunks** (median about 900 characters):

- **Split at headings** with LangChain's `MarkdownHeaderTextSplitter`, so a chunk
  never mixes, say, a technique's "Description" with its "Tests".
- **Merge short neighbours** from the same page ("When to Use" is often one line)
  up to about 1,200 characters, and cut longer sections with
  `RecursiveCharacterTextSplitter` (150 characters of overlap, so a sentence cut
  in two survives in one of the pieces).
- **Drop link lists** ("Resources", "Related Techniques"): they contain many
  keywords and no instructions, so they would match everything.
- **Prefix every chunk** with its page title and section, e.g.
  `Using alt attributes on img elements — Description`. A chunk then says what
  it is about even on its own, for both search indexes and for the model.
- **Keep the metadata** from the page: success criteria, page type, technique id, URL.

Chunk size is a trade-off: smaller chunks are more specific but lose
context; bigger ones cost prompt tokens. About 300 tokens per chunk, three
chunks per prompt, is roughly 1,000 tokens of guidance.

---

## 3. Two ways to search

**Keywords (BM25).** Scores a chunk by how often the query's words appear in
it, weighted by how rare each word is across the corpus. It is exact: it
finds `aria-labelledby` or `H37` when they are typed. It cannot see that
"images need a description" and "text alternatives for img elements" mean
the same thing. (`rank_bm25`, with a tokenizer that keeps names like
`aria-label` whole.)

**Meaning (dense embeddings).** An embedding model turns each chunk into 384
numbers so that texts with similar meaning are close together; a query is
embedded the same way and compared with every chunk (cosine similarity). It
finds paraphrases, and misses exact identifiers more often.

The embedding model is **bge-small-en-v1.5** (BAAI, MIT licence), in its
8-bit ONNX form, run on the CPU with `onnxruntime` (`embeddings.py`). It is
small enough for a basic laptop (34 MB), costs nothing per call, and needs no
API. Like everything else it is downloaded by `fetch_resources.py`, pinned to
a Hugging Face revision and checked against its SHA-256. bge models are told
which text is the question: queries get the prefix "Represent this sentence
for searching relevant passages:", passages do not.

Embedding the 2,357 chunks takes a few minutes on a laptop CPU, once: every
vector is cached in `data/index/embeddings.sqlite`, so later runs load them
in seconds. Two details make that first build less painful. Texts are batched
with others of similar length, because every text in a batch is padded to the
longest one and padding is wasted work. And the cache saves every 256 vectors
as it goes and prints progress, so a build stopped with Ctrl+C keeps what it
finished and the next one carries on from there.

---

## 4. Hybrid search: reciprocal rank fusion

The two rankings are combined with **reciprocal rank fusion (RRF)**: a chunk
gets `1 / (60 + rank)` from each ranking it appears in (top 50 of each), and
the sums are sorted. RRF uses ranks, not raw scores, so it does not matter
that BM25 scores and cosine similarities are on different scales. A chunk
that both methods rank highly wins; a chunk only one method finds still has a
chance.

## 5. Filtering by success criterion

axe-core maps each rule to WCAG success criteria (`image-alt` → 1.1.1). The
agent's search is limited to chunks of those criteria, and to technique,
failure and understanding pages, so a contrast problem never retrieves a
page about captions. Best-practice rules such as `region` have no criterion
and search everything.

## 6. Why there is no vector database

The plan said Chroma. With about 2,000 chunks, comparing a query with every
vector in memory takes milliseconds, so a database would add a process,
a dependency and a format without making anything faster. The dense index is
LangChain's `InMemoryVectorStore`, behind the standard `VectorStore`
interface; swapping in Chroma, pgvector or another store changes one line in
`retrieval.py`. That is worth doing at hundreds of thousands of chunks, or
when several processes must share one index. (chromadb was removed from the
dependencies.)

The index is also a LangChain retriever (`index.as_retriever(...)`), so it
plugs into any LangChain chain.

---

## 7. Measuring retrieval on its own

```bash
python -m a11y_agent.retrieval eval
```

**The test set for retrieval:** one query per axe-core rule that maps to a
success criterion (75 rules). The query is the rule's help text and
description, which is what the agent knows when it searches. A retrieved
chunk is **relevant** if it belongs to one of the rule's criteria. No filter
is applied here: the question is whether search alone finds the right part
of WCAG.

**The measures:** *hit@k* is the share of queries with at least one
relevant chunk in the top *k*; *MRR@10* (mean reciprocal rank) averages
1/rank of the first relevant chunk, so finding it first counts most.

Measured in CI with the real model (2,357 chunks, 75 queries; Linux and Windows gave identical numbers):

| Search | hit@1 | hit@3 | hit@10 | MRR@10 |
|---|---|---|---|---|
| BM25 (keywords) | 0.59 | 0.80 | 0.91 | 0.71 |
| Dense (bge-small embeddings) | 0.60 | 0.77 | 0.89 | 0.70 |
| **Hybrid (RRF)** | **0.64** | **0.84** | **0.91** | **0.74** |

Reading it: keywords and embeddings are about equally good on their own,
and fusing their rankings is best on every measure (tied with keywords on
hit@10): the right criterion is in the top three for 84% of rules, against
80% and 77%.
The gains are a few points, not dramatic, which is typical when the queries
use the same vocabulary as the documents (axe-core's help texts are written
in WCAG's terms). Hybrid search is kept: it ranks best here, and it is the
safer choice for queries that do not share that vocabulary.

**How much a few points mean.** With 75 queries, one query is 1.3 points of
hit@k, so differences of 2–3 points are a couple of queries. The dense
numbers also move a little from machine to machine: BM25 gave exactly the
same scores in every CI run, but the same code gave dense MRR@10 0.72 on one
CI machine and 0.70 on another, because the 8-bit model's arithmetic differs
slightly between CPU types (and with how texts are batched, section 3). Over
four CI runs hybrid MRR@10 stayed between 0.73 and 0.74. Treat differences of
this size as noise; the check below uses a 0.02 tolerance for that reason.

In the agent the search is also filtered to the rule's criteria, so every
passage it sees belongs to the right criterion; the measurement above shows
how well search works even without that help, and which mode ranks best.

**Fusion only helps when both rankings are good.** The same evaluation with
the tests' stand-in embedder (`A11Y_EMBEDDINGS=hash`, which only hashes
words) gives dense MRR@10 0.39, and hybrid 0.64, *below* keywords alone (0.71):
a poor ranking fused with a good one drags it down. So CI runs
`python -m a11y_agent.retrieval eval --check` with the real model on every
push, and the run fails if hybrid search ever ranks worse than keywords
alone (by more than 0.02). That is a regression test for the retrieval
design, not just for the code.

---

## 8. Retrieval in the agent

**A new node.** The graph gained one step (`python -m a11y_agent.agent --graph`):

```
pick --next rule--> retrieve --> propose ...
```

`retrieve` runs once per rule, before the first attempt. It searches for the
rule's help text plus axe-core's first failure message, filtered to the
rule's criteria, and stores three passages in the state (`guidance`).
`propose` adds them to the prompt under "Guidance from the W3C's WCAG
documents", each with its title, section and link. Retries reuse them; they
are not fetched again. Without a retriever in the runtime context the node
does nothing, so the earlier methods are unchanged.

**A new MCP tool.** `search_wcag(query, success_criteria, k, page_types)` on
the MCP server serves the same index to any client. The agent's `retrieve`
node calls it through the MCP session (`McpRetriever`), and in method
`agent_rag` the model may also call it itself, next to `explain_rule` and
`contrast_fix`.

**Method D: `agent_rag`** = `agent_tools` + retrieval. Comparing it with
`agent_tools` on the same items isolates what retrieval adds.

---

## 9. How it is tested

`tests/test_retrieval.py` builds the index with `HashEmbeddings`, a stand-in
that needs no download, and checks the pipeline: chunk sizes and metadata,
link lists dropped, keyword search finding a technique by its id, criteria
filters, fusion using both rankings, the LangChain retriever interface, the
evaluation, the `--check` rule, and the embedding cache (including keeping the
finished part of an interrupted build). Two tests marked `embedding` use the
real model on a few sentences wherever it is downloaded (CI): related text
must score higher, and vectors must come back in the order of their texts
even though they are batched by length. Embedding the whole corpus takes
minutes, so it is not a unit test; the CI step above does it.

`tests/test_agent.py` checks that retrieved guidance reaches every attempt's
prompt but is fetched once per rule, and that without a retriever nothing
changes. `tests/test_mcp.py` checks the `search_wcag` tool and `agent_rag` in
the evaluation harness. CI runs the retrieval evaluation with the real model
on Linux and Windows and shows the numbers on the run page.

---

## 10. Running it

```bash
git pull
pip install -e ".[dev]"
python scripts/fetch_resources.py --only embedding-model     # 34 MB
python scripts/check_setup.py                               # new line: "Embeddings  OK  384 dimensions ..."
python -m a11y_agent.retrieval build                        # embed the corpus once (a few minutes)
python -m a11y_agent.retrieval search "icon link has no accessible name" --sc 2.4.4
python -m a11y_agent.retrieval eval --save
```

Then the evaluation runs for this phase, all on the same model, in one
sitting. The two agent methods are re-run because of the round-2 fixes
(docs/02-agent.md §15); `--fresh` replaces their earlier results.

```bash
python -m a11y_agent.evaluate run --method agent --fresh
python -m a11y_agent.evaluate run --method agent_tools --fresh
python -m a11y_agent.evaluate run --method agent_rag
python -m a11y_agent.evaluate report --save
```

---

## 11. What retrieval did in the agent: dev results

All methods on Mistral Small 4, the same 40 dev items, after the round-2
agent fixes (docs/02-agent.md §15):

| Method | Score decrease | Resolved | Element-level (26) | Page-level resolved (14) | Broke another rule | Tokens per item |
|---|---|---|---|---|---|---|
| Zero-shot | 0.61 | 23/40 | 0.62 | 8 | 7 | 1,825 |
| Agent (C) | 0.68 | 28/40 | 0.66 | 10 | 0 | 7,721 |
| **Agent + MCP tools** | **0.78** | **31/40** | **0.72** | **13** | 2 | 15,710 |
| Agent + tools + retrieval (D) | 0.64 | 26/40 | 0.64 | 9 | 1 | 19,760 |

**Retrieval did not help: 0.64 against 0.78, at 26% more tokens.** Two
things make the size of that gap uncertain, and the traces explain it.

**How much is chance.** Item by item, `agent_rag` lost 7 items that
`agent_tools` fixed and won 2. That is lopsided, but any two agent variants
here disagree on about 10 of 40 items (the plain agent and `agent_tools`
split 4 and 7), and a split of 7 to 2 happens by chance about one time in
five (sign test, p ≈ 0.18). Most of the gap is on the page-level rules
(13 against 9 resolved); on the 26 element-level items the two resolved 18
and 17.

**What went wrong on the 7 losses.** The passages were on topic: H42 "Using
h1-h6 to identify headings" for a missing heading, ARIA11 and H101 for a
missing `main` landmark, G18 for contrast. None of the failed edits copied
anything from them. The failures are the mechanical ones from the earlier
error analyses: a `find` text that is not in the page (`</html>` in a
fragment that has none, `<<start>>` followed by page text), repeated after
the feedback said so, and long pages running out of attempts. With three
passages more in every prompt, the model's attempts took a different path,
and on these items a worse one. Recovering from a failed attempt is
sensitive to small prompt changes; adding knowledge does not fix it.

So this is the result the round-1 analysis (docs/02-agent.md §14) predicted:
on this dev set the agent fails on mechanics, not on not knowing WCAG, and
retrieval cannot help with mechanics.

**Two retrieval defects seen in the traces**, worth fixing before the test
run but not by tuning on these 40 items:

- *Duplicates.* The three passages are often from the same page (ARIA11
  twice, ARIA14 three times), so the prompt carries one technique instead of
  three. One passage per page would fix it.
- *Rules without a success criterion.* Best-practice rules search everything,
  and `empty-heading` got F46 (table headers) and ARIA7 (link purpose).

**Decision.** `agent_tools` stays the main method, and Phase 5's guardrails
are built on it (method E = agent + tools + guardrails). Retrieval stays in
the comparison as method D and is reported on the test set as measured: a
negative result for a popular technique is a finding, not a failure. The
retrieval system itself works (the right criterion in the top three for 84%
of rules); what this shows is that in this task, the model's bottleneck was
elsewhere.

**What the results hand to Phase 5.** Reading every accepted fix turned up
three things the scanner does not object to:

- *Leftover markup.* An accepted `agent_tools` fix for `page-has-heading-one`
  ends the page with a literal `<</end>>` (a mistyped anchor) and puts
  `<main>` before `<html>`. axe-core passes it.
- *Invented text.* Every accepted fix for `page-has-heading-one`, in all
  three agent methods, adds a heading the model made up: "Page Content",
  "Page Title", "Hauptinhalt". The scanner is satisfied; a reader is not.
  Only a person knows what the heading should say.
- *Hidden text.* Attempts like `<span class="visually-hidden">Featured</span>`
  inside an empty heading satisfy the rule for screen readers while sighted
  users see nothing.

---

## What you can now explain

- **RAG's three steps**, and when retrieval is (and is not) the bottleneck.
- **Chunking choices**: heading-aware splits, merging, overlap, context headers, what to drop.
- **Keyword versus dense search**, what an embedding is, query instructions, caching vectors.
- **Hybrid search with reciprocal rank fusion**, and why it fuses ranks, not scores.
- **Metadata filtering** to the violated rule's success criteria.
- **When a vector database is worth it**, and the VectorStore interface that keeps the choice open.
- **Evaluating retrieval on its own**: a query set, a relevance definition, hit@k and MRR.
- **RAG as a graph node** and as an MCP tool.
- **Reading an ablation honestly**: item-level flips, chance, and where the bottleneck really is.

## Next: Phase 5

Guardrails and human approval: refuse prompt injection in page text, reject
fixes that hide content (the traces already show the model reaching for
visually-hidden headings), never accept a fix that raises the score, and
pause the graph so a person approves the final diff.
