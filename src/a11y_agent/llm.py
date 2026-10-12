"""One place to create the chat model, whichever provider is configured.

The rest of the code asks for "the model" and never imports a provider SDK
directly. Switching from Groq to a local Ollama model, or to a paid API later,
is then a one-line change in .env.

    from a11y_agent.llm import get_chat_model
    llm = get_chat_model()                     # uses MODEL from .env
    llm.invoke("Say hello").content
"""

from __future__ import annotations

import os
import time

from .config import settings

# Environment variable that holds the API key for each provider.
KEY_VARS = {"groq": "GROQ_API_KEY", "openai": "OPENAI_API_KEY", "ollama": None}


def parse_model_spec(spec: str) -> tuple[str, str]:
    """Split "provider:model" -> (provider, model). Only the first colon counts,
    so Ollama tags like "ollama:qwen2.5-coder:3b" work."""
    provider, sep, model = (spec or "").strip().partition(":")
    if not sep or not provider or not model:
        raise ValueError(
            f"model spec {spec!r} must look like 'provider:model', e.g. 'groq:openai/gpt-oss-120b'"
        )
    return provider.lower(), model


def missing_key(spec: str | None = None) -> str | None:
    """Name of the API-key variable the configured provider needs but lacks, if any."""
    provider, _ = parse_model_spec(spec or settings.model)
    var = KEY_VARS.get(provider)
    if var and not os.getenv(var, "").strip():
        return var
    return None


def get_chat_model(spec: str | None = None, *, temperature: float = 0.0, **kwargs):
    """Build a LangChain chat model from a "provider:model" spec."""
    from langchain.chat_models import init_chat_model  # imported lazily: heavy package

    provider, model = parse_model_spec(spec or settings.model)
    options: dict = {"temperature": temperature}
    if provider == "groq":
        # Retries with backoff handle the free tier's per-minute limits (HTTP 429).
        options["max_retries"] = settings.max_retries
        if model.startswith("openai/gpt-oss"):
            options["reasoning_effort"] = settings.reasoning_effort
    options.update(kwargs)
    return init_chat_model(model, model_provider=provider, **options)


def ping(spec: str | None = None) -> tuple[str, float]:
    """Send one tiny request; return (reply, seconds). Used by check_setup.py."""
    llm = get_chat_model(spec)
    start = time.perf_counter()
    reply = llm.invoke("Reply with exactly one word: ready")
    return str(reply.content).strip(), time.perf_counter() - start
