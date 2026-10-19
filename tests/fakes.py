"""A scripted stand-in for the language model, so the harness can be tested
end to end without an API key, a network or any cost.

It reads the broken HTML out of the prompt, applies a simple rule-based fix
(or a deliberately bad one), and answers in the format the method asked for:
a ```html block for zero-shot, the ###albidaya### markers for AccessGuru.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult


def good_fix(html: str) -> str:
    html = re.sub(r"<img(?![^>]*\balt=)", '<img alt="A cat"', html)
    html = html.replace("<button></button>", '<button aria-label="Search"></button>')
    return html


def delete_everything(html: str) -> str:
    return "<p>fixed</p>"


def no_change(html: str) -> str:
    return html


def make_it_worse(html: str) -> str:
    return html + '<a href="/more"></a>'        # adds a link with no name; fixes nothing


def _snippet(text: str) -> str:
    blocks = re.findall(r"```html[ \t]*\n(.*?)```", text, re.S)
    if blocks:
        return blocks[-1].strip()
    m = re.search(r"Affected HTML Element\(s\): ```html(.*?)```", text, re.S)       # re-prompt format
    if m:
        return m.group(1).strip()
    m = re.search(r'Affected HTML Element\(s\): "(.*?)"\s*$', text, re.S | re.M)   # first-pass format
    return m.group(1).strip() if m else ""


class ScriptedModel(BaseChatModel):
    """Fake chat model. ``first`` fixes pass 1 of AccessGuru and zero-shot;
    ``reprompt`` is used once AccessGuru's stricter re-prompt appears."""

    first: Callable[[str], str] = good_fix
    reprompt: Callable[[str], str] | None = None
    reply_without_code: bool = False
    raise_error: Exception | None = None
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted-fake"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs: Any) -> ChatResult:
        self.calls += 1
        if self.raise_error is not None:
            raise self.raise_error
        humans = [m.content for m in messages if m.type == "human"]
        original = _snippet(humans[0])
        is_reprompt = "ABSOLUTE RULES" in humans[0]
        fix = self.reprompt if (is_reprompt and self.reprompt) else self.first
        fixed = fix(original)
        if self.reply_without_code:
            text = "I think the page is fine."
        elif "###albidaya###" in humans[-1]:
            text = f"Confirmed.\n###albidaya###\n{fixed}\n###alnihaya###"
        elif len(humans) > 1 and "###albidaya###" not in humans[-1]:
            text = f"Analysis step.\n```html\n{fixed}\n```"
        else:
            text = f"Here is the fix:\n```html\n{fixed}\n```"
        message = AIMessage(content=text, usage_metadata={"input_tokens": 100, "output_tokens": 50,
                                                          "total_tokens": 150})
        return ChatResult(generations=[ChatGeneration(message=message)])


class RateLimitError(Exception):
    """Mimics a provider 429 error."""

    def __init__(self, message: str):
        super().__init__(message)
        self.status_code = 429
