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
in seconds.

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

RETRIEVAL_TABLE

In the agent the search is also filtered to the rule's criteria, so every
passage it sees belongs to the right criterion; the measurement above shows
how well search works even without that help, and which mode ranks best.

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
evaluation, and the embedding cache. Two tests marked `embedding` use the
real model wherever it is downloaded (CI): related text must score higher,
and hybrid search must not do worse than keywords alone.

`tests/test_agent.py` checks that retrieved guidance reaches every attempt's
prompt but is fetched once per rule, and that without a retriever nothing
changes. `tests/test_mcp.py` checks the `search_wcag` tool and `agent_rag` in
the evaluation harness. CI also runs the retrieval evaluation with the real
model and shows the numbers on the run page.

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

## What you can now explain

- **RAG's three steps**, and when retrieval is (and is not) the bottleneck.
- **Chunking choices**: heading-aware splits, merging, overlap, context headers, what to drop.
- **Keyword versus dense search**, what an embedding is, query instructions, caching vectors.
- **Hybrid search with reciprocal rank fusion**, and why it fuses ranks, not scores.
- **Metadata filtering** to the violated rule's success criteria.
- **When a vector database is worth it**, and the VectorStore interface that keeps the choice open.
- **Evaluating retrieval on its own**: a query set, a relevance definition, hit@k and MRR.
- **RAG as a graph node** and as an MCP tool.

## Next: Phase 5

Guardrails and human approval: refuse prompt injection in page text, reject
fixes that hide content (the traces already show the model reaching for
visually-hidden headings), never accept a fix that raises the score, and
pause the graph so a person approves the final diff.
