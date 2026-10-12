"""Model configuration. No network calls: we only build the client objects."""

import pytest

from a11y_agent.llm import missing_key, parse_model_spec


def test_parse_model_spec():
    assert parse_model_spec("groq:openai/gpt-oss-120b") == ("groq", "openai/gpt-oss-120b")
    # only the first colon separates provider and model
    assert parse_model_spec("ollama:qwen2.5-coder:3b") == ("ollama", "qwen2.5-coder:3b")


@pytest.mark.parametrize("bad", ["", "gpt-oss-120b", "groq:", ":model"])
def test_parse_model_spec_rejects_bad_input(bad):
    with pytest.raises(ValueError):
        parse_model_spec(bad)


def test_missing_key(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    assert missing_key("groq:openai/gpt-oss-120b") == "GROQ_API_KEY"
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    assert missing_key("groq:openai/gpt-oss-120b") is None
    assert missing_key("ollama:qwen2.5-coder:3b") is None  # local models need no key


def test_groq_client_is_configured_for_free_tier(monkeypatch):
    pytest.importorskip("langchain_groq")
    from a11y_agent.llm import get_chat_model

    monkeypatch.setenv("GROQ_API_KEY", "test-key")  # never sent: no request is made
    llm = get_chat_model("groq:openai/gpt-oss-120b")
    assert type(llm).__name__ == "ChatGroq"
    assert llm.model_name == "openai/gpt-oss-120b"
    # ChatGroq quietly turns 0 into 1e-8 (Groq treats an exact 0 specially),
    # so we check "effectively zero" rather than equality.
    assert llm.temperature < 1e-6
    assert llm.reasoning_effort == "low"
    assert llm.max_retries == 5
