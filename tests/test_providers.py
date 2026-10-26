"""Model providers, checked offline.

Mistral's API is replaced by a mock HTTP transport, so these tests see the
exact request our code sends (JSON schema, tool choice, parameters) and parse
real-shaped responses and errors, without a key or a network.
"""

import asyncio
import json
import time

import httpx
import pytest

from a11y_agent import model_client
from a11y_agent.config import settings
from a11y_agent.edits import proposal_format, submit_tool_spec
from a11y_agent.llm import get_chat_model, missing_key, parse_model_spec
from a11y_agent.model_client import AuthFailed, ModelClient, QuotaExhausted, RateLimiter, _error_kind

pytest.importorskip("langchain_mistralai")

SPEC = "mistralai:mistral-small-2603"
EDITS = {"explanation": "Added alt text.", "edits": [{"find": "<img>", "replace": '<img alt="Logo">'}]}


class FakeMistral:
    """Answers POST /chat/completions with the queued (status, body) pairs; records each request."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        status, body = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        return httpx.Response(status, json=body)


def completion(content: str = "", tool_calls: list | None = None, finish: str = "stop") -> tuple[int, dict]:
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return 200, {"id": "cmpl-1", "object": "chat.completion", "model": "mistral-small-2603",
                 "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                 "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150}}


def client_for(fake: FakeMistral, monkeypatch) -> ModelClient:
    monkeypatch.setenv("MISTRAL_API_KEY", "test-key-not-real")
    http = httpx.AsyncClient(base_url="https://api.mistral.ai/v1", transport=httpx.MockTransport(fake.handler))
    return ModelClient(SPEC, llm=get_chat_model(SPEC, async_client=http), limiter=RateLimiter())


def test_mistral_structured_request_and_reply(monkeypatch):
    fake = FakeMistral(completion(json.dumps(EDITS)))
    client = client_for(fake, monkeypatch)
    text, record = asyncio.run(client.chat([("system", "s"), ("human", "fix it")], max_tokens=500,
                                           response_format=proposal_format()))
    body = fake.requests[0]
    assert body["model"] == "mistral-small-2603" and body["temperature"] == 0 and body["random_seed"] == 0
    assert body["max_tokens"] == 500 and "reasoning_effort" not in body
    assert body["response_format"]["type"] == "json_schema" and body["response_format"]["json_schema"]["strict"]
    assert "$ref" not in json.dumps(body["response_format"])
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert json.loads(text) == EDITS
    assert (record.input_tokens, record.output_tokens, record.finish_reason) == (120, 30, "stop")


def test_mistral_tool_requests_use_mistral_names(monkeypatch):
    from langchain_core.messages import HumanMessage

    call = {"id": "abcDEF123", "type": "function",
            "function": {"name": "submit_fix", "arguments": json.dumps(EDITS)}}
    fake = FakeMistral(completion(tool_calls=[call], finish="tool_calls"))
    client = client_for(fake, monkeypatch)
    tools = [submit_tool_spec()]
    reply, _ = asyncio.run(client.chat_with_tools([HumanMessage(content="fix it")], tools=tools,
                                                  tool_choice="required", max_tokens=400))
    assert fake.requests[0]["tool_choice"] == "any"                  # Mistral's word for "must call a tool"
    assert fake.requests[0]["tools"][0]["function"]["name"] == "submit_fix"
    assert reply.tool_calls[0]["name"] == "submit_fix" and reply.tool_calls[0]["args"] == EDITS
    asyncio.run(client.chat_with_tools([HumanMessage(content="again")], tools=tools, tool_choice="submit_fix",
                                       max_tokens=400))
    assert fake.requests[1]["tool_choice"] == {"type": "function", "function": {"name": "submit_fix"}}


def test_mistral_errors_become_our_failure_types(monkeypatch):
    monkeypatch.setattr(model_client.asyncio, "sleep", _no_sleep)
    rejected = FakeMistral((401, {"message": "Unauthorized", "request_id": "r1"}))
    with pytest.raises(AuthFailed):
        asyncio.run(client_for(rejected, monkeypatch).chat([("human", "hi")], max_tokens=50))

    busy = FakeMistral((429, {"message": "Requests rate limit exceeded"}), completion("ok"))
    text, _ = asyncio.run(client_for(busy, monkeypatch).chat([("human", "hi")], max_tokens=50))
    assert text == "ok" and len(busy.requests) == 2                  # waited, then retried

    monthly = FakeMistral((429, {"message": "You have exceeded your monthly token quota"}))
    with pytest.raises(QuotaExhausted):
        asyncio.run(client_for(monthly, monkeypatch).chat([("human", "hi")], max_tokens=50))


async def _no_sleep(seconds):
    return None


def test_provider_names_and_keys(monkeypatch):
    assert parse_model_spec("mistral:mistral-small-2603") == ("mistralai", "mistral-small-2603")
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    assert missing_key(SPEC) == "MISTRAL_API_KEY"
    monkeypatch.setenv("MISTRAL_API_KEY", "x")
    assert missing_key(SPEC) is None


def test_limits_are_per_provider(monkeypatch):
    for name in ("RATE_LIMIT_TPM", "GROQ_RATE_LIMIT_TPM", "MISTRAL_RATE_LIMIT_RPM", "MISTRAL_MAX_REQUEST_TOKENS"):
        monkeypatch.delenv(name, raising=False)
    mistral, groq = settings.limits(SPEC), settings.limits("groq:openai/gpt-oss-120b")
    assert groq.max_request_tokens == 8000 and mistral.max_request_tokens > 8000
    assert mistral.min_interval >= 1.0                                 # free tier: 1 request per second
    monkeypatch.setenv("RATE_LIMIT_TPM", "1234")                       # an old .env: applies to Groq only
    monkeypatch.setenv("MISTRAL_RATE_LIMIT_RPM", "0")
    assert settings.limits("groq:openai/gpt-oss-120b").tokens_per_minute == 1234
    assert settings.limits(SPEC).tokens_per_minute == mistral.tokens_per_minute
    assert settings.limits(SPEC).requests_per_minute == 0
    assert ModelClient(SPEC, llm=object()).max_request_tokens == mistral.max_request_tokens


def test_cache_keys_for_groq_are_unchanged():
    """Adding providers must not invalidate replies already cached for gpt-oss."""
    assert ModelClient("groq:openai/gpt-oss-120b", llm=object()).params == \
        {"temperature": 0.0, "reasoning_effort": settings.reasoning_effort}
    assert ModelClient(SPEC, llm=object()).params == {"temperature": 0.0}


def test_minimum_interval_between_requests():
    limiter = RateLimiter(min_interval=0.3)

    async def two():
        start = time.monotonic()
        await limiter.acquire(10)
        await limiter.acquire(10)
        return time.monotonic() - start
    assert asyncio.run(two()) >= 0.29


def test_error_kinds_for_other_wordings():
    class Err(Exception):
        status_code = 422
    assert _error_kind(Err("response_format json_schema: unsupported keyword"))[0] == "format_unsupported"
    assert _error_kind(Err("Prompt contains 300000 tokens, too large for model with 262144 maximum context length"))[0] == "too_large"
