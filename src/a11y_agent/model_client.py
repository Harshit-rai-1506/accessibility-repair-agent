"""Calling the model for evaluations: cached, rate-limited, and honest about limits.

Evaluation runs make hundreds of model calls on a free tier with tight
limits (for gpt-oss-120b on Groq: 30 requests and 8,000 tokens per minute,
200,000 tokens per day). ``ModelClient`` wraps the LangChain chat model with
three things a plain ``llm.invoke()`` does not give you:

* **A response cache on disk.** Each request (model, settings, messages) is
  hashed; the reply is stored in an SQLite file. Running the same evaluation
  again costs nothing and returns identical answers, so you can change the
  scoring or the report without spending quota. (LangChain has its own cache
  hook, ``set_llm_cache``, but it does not tell you whether a call was a hit,
  which we need to report real token use.)

* **A rate limiter.** Before each call it waits until the last minute's token
  and request counts leave room, so we rarely hit the per-minute limit at all.

* **Clear failure types.** A request too big for the per-minute limit raises
  ``RequestTooLarge`` (the row is scored as not fixed and counted). Running out
  of the daily quota raises ``QuotaExhausted`` and a rejected key raises
  ``AuthFailed``; both stop the run cleanly. Finished rows are kept, so running
  the same command later continues where it stopped. A reply the provider
  itself rejects as not matching the requested JSON schema raises
  ``BadReply``, which the agent treats as one failed attempt.

**Structured output.** ``chat(..., response_format=schema)`` asks the provider
to constrain the reply to a JSON schema (Groq and OpenAI: ``response_format``;
Ollama: ``format``). The reply is still returned as text, so it is cached like
any other; the caller validates it (the agent uses Pydantic).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from .config import settings
from .llm import get_chat_model

Messages = list[tuple[str, str]]   # [("system" | "human" | "ai", text), ...]


class RequestTooLarge(Exception):
    """The request alone exceeds the per-minute token limit, so it can never succeed."""


class StopRun(Exception):
    """A problem that affects every remaining item, so the run should stop."""


class QuotaExhausted(StopRun):
    """The daily quota is used up. Stop and resume later."""


class AuthFailed(StopRun):
    """The API key is missing, wrong or revoked."""


class BadReply(Exception):
    """The provider rejected the model's own output (e.g. JSON that does not match the schema)."""


def estimate_tokens(text: str) -> int:
    """Rough token count: about 4 characters per token for English and HTML."""
    return len(text) // 4 + 1


def messages_tokens(messages: Messages) -> int:
    return sum(estimate_tokens(text) + 4 for _, text in messages)


@dataclass
class CallRecord:
    """What one model call cost."""
    input_tokens: int
    output_tokens: int
    seconds: float
    cached: bool
    finish_reason: str = ""


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

class ResponseCache:
    """Model replies stored in one SQLite file, keyed by a hash of the request."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS replies (key TEXT PRIMARY KEY, value TEXT NOT NULL, created REAL)")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=30)

    @staticmethod
    def key(model: str, params: dict, messages: Messages) -> str:
        payload = json.dumps({"model": model, "params": params, "messages": messages},
                             sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def get(self, key: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT value FROM replies WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key: str, value: dict) -> None:
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO replies VALUES (?, ?, ?)",
                       (key, json.dumps(value, ensure_ascii=False), time.time()))

    def __len__(self) -> int:
        with self._connect() as db:
            return db.execute("SELECT COUNT(*) FROM replies").fetchone()[0]


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

class RateLimiter:
    """Keeps tokens and requests within per-minute limits (0 = no limit).

    It remembers what was spent in the last 60 seconds. ``acquire`` waits
    until the planned request fits; ``settle`` replaces the estimate with the
    real token count once the reply arrives.
    """

    def __init__(self, tokens_per_minute: int = 0, requests_per_minute: int = 0, window: float = 60.0):
        self.tpm = tokens_per_minute
        self.rpm = requests_per_minute
        self.window = window
        self._events: deque[list] = deque()    # [timestamp, tokens]
        self._lock = asyncio.Lock()

    def _expire(self, now: float) -> None:
        while self._events and now - self._events[0][0] >= self.window:
            self._events.popleft()

    async def acquire(self, tokens: int) -> list:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._expire(now)
                used = sum(e[1] for e in self._events)
                tokens_ok = not self.tpm or used + tokens <= self.tpm or not self._events
                requests_ok = not self.rpm or len(self._events) < self.rpm
                if tokens_ok and requests_ok:
                    event = [now, tokens]
                    self._events.append(event)
                    return event
                await asyncio.sleep(max(0.05, self.window - (now - self._events[0][0])))

    def settle(self, event: list, actual_tokens: int) -> None:
        event[1] = actual_tokens


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

_WAIT = re.compile(r"try again in (?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)s)?", re.I)


def _error_kind(error: Exception) -> tuple[str, float | None]:
    """Classify a provider error: 'auth', 'too_large', 'bad_output', 'format_unsupported',
    'daily', 'minute' or 'other'."""
    status = getattr(error, "status_code", None) or getattr(getattr(error, "response", None), "status_code", None)
    text = str(error).lower()
    wait = None
    m = _WAIT.search(text)
    if m and any(m.groups()):
        h, mi, s = (float(x) if x else 0.0 for x in m.groups())
        wait = h * 3600 + mi * 60 + s
    if status in (401, 403) or "invalid api key" in text or "invalid_api_key" in text:
        return "auth", wait
    if (status == 413 or "request too large" in text or "context_length" in text
            or "maximum context" in text or "reduce the length" in text):
        return "too_large", wait
    if "json_validate_failed" in text or "does not match the expected schema" in text:
        return "bad_output", wait
    if status == 400 and ("response_format" in text or "json_schema" in text):
        return "format_unsupported", wait
    if status == 429 or "rate limit" in text or "rate_limit" in text:
        if "per day" in text or "(tpd)" in text or "(rpd)" in text:
            return "daily", wait
        return "minute", wait
    return "other", wait


def _reply_text(message) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    return "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)


class ModelClient:
    """Cached, rate-limited access to one chat model."""

    def __init__(self, spec: str | None = None, *, llm=None, cache: ResponseCache | None = None,
                 limiter: RateLimiter | None = None, max_request_tokens: int | None = None,
                 minute_retries: int = 3):
        self.spec = spec or settings.model
        self._llm = llm
        self.cache = cache
        self.limiter = limiter or RateLimiter(settings.rate_limit_tpm, settings.rate_limit_rpm)
        self.max_request_tokens = settings.max_request_tokens if max_request_tokens is None else max_request_tokens
        self.minute_retries = minute_retries
        self.params = {"temperature": 0.0, "reasoning_effort": settings.reasoning_effort}
        self.native_schema = True       # set to False if the provider rejects response_format

    @property
    def llm(self):
        if self._llm is None:
            self._llm = get_chat_model(self.spec)
        return self._llm

    def output_budget(self, html: str) -> int:
        """max_tokens for a reply that must contain a corrected copy of ``html``,
        plus room for the model's reasoning."""
        return min(settings.max_output_tokens, 2 * estimate_tokens(html) + 1024)

    def _structured_kwargs(self, response_format: dict) -> dict:
        """How each provider is asked for JSON that matches a schema."""
        provider = self.spec.split(":", 1)[0].lower()
        if provider == "ollama":
            return {"format": response_format["json_schema"]["schema"]}
        return {"response_format": response_format}       # Groq, OpenAI (and the test fakes ignore it)

    async def chat(self, messages: Messages, *, max_tokens: int,
                   response_format: dict | None = None) -> tuple[str, CallRecord]:
        """Send one request; returns (reply text, what it cost).

        ``response_format`` is an OpenAI-style ``{"type": "json_schema", ...}``
        dict; when given, the provider constrains the reply to that schema.
        """
        params = {**self.params, "max_tokens": max_tokens}
        if response_format is not None:          # only then, so older cache entries stay valid
            params["response_format"] = response_format
        key = ResponseCache.key(self.spec, params, messages)
        if self.cache is not None and (hit := self.cache.get(key)) is not None:
            return hit["text"], CallRecord(hit["input_tokens"], hit["output_tokens"], 0.0, True,
                                           hit.get("finish_reason", ""))

        planned = messages_tokens(messages) + max_tokens
        if self.max_request_tokens and planned > self.max_request_tokens:
            raise RequestTooLarge(f"needs about {planned} tokens; the limit per request is {self.max_request_tokens}")

        lc_messages = [{"system": SystemMessage, "human": HumanMessage, "ai": AIMessage}[role](content=text)
                       for role, text in messages]
        use_schema = response_format is not None and self.native_schema
        extra = self._structured_kwargs(response_format) if use_schema else {}
        expected = messages_tokens(messages) + min(max_tokens, 1500)
        attempt = 0
        while True:
            event = await self.limiter.acquire(expected)
            start = time.perf_counter()
            try:
                reply = await self.llm.ainvoke(lc_messages, max_tokens=max_tokens, **extra)
                break
            except Exception as error:
                self.limiter.settle(event, expected)
                kind, wait = _error_kind(error)
                if kind == "format_unsupported" and extra:
                    # This model or provider cannot constrain output to a schema. The prompt
                    # already describes the JSON, so ask again without the option, and remember.
                    self.native_schema, extra = False, {}
                    continue
                if kind == "auth":
                    raise AuthFailed(f"the provider rejected the API key ({str(error).splitlines()[0][:150]}); "
                                     "check GROQ_API_KEY in .env") from error
                if kind == "too_large":
                    raise RequestTooLarge(str(error).splitlines()[0][:300]) from error
                if kind == "bad_output":
                    raise BadReply(str(error).splitlines()[0][:300]) from error
                if kind == "daily":
                    hint = f" (the provider suggests trying again in {wait / 60:.0f} minutes)" if wait else ""
                    raise QuotaExhausted(f"daily limit reached{hint}: {str(error).splitlines()[0][:200]}") from error
                if kind == "minute" and attempt < self.minute_retries:
                    attempt += 1
                    await asyncio.sleep(min(wait or 20.0 * attempt, 90.0))
                    continue
                raise
        seconds = time.perf_counter() - start

        usage = getattr(reply, "usage_metadata", None) or {}
        input_tokens = usage.get("input_tokens") or messages_tokens(messages)
        text = _reply_text(reply)
        output_tokens = usage.get("output_tokens") or estimate_tokens(text)
        self.limiter.settle(event, input_tokens + output_tokens)
        finish = (getattr(reply, "response_metadata", None) or {}).get("finish_reason", "") or ""

        if self.cache is not None:
            self.cache.put(key, {"text": text, "input_tokens": input_tokens, "output_tokens": output_tokens,
                                 "finish_reason": finish, "seconds": round(seconds, 3)})
        return text, CallRecord(input_tokens, output_tokens, seconds, False, finish)
