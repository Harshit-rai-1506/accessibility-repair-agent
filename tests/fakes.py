"""A scripted stand-in for the language model, so the harness can be tested
end to end without an API key, a network or any cost.

``ScriptedModel`` (baselines) reads the broken HTML out of the prompt, applies
a simple rule-based fix (or a deliberately bad one), and answers in the format
the method asked for: a ```html block for zero-shot, the ###albidaya###
markers for AccessGuru. ``EditModel`` (the agent) replies with JSON edits.
"""

from __future__ import annotations

import json
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


# ---------------------------------------------------------------------------
# For the agent: replies are JSON edits
# ---------------------------------------------------------------------------

def proposal(*edits: tuple[str, str], explanation: str = "Fixed it.") -> dict:
    """A FixProposal as a dict: proposal(("find", "replace"), ...)."""
    return {"explanation": explanation, "edits": [{"find": f, "replace": r} for f, r in edits]}


def shown_html(prompt: str) -> str:
    """The HTML the agent showed the model (the last ```html block of the prompt)."""
    blocks = re.findall(r"```html[ \t]*\n(.*?)```", prompt, re.S)
    return blocks[-1].strip() if blocks else ""


def rule_in(prompt: str) -> str:
    m = re.search(r'the axe-core rule "([^"]+)" fails', prompt)
    return m.group(1) if m else ""


class EditModel(BaseChatModel):
    """Fake model for the agent.

    ``replies`` are used in order, one per call (the last one repeats). Each is
    a dict (sent as JSON), a string (sent as is), or a function
    ``f(prompt) -> dict | str``. Every prompt is kept in ``prompts``.
    """

    replies: list = []
    prompts: list = []
    calls: int = 0
    finish_reason: str = "stop"
    raise_error: Exception | None = None

    @property
    def _llm_type(self) -> str:
        return "edit-fake"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs: Any) -> ChatResult:
        self.calls += 1
        if self.raise_error is not None:
            raise self.raise_error
        prompt = [m.content for m in messages if m.type == "human"][-1]
        self.prompts.append(prompt)
        reply = self.replies[min(self.calls - 1, len(self.replies) - 1)]
        value = reply(prompt) if callable(reply) else reply
        text = value if isinstance(value, str) else json.dumps(value)
        message = AIMessage(content=text, usage_metadata={"input_tokens": 200, "output_tokens": 40,
                                                          "total_tokens": 240},
                            response_metadata={"finish_reason": self.finish_reason})
        return ChatResult(generations=[ChatGeneration(message=message)])


def page_fixer(prompt: str) -> dict:
    """Knows a correct edit for a few rules, chosen by the rule named in the prompt."""
    rule = rule_in(prompt)
    html = shown_html(prompt)
    if rule == "image-alt":
        return proposal(('<img src="cat.png">', '<img src="cat.png" alt="A cat">'))
    if rule == "button-name":
        return proposal(("<button></button>", '<button aria-label="Search"></button>'))
    if rule == "html-has-lang":
        return proposal(("<html>", '<html lang="en">'))
    return proposal((html[:20], html[:20]))          # changes nothing
