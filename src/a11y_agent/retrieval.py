"""Retrieval over the W3C's WCAG documents (the "R" in RAG), and how good it is.

The pipeline, built from the Documents the Phase 0 loaders already produce:

    581 WCAG pages + 105 axe rules (LangChain Documents, Markdown)
      -> chunks        split at headings, then to about 1,200 characters each
      -> two indexes   BM25 (keywords) and dense vectors (bge-small embeddings)
      -> hybrid search reciprocal rank fusion of the two rankings,
                       optionally filtered to the violated rule's success criteria

Why two indexes? Keywords find exact terms ("aria-labelledby", "H37");
embeddings find meaning ("images need a text alternative" matches "alt
attributes on img elements"). Each misses what the other finds; fusing their
rankings is a simple, robust way to get both.

Why no vector database? The whole corpus is about 2,000 chunks, so comparing
a query with every vector takes milliseconds in memory. The dense index is
LangChain's ``InMemoryVectorStore``; replacing it with Chroma, pgvector or
any other LangChain vector store changes one line, which is worth doing when
the corpus reaches hundreds of thousands of chunks or several processes need
the same index.

Commands (from the repository root):

    python -m a11y_agent.retrieval build                      # chunk and embed (first time: a few minutes)
    python -m a11y_agent.retrieval search "link has no text" --sc 2.4.4
    python -m a11y_agent.retrieval eval                       # hit@k and MRR for BM25, dense and hybrid
    python -m a11y_agent.retrieval eval --save                # also write results/retrieval/
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from functools import lru_cache

from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.retrievers import BaseRetriever
from langchain_core.vectorstores import InMemoryVectorStore
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter
from rank_bm25 import BM25Okapi

from .config import ROOT

CHUNK_SIZE = 1200          # characters, about 300 tokens: small enough to be specific, big enough to make sense
CHUNK_OVERLAP = 150        # repeated between neighbouring chunks, so a sentence cut in two is not lost
RRF_K = 60                 # the usual constant in reciprocal rank fusion
CANDIDATES = 50            # how far down each ranking fusion looks
GUIDANCE_TYPES = ("technique", "failure", "understanding")   # W3C pages that say how to meet a criterion
WCAG_TYPES = GUIDANCE_TYPES + ("success_criterion",)
RESULTS_DIR = ROOT / "results" / "retrieval"

_WORD = re.compile(r"[a-z0-9][a-z0-9-]*")
_STOP = frozenset("a an and are as at be by for from has have in is it its of on or that the this to with "
                  "which when must should can not no be been".split())


def tokenize(text: str) -> list[str]:
    """Lower-case words for BM25; keeps hyphenated names like aria-label whole."""
    return [w for w in _WORD.findall(text.lower()) if w not in _STOP]


def criteria_of(doc: Document) -> set[str]:
    """Success criteria a chunk belongs to (the loaders store them comma-separated)."""
    return {c for c in (doc.metadata.get("sc") or "").split(",") if c}


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

# Sections that are only lists of links: they match many queries and tell the model nothing.
SKIP_SECTIONS = frozenset({"resources", "related resources", "related techniques"})


def chunk_documents(docs: list[Document]) -> list[Document]:
    """Split each page at its headings, merge short neighbouring sections, cut long ones.

    The result is chunks of up to about CHUNK_SIZE characters that follow the
    page's own structure. Each chunk starts with the page title and section
    ("Using alt attributes on img elements — Description"), so on its own it
    still says what it is about: both indexes see that context, and so does
    the model when the chunk is shown to it. Link-list sections are dropped.
    """
    by_heading = MarkdownHeaderTextSplitter(headers_to_split_on=[("#", "h1"), ("##", "h2"), ("###", "h3")])
    by_size = RecursiveCharacterTextSplitter(chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP)
    chunks = []
    for doc in docs:
        sections = []
        for section in by_heading.split_text(doc.page_content):
            name = section.metadata.get("h3") or section.metadata.get("h2") or ""
            if name.strip().lower() in SKIP_SECTIONS or not section.page_content.strip():
                continue
            sections.append((name, section.page_content.strip()))

        groups: list[tuple[list[str], str]] = []          # (section names, text), merged up to CHUNK_SIZE
        for name, text in sections:
            if groups and len(groups[-1][1]) + len(text) + 2 <= CHUNK_SIZE:
                names, merged = groups[-1]
                groups[-1] = (names + [name], f"{merged}\n\n{text}")
            else:
                groups.append(([name], text))

        number = 0
        for names, text in groups:
            path = "; ".join(n for n in dict.fromkeys(names) if n)
            header = doc.metadata.get("title", "") + (f" — {path}" if path else "")
            for piece in by_size.split_text(text):
                chunks.append(Document(
                    id=f"{doc.id}#{number}",
                    page_content=f"{header}\n{piece}",
                    metadata={**doc.metadata, "section": path, "parent_id": doc.id, "chunk": number},
                ))
                number += 1
    return chunks


def load_corpus() -> list[Document]:
    """The Phase 0 Documents: WCAG pages and axe-core rules."""
    from .config import settings
    from .loaders import AxeRuleLoader, WCAGIndex, WCAGLoader

    wcag = WCAGLoader()
    index = WCAGIndex.build(settings.corpus_dir / "wcag")
    return wcag.load() + AxeRuleLoader(index=index).load()


# ---------------------------------------------------------------------------
# The hybrid index
# ---------------------------------------------------------------------------

@dataclass
class Hit:
    document: Document
    score: float                  # fused score (or the single ranking's score)
    bm25_rank: int | None = None  # 1 = best; None = not in that ranking's top CANDIDATES
    dense_rank: int | None = None

    def as_dict(self, max_chars: int = 900) -> dict:
        m = self.document.metadata
        text = self.document.page_content.split("\n", 1)[-1].strip()
        return {"title": m.get("title", ""), "section": m.get("section", ""), "url": m.get("url", ""),
                "doc_type": m.get("doc_type", ""), "technique_id": m.get("technique_id", ""),
                "sc": m.get("sc", ""), "text": text if len(text) <= max_chars else text[: max_chars - 1] + "…"}


class WcagIndex:
    """BM25 and dense search over the chunks, fused with reciprocal rank fusion."""

    def __init__(self, chunks: list[Document], embeddings: Embeddings):
        self.chunks = chunks
        self.embeddings = embeddings
        self.vectors = InMemoryVectorStore(embedding=embeddings)
        self.vectors.add_documents(chunks, ids=[c.id for c in chunks])
        self.bm25 = BM25Okapi([tokenize(c.page_content) for c in chunks])

    @classmethod
    def build(cls, embeddings: Embeddings | None = None, docs: list[Document] | None = None) -> "WcagIndex":
        from .embeddings import default_embeddings
        return cls(chunk_documents(docs if docs is not None else load_corpus()), embeddings or default_embeddings())

    @staticmethod
    def _filter(criteria: list[str] | None, doc_types: tuple[str, ...] | None, rule_id: str | None):
        wanted = set(criteria or [])

        def allowed(doc: Document) -> bool:
            m = doc.metadata
            if rule_id and m.get("doc_type") == "axe_rule" and m.get("rule_id") == rule_id:
                return True                                 # the rule's own description always qualifies
            if doc_types and m.get("doc_type") not in doc_types:
                return False
            return not wanted or bool(wanted & criteria_of(doc))
        return allowed

    def bm25_ranking(self, query: str, allowed, n: int) -> list[tuple[Document, float]]:
        scores = self.bm25.get_scores(tokenize(query))
        order = sorted(range(len(scores)), key=lambda i: -scores[i])
        ranked = [(self.chunks[i], float(scores[i])) for i in order if scores[i] > 0 and allowed(self.chunks[i])]
        return ranked[:n]

    def dense_ranking(self, query: str, allowed, n: int) -> list[tuple[Document, float]]:
        return self.vectors.similarity_search_with_score(query, k=n, filter=allowed)

    def search(self, query: str, k: int = 3, *, criteria: list[str] | None = None,
               doc_types: tuple[str, ...] | None = None, rule_id: str | None = None,
               mode: str = "hybrid") -> list[Hit]:
        """The ``k`` best chunks. ``criteria`` (e.g. ["1.1.1"]) keeps only chunks for those
        success criteria; ``doc_types`` keeps only those page types; ``mode`` is
        "hybrid", "bm25" or "dense"."""
        allowed = self._filter(criteria, doc_types, rule_id)
        if mode == "bm25":
            return [Hit(d, s, bm25_rank=r) for r, (d, s) in enumerate(self.bm25_ranking(query, allowed, k), 1)]
        if mode == "dense":
            return [Hit(d, s, dense_rank=r) for r, (d, s) in enumerate(self.dense_ranking(query, allowed, k), 1)]

        fused: dict[str, Hit] = {}
        for name, ranking in (("bm25", self.bm25_ranking(query, allowed, CANDIDATES)),
                              ("dense", self.dense_ranking(query, allowed, CANDIDATES))):
            for rank, (doc, _) in enumerate(ranking, start=1):
                hit = fused.setdefault(doc.id, Hit(doc, 0.0))
                hit.score += 1.0 / (RRF_K + rank)            # reciprocal rank fusion
                setattr(hit, f"{name}_rank", rank)
        return sorted(fused.values(), key=lambda h: -h.score)[:k]

    def as_retriever(self, **search_options) -> "WcagRetriever":
        return WcagRetriever(index=self, search_options=search_options)


class WcagRetriever(BaseRetriever):
    """The index as a LangChain retriever: ``retriever.invoke("question") -> [Document, ...]``."""

    index: object
    search_options: dict = {}

    model_config = {"arbitrary_types_allowed": True}

    def _get_relevant_documents(self, query: str, *, run_manager: CallbackManagerForRetrieverRun) -> list[Document]:
        return [h.document for h in self.index.search(query, **self.search_options)]


@lru_cache(maxsize=1)
def shared_index() -> WcagIndex:
    """One index per process (the MCP server and the CLI build it on first use)."""
    return WcagIndex.build()


class LocalRetriever:
    """What the agent's ``retrieve`` node needs, from an index in this process."""

    def __init__(self, index: WcagIndex | None = None):
        self._index = index

    async def guidance(self, query: str, criteria: list[str] | None, k: int = 3) -> list[dict]:
        index = self._index or shared_index()
        return [h.as_dict() for h in index.search(query, k, criteria=criteria, doc_types=GUIDANCE_TYPES)]


# ---------------------------------------------------------------------------
# Measuring retrieval on its own
# ---------------------------------------------------------------------------

def evaluation_queries() -> list[dict]:
    """One query per axe-core rule that maps to a WCAG success criterion.

    The query is the rule's own help text and description (what the agent
    knows about a violation); a retrieved chunk counts as relevant if it
    belongs to one of the rule's success criteria. Best-practice rules have
    no criterion and are left out.
    """
    from .prompts import axe_rule_text
    from .loaders import AxeRuleLoader

    queries = []
    for doc in AxeRuleLoader().load():
        criteria = sorted(criteria_of(doc))
        if criteria:
            help_text, description = axe_rule_text(doc.metadata["rule_id"])
            queries.append({"rule_id": doc.metadata["rule_id"], "query": f"{help_text}. {description}",
                            "criteria": criteria})
    return queries


def evaluate(index: WcagIndex, queries: list[dict] | None = None, k_max: int = 10) -> dict:
    """hit@1, hit@3, hit@10 and MRR@10 for each search mode, without any criteria filter."""
    queries = queries if queries is not None else evaluation_queries()
    results = {}
    for mode in ("bm25", "dense", "hybrid"):
        ranks = []
        for q in queries:
            hits = index.search(q["query"], k_max, doc_types=WCAG_TYPES, mode=mode)
            wanted = set(q["criteria"])
            rank = next((i for i, h in enumerate(hits, 1) if wanted & criteria_of(h.document)), None)
            ranks.append(rank)
        n = len(ranks) or 1
        results[mode] = {
            "hit@1": round(sum(r == 1 for r in ranks if r) / n, 3),
            "hit@3": round(sum(r <= 3 for r in ranks if r) / n, 3),
            "hit@10": round(sum(1 for r in ranks if r) / n, 3),
            "mrr@10": round(sum(1 / r for r in ranks if r) / n, 3),
        }
    return {"queries": len(queries), "chunks": len(index.chunks),
            "embeddings": getattr(index.embeddings, "model_id", type(index.embeddings).__name__),
            "modes": results}


def eval_table(report: dict) -> str:
    lines = ["| Search | hit@1 | hit@3 | hit@10 | MRR@10 |", "|---|---|---|---|---|"]
    for mode in ("bm25", "dense", "hybrid"):
        r = report["modes"][mode]
        lines.append(f"| {mode} | {r['hit@1']:.2f} | {r['hit@3']:.2f} | {r['hit@10']:.2f} | {r['mrr@10']:.2f} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Retrieval over the WCAG documents.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("build", help="chunk the corpus and embed every chunk (cached)")
    find = sub.add_parser("search", help="try a query")
    find.add_argument("query")
    find.add_argument("--sc", nargs="+", help="only these success criteria, e.g. 1.1.1")
    find.add_argument("-k", type=int, default=3)
    find.add_argument("--mode", choices=["hybrid", "bm25", "dense"], default="hybrid")
    ev = sub.add_parser("eval", help="measure retrieval: hit@k and MRR per search mode")
    ev.add_argument("--save", action="store_true", help="write results/retrieval/")
    ev.add_argument("--annotate", action="store_true", help="also print GitHub Actions notices")
    ev.add_argument("--check", action="store_true",
                    help="exit with an error if hybrid search ranks worse than keywords alone (used in CI)")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    started = time.perf_counter()
    index = WcagIndex.build()
    computed = getattr(index.embeddings, "computed", 0)
    built = (f"{len(index.chunks)} chunks indexed in {time.perf_counter() - started:.1f} s "
             f"({computed} newly embedded, the rest from the cache)")
    print(built)
    if getattr(args, "annotate", False):
        print(f"::notice title=retrieval index::{built}")
    if args.command == "build":
        return 0
    if args.command == "search":
        for i, hit in enumerate(index.search(args.query, args.k, criteria=args.sc, mode=args.mode), 1):
            d = hit.as_dict(400)
            print(f"\n[{i}] {d['title']}" + (f" — {d['section']}" if d["section"] else "")
                  + f"  (score {hit.score:.4f}; bm25 #{hit.bm25_rank}, dense #{hit.dense_rank})\n{d['url']}\n{d['text']}")
        return 0

    report = evaluate(index)
    table = eval_table(report)
    print(f"\n{report['queries']} queries (one per axe rule with a WCAG criterion), "
          f"{report['chunks']} chunks, embeddings: {report['embeddings']}\n\n{table}")
    if args.annotate:
        for mode, r in report["modes"].items():
            print(f"::notice title=retrieval {mode}::hit@1 {r['hit@1']} hit@3 {r['hit@3']} hit@10 {r['hit@10']} "
                  f"MRR@10 {r['mrr@10']} ({report['queries']} queries, {report['chunks']} chunks, {report['embeddings']})")
    if args.save:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        (RESULTS_DIR / "summary.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        (RESULTS_DIR / "README.md").write_text(
            "# Retrieval results\n\nMade with `python -m a11y_agent.retrieval eval --save`.\n\n"
            f"{report['queries']} queries, one per axe-core rule that maps to a WCAG success criterion "
            "(the rule's help text and description). A result counts as relevant if it belongs to one of the "
            f"rule's criteria. {report['chunks']} chunks; embeddings: {report['embeddings']}; no criteria "
            "filter.\n\n" + table + "\n", encoding="utf-8")
        print(f"\nSaved to {RESULTS_DIR.relative_to(ROOT)}")
    if args.check:
        problem = check_report(report)
        if problem:
            print(f"::error title=retrieval check::{problem}" if args.annotate else f"\nCheck failed: {problem}")
            return 1
        print("\nCheck passed: hybrid search ranks at least as well as keywords alone.")
    return 0


def check_report(report: dict, tolerance: float = 0.02) -> str:
    """The Phase 4 claim, checked on every CI run: fusing meaning and keywords must not hurt.

    Returns a description of the problem, or "" when the report is fine.
    """
    hybrid, bm25 = report["modes"]["hybrid"]["mrr@10"], report["modes"]["bm25"]["mrr@10"]
    if hybrid < bm25 - tolerance:
        return f"hybrid MRR@10 {hybrid} is below keyword-only {bm25} (tolerance {tolerance})"
    return ""


if __name__ == "__main__":
    sys.exit(main())
