"""Retrieval over the WCAG documents (Phase 4).

Most tests use HashEmbeddings, a tiny stand-in that needs no download, so
they check the pipeline (chunking, the two rankings, fusion, filters, the
evaluation) rather than the quality of meaning-based search. The tests
marked ``embedding`` use the real bge-small model on a few sentences and run
wherever it has been downloaded (CI does). Retrieval quality over the whole
corpus is measured by ``python -m a11y_agent.retrieval eval``, not here.
"""

import asyncio

import pytest

from a11y_agent.embeddings import CachedEmbeddings, HashEmbeddings, OnnxEmbeddings, model_dir
from a11y_agent.retrieval import (
    CHUNK_SIZE,
    GUIDANCE_TYPES,
    LocalRetriever,
    WcagIndex,
    chunk_documents,
    criteria_of,
    evaluate,
    evaluation_queries,
    load_corpus,
    tokenize,
)

pytestmark = pytest.mark.data


@pytest.fixture(scope="module")
def corpus():
    return load_corpus()


@pytest.fixture(scope="module")
def index(corpus, tmp_path_factory):
    cache = tmp_path_factory.mktemp("index") / "embeddings.sqlite"
    return WcagIndex(chunk_documents(corpus), CachedEmbeddings(HashEmbeddings(), cache))


def test_chunks_follow_the_pages_and_stay_small(corpus):
    chunks = chunk_documents(corpus)
    assert 1500 < len(chunks) < 4000
    assert max(len(c.page_content) for c in chunks) < CHUNK_SIZE + 400          # header line + a little slack
    assert len({c.id for c in chunks}) == len(chunks)
    h37 = [c for c in chunks if c.metadata.get("technique_id") == "H37"]
    assert h37 and all(c.page_content.startswith(h37[0].metadata["title"]) for c in h37)
    assert all(c.metadata["sc"] == h37[0].metadata["sc"] for c in h37)          # metadata kept from the page
    assert not any(c.metadata["section"].lower() == "resources" for c in chunks)  # link lists dropped


def test_tokenizer_keeps_attribute_names_whole():
    assert tokenize("The aria-label attribute, and H37!") == ["aria-label", "attribute", "h37"]


def test_keyword_search_finds_exact_technique_ids(index):
    hits = index.search("H37 alt attributes on img elements", 3, mode="bm25")
    assert hits[0].document.metadata["technique_id"] == "H37"


def test_criteria_filter_and_page_types(index):
    hits = index.search("text alternative for images", 5, criteria=["1.1.1"], doc_types=GUIDANCE_TYPES)
    assert hits and all("1.1.1" in criteria_of(h.document) for h in hits)
    assert all(h.document.metadata["doc_type"] in GUIDANCE_TYPES for h in hits)
    rule = index.search("alternative text", 5, criteria=["9.9.9"], rule_id="image-alt")
    assert [h.document.metadata.get("rule_id") for h in rule] == ["image-alt"]  # only the rule's own text matches


def test_hybrid_search_fuses_both_rankings(index):
    hits = index.search("Links must have discernible text", 10)
    assert all(h.bm25_rank or h.dense_rank for h in hits)
    assert any(h.bm25_rank and h.dense_rank for h in hits)
    assert [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)


def test_langchain_retriever_interface(index):
    docs = index.as_retriever(k=2, criteria=["1.4.3"]).invoke("contrast between text and background")
    assert len(docs) == 2 and all("1.4.3" in criteria_of(d) for d in docs)


def test_local_retriever_returns_guidance_passages(index):
    passages = asyncio.run(LocalRetriever(index).guidance("Images must have alternative text", ["1.1.1"], 3))
    assert len(passages) == 3
    assert {"title", "section", "url", "text", "technique_id"} <= set(passages[0])
    assert all(p["doc_type"] in GUIDANCE_TYPES for p in passages)


def test_evaluation_reports_each_search_mode(index):
    queries = evaluation_queries()
    assert len(queries) > 50 and all(q["criteria"] for q in queries)
    report = evaluate(index, queries[:20])
    assert set(report["modes"]) == {"bm25", "dense", "hybrid"}
    for scores in report["modes"].values():
        assert 0 <= scores["hit@1"] <= scores["hit@3"] <= scores["hit@10"] <= 1
    assert report["modes"]["bm25"]["hit@10"] > 0.5                  # keywords alone already find most criteria


def test_embeddings_are_cached(tmp_path):
    cache = CachedEmbeddings(HashEmbeddings(), tmp_path / "e.sqlite")
    first = cache.embed_documents(["alpha beta", "gamma"])
    again = CachedEmbeddings(HashEmbeddings(), tmp_path / "e.sqlite")
    import numpy as np
    assert np.allclose(again.embed_documents(["alpha beta", "gamma"]), first, atol=1e-6)
    assert cache.computed == 2 and again.computed == 0


real_model = pytest.mark.skipif(not (model_dir() / "model_quantized.onnx").exists(),
                                reason="embedding model not downloaded (python scripts/fetch_resources.py --only embedding-model)")


@pytest.mark.embedding
@real_model
def test_real_embeddings_capture_meaning():
    model = OnnxEmbeddings()
    query = model.embed_query("images must have alternative text")
    passages = model.embed_documents(["Using alt attributes on img elements to provide a text alternative.",
                                      "Ensuring a contrast ratio of at least 4.5:1 between text and background."])
    assert len(query) == 384 and abs(sum(v * v for v in query) - 1) < 1e-3
    near, far = (sum(a * b for a, b in zip(query, p)) for p in passages)
    assert near > far


@pytest.mark.embedding
@real_model
def test_real_embeddings_keep_the_order_of_their_inputs():
    """Texts are batched by length inside the model; the vectors must still come back in order."""
    import numpy as np
    model = OnnxEmbeddings(batch_size=2)
    texts = ["Ensuring sufficient colour contrast between text and its background",
             "Using alt attributes on img elements to provide a text alternative for images.",
             "Providing captions for videos"]
    together = np.array(model.embed_documents(texts))
    one_by_one = np.array([model.embed_documents([t])[0] for t in texts])
    # Not identical: the 8-bit model scales its numbers per batch, so padding
    # shifts a vector slightly. Each vector must still be nearest its own text.
    similarity = together @ one_by_one.T
    assert list(similarity.argmax(axis=1)) == [0, 1, 2]
    assert similarity.diagonal().min() > 0.95


# The headline claim on the real model (hybrid search ranks at least as well as
# keywords alone) needs the whole corpus embedded, which takes minutes, so it is
# not a unit test: CI runs `python -m a11y_agent.retrieval eval --check`.
def test_the_retrieval_check_compares_hybrid_with_keywords():
    from a11y_agent.retrieval import check_report

    def report(hybrid, bm25):
        return {"modes": {"hybrid": {"mrr@10": hybrid}, "bm25": {"mrr@10": bm25}}}
    assert check_report(report(0.73, 0.71)) == ""
    assert check_report(report(0.70, 0.71)) == ""                     # within the tolerance
    assert "below" in check_report(report(0.60, 0.71))


def test_cache_saves_each_part_as_it_goes(tmp_path):
    class Breaks(HashEmbeddings):
        calls = 0

        def embed_documents(self, texts):
            Breaks.calls += 1
            if Breaks.calls == 2:
                raise RuntimeError("stopped")                          # e.g. Ctrl+C halfway through
            return super().embed_documents(texts)
    cache = CachedEmbeddings(Breaks(), tmp_path / "e.sqlite")
    cache.PART = 2
    with pytest.raises(RuntimeError):
        cache.embed_documents(["a", "b", "c", "d"])
    again = CachedEmbeddings(HashEmbeddings(), tmp_path / "e.sqlite")
    again.embed_documents(["a", "b", "c", "d"])
    assert again.computed == 2                                         # "a" and "b" were kept
