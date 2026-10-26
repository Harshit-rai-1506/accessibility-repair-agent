"""Turning text into vectors on your own CPU, for retrieval (Phase 4).

An *embedding* is a list of numbers (here 384) that places a piece of text in
a space where texts with similar meaning are close together. Retrieval embeds
every chunk of the WCAG documents once, then embeds each question and returns
the nearest chunks. That finds "img elements need a text alternative" for the
question "images must have alt text" even though the words differ, which a
keyword search cannot.

Three classes, all LangChain ``Embeddings`` (``embed_documents`` and
``embed_query``), so any LangChain vector store can use them:

``OnnxEmbeddings``
    BAAI's bge-small-en-v1.5 (MIT licence), 8-bit ONNX, run with
    onnxruntime. Downloaded once by ``scripts/fetch_resources.py`` (34 MB,
    pinned and checksum-verified); no GPU, no API key, no cost per call.

``CachedEmbeddings``
    Wraps another embedder and stores every vector in SQLite, keyed by the
    model and the text. The corpus is embedded once (a few minutes on a laptop
    CPU, saved as it goes); later runs load the vectors in seconds.

``HashEmbeddings``
    A tiny stand-in with no download: words are hashed into 256 buckets. It
    captures shared words only, not meaning, so it is for tests (and set with
    ``A11Y_EMBEDDINGS=hash``), never for real results.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import numpy as np
from langchain_core.embeddings import Embeddings

from .config import settings

MODEL_DIR = "bge-small-en-v1.5"
# bge models embed questions with this instruction, and passages without it.
QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "


def model_dir() -> Path:
    return settings.data_dir / "models" / MODEL_DIR


class OnnxEmbeddings(Embeddings):
    """bge-small-en-v1.5 on the CPU: tokenise, run the network, take the [CLS] vector, normalise."""

    model_id = "bge-small-en-v1.5-int8"
    dimensions = 384

    def __init__(self, directory: Path | None = None, batch_size: int = 16, max_length: int = 512):
        self.directory = Path(directory or model_dir())
        self.batch_size = batch_size
        self.max_length = max_length
        self._session = None
        self._tokenizer = None

    def _load(self) -> None:
        if self._session is not None:
            return
        model, tokenizer = self.directory / "model_quantized.onnx", self.directory / "tokenizer.json"
        if not model.exists() or not tokenizer.exists():
            raise FileNotFoundError(f"embedding model not found in {self.directory}. "
                                    "Run: python scripts/fetch_resources.py --only embedding-model")
        import onnxruntime
        from tokenizers import Tokenizer

        options = onnxruntime.SessionOptions()
        options.log_severity_level = 3                      # warnings and errors only
        self._session = onnxruntime.InferenceSession(str(model), options, providers=["CPUExecutionProvider"])
        self._inputs = {i.name for i in self._session.get_inputs()}
        self._tokenizer = Tokenizer.from_file(str(tokenizer))
        self._tokenizer.enable_truncation(max_length=self.max_length)
        self._tokenizer.enable_padding()

    def _embed(self, texts: list[str]) -> np.ndarray:
        self._load()
        # Texts in a batch are padded to the longest one, so batching texts of
        # similar length wastes less work; the vectors are put back in order after.
        # (The 8-bit model scales its numbers per batch, so a text's vector
        # depends very slightly on its batch: cosine similarity above 0.95.)
        order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
        vectors = np.zeros((len(texts), self.dimensions), dtype=np.float32)
        for start in range(0, len(order), self.batch_size):
            batch = order[start:start + self.batch_size]
            encoded = self._tokenizer.encode_batch([texts[i] for i in batch])
            feed = {"input_ids": np.array([e.ids for e in encoded], dtype=np.int64),
                    "attention_mask": np.array([e.attention_mask for e in encoded], dtype=np.int64)}
            if "token_type_ids" in self._inputs:
                feed["token_type_ids"] = np.array([e.type_ids for e in encoded], dtype=np.int64)
            hidden = self._session.run(None, feed)[0]           # (batch, tokens, 384)
            vectors[batch] = hidden[:, 0]                       # bge uses the first ([CLS]) token's vector
        return vectors / np.linalg.norm(vectors, axis=1, keepdims=True).clip(min=1e-12)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(list(texts)).tolist()

    def embed_query(self, text: str) -> list[float]:
        return self._embed([QUERY_INSTRUCTION + text])[0].tolist()


class HashEmbeddings(Embeddings):
    """Words and word pairs hashed into a fixed number of buckets. For tests only."""

    model_id = "hash-256"

    def __init__(self, dimensions: int = 256):
        self.dimensions = dimensions

    def _vector(self, text: str) -> list[float]:
        words = re.findall(r"[a-z0-9]+", text.lower())
        vector = np.zeros(self.dimensions, dtype=np.float32)
        for token in words + [f"{a} {b}" for a, b in zip(words, words[1:])]:
            digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "little") % self.dimensions
            vector[index] += 1.0 if digest[4] % 2 else -1.0
        norm = np.linalg.norm(vector)
        return (vector / norm if norm else vector).tolist()

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


class CachedEmbeddings(Embeddings):
    """Remember every vector on disk, so the corpus is embedded only once.

    New texts are embedded in parts of ``PART`` and each part is saved as soon
    as it is done, so an interrupted build (Ctrl+C) keeps what it finished. When
    embedding is slow (the real model on a laptop), progress is printed to stderr.
    """

    PART = 256
    QUIET_SECONDS = 5.0                                    # no progress lines for jobs faster than this

    def __init__(self, base: Embeddings, path: Path):
        self.base = base
        self.model_id = getattr(base, "model_id", type(base).__name__)
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS vectors (key TEXT PRIMARY KEY, vector BLOB NOT NULL)")
        self.computed = 0                                  # how many texts were embedded (not cached) so far

    @contextmanager
    def _db(self):
        """A connection that commits and is closed afterwards (``with sqlite3.connect()`` only commits)."""
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def _key(self, text: str) -> str:
        return hashlib.sha256(f"{self.model_id}\n{text}".encode("utf-8")).hexdigest()

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        keys = [self._key(t) for t in texts]
        found: dict[str, list[float]] = {}
        with self._db() as db:
            for start in range(0, len(keys), 500):
                part = keys[start:start + 500]
                rows = db.execute(f"SELECT key, vector FROM vectors WHERE key IN ({','.join('?' * len(part))})", part)
                found.update((k, np.frombuffer(v, dtype=np.float32).tolist()) for k, v in rows)
        missing = [(k, t) for k, t in zip(keys, texts) if k not in found]
        started = time.perf_counter()
        for start in range(0, len(missing), self.PART):
            part = missing[start:start + self.PART]
            vectors = self.base.embed_documents([t for _, t in part])
            with self._db() as db:
                db.executemany("INSERT OR REPLACE INTO vectors VALUES (?, ?)",
                               [(k, np.asarray(v, dtype=np.float32).tobytes()) for (k, _), v in zip(part, vectors)])
            found.update((k, list(v)) for (k, _), v in zip(part, vectors))
            self.computed += len(part)
            elapsed = time.perf_counter() - started
            if elapsed > self.QUIET_SECONDS:
                done = start + len(part)
                left = elapsed / done * (len(missing) - done)
                print(f"  embedded {done}/{len(missing)} texts in {elapsed:.0f} s (about {left:.0f} s left)",
                      file=sys.stderr, flush=True)
        return [found[k] for k in keys]

    def embed_query(self, text: str) -> list[float]:
        return self.base.embed_query(text)                 # queries are cheap and rarely repeat


def default_embeddings(cache_path: Path | None = None) -> Embeddings:
    """The embedder to use: the ONNX model, or the hashing stand-in when A11Y_EMBEDDINGS=hash."""
    choice = os.getenv("A11Y_EMBEDDINGS", "onnx").strip().lower()
    base = HashEmbeddings() if choice == "hash" else OnnxEmbeddings()
    return CachedEmbeddings(base, cache_path or settings.data_dir / "index" / "embeddings.sqlite")
