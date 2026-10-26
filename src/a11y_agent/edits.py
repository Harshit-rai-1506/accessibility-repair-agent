"""How the agent changes HTML: small find-and-replace edits, applied by us.

The baselines ask the model to return the *whole* corrected HTML. The agent
asks for **edits** instead: a list of ``{"find": ..., "replace": ...}`` pairs,
where ``find`` is text copied exactly from the HTML. Our code applies them.
Coding agents use the same idea ("search and replace blocks"). Compared with
a full rewrite it

* costs far fewer output tokens (a typical fix changes one attribute),
* cannot quietly drop or rewrite parts of the page the model never meant to
  touch, because everything outside the edits is copied byte for byte,
* lets long pages fit the free tier's request limit, since the reply no
  longer grows with the page,
* gives a readable diff, which Phase 5 shows to a person for approval.

The reply format is a JSON schema generated from the Pydantic models below
(``FixProposal``). The provider constrains the model's output to that schema
("structured output"), and we validate it again with Pydantic, because a
model can still return something unusable (cut off, or empty).

Long pages: if the whole page does not fit the token budget, ``focus_html``
shows the model the parts around the failing elements plus the start and end
of the document, with markers where text was left out. Edits are still
applied to the full page.
"""

from __future__ import annotations

import json
import re

from pydantic import BaseModel, ConfigDict, Field, ValidationError


# ---------------------------------------------------------------------------
# The reply format
# ---------------------------------------------------------------------------

class Edit(BaseModel):
    """One change: replace the exact text ``find`` with ``replace``."""
    model_config = ConfigDict(extra="forbid")

    find: str = Field(description="Text copied exactly from the HTML, long enough to occur only once.")
    replace: str = Field(description="What that text becomes.")


class FixProposal(BaseModel):
    """What the model must return."""
    model_config = ConfigDict(extra="forbid")

    explanation: str = Field(description="One or two sentences: what was wrong and what you changed.")
    edits: list[Edit] = Field(description="The changes, applied in order.")


def proposal_schema() -> dict:
    """FixProposal's JSON schema with nested definitions written out in place.

    Pydantic puts ``Edit`` under ``$defs`` and refers to it with ``$ref``.
    Not every provider follows references in structured-output schemas, so the
    schema sent to models has none.
    """
    from langchain_core.utils.json_schema import dereference_refs

    schema = dereference_refs(FixProposal.model_json_schema())
    schema.pop("$defs", None)
    return schema


def proposal_format() -> dict:
    """OpenAI-style ``response_format`` asking for a ``FixProposal`` (strict schema).

    Groq (gpt-oss) and Mistral both accept this form."""
    return {
        "type": "json_schema",
        "json_schema": {"name": "fix_proposal", "strict": True, "schema": proposal_schema()},
    }


SUBMIT_TOOL = "submit_fix"


def submit_tool_spec() -> dict:
    """The final answer offered as a tool, for the tool-calling agent (Phase 3).

    When the model can also call other tools, the provider cannot enforce a
    JSON schema on the reply at the same time. So the answer itself becomes a
    tool, ``submit_fix``, whose arguments are a FixProposal: the model's
    work ends when it calls it, and we validate the arguments with Pydantic.
    """
    from langchain_core.utils.function_calling import convert_to_openai_tool

    spec = convert_to_openai_tool(FixProposal)
    spec["function"]["name"] = SUBMIT_TOOL
    spec["function"]["description"] = ("Submit your fix: a one or two sentence explanation and the edits. "
                                       "Call this exactly once, when your fix is ready.")
    return spec


def proposal_from_args(args: dict) -> FixProposal:
    """Validate the arguments of a ``submit_fix`` call."""
    try:
        proposal = FixProposal.model_validate(args)
    except ValidationError as e:
        first = e.errors()[0]
        where = ".".join(str(p) for p in first["loc"]) or "the arguments"
        raise ProposalError(f"Your submit_fix call did not match the required format ({where}: {first['msg']}).")
    if not proposal.edits:
        raise ProposalError("Your submit_fix call contained no edits. Give at least one edit that fixes the problem.")
    return proposal


class ProposalError(ValueError):
    """The reply could not be read as a FixProposal. The message is written for the model."""


class EditError(ValueError):
    """One or more edits could not be applied. The message is written for the model."""

    def __init__(self, problems: list[str]):
        super().__init__(" ".join(problems))
        self.problems = problems


def parse_proposal(reply: str) -> FixProposal:
    """Read the model's reply. Tolerates a ```json fence (providers without native schemas)."""
    text = (reply or "").strip()
    fenced = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    if not text.startswith("{") and "{" in text:
        text = text[text.index("{"): text.rindex("}") + 1]
    if not text:
        raise ProposalError("Your reply was empty. Reply with the JSON object described above.")
    try:
        proposal = FixProposal.model_validate(json.loads(text))
    except json.JSONDecodeError:
        raise ProposalError("Your reply was not valid JSON. Reply with only the JSON object described above.")
    except ValidationError as e:
        first = e.errors()[0]
        where = ".".join(str(p) for p in first["loc"]) or "the reply"
        raise ProposalError(f"Your reply did not match the required format ({where}: {first['msg']}).")
    if not proposal.edits:
        raise ProposalError("Your reply contained no edits. Give at least one edit that fixes the problem.")
    return proposal


# ---------------------------------------------------------------------------
# Applying edits
# ---------------------------------------------------------------------------

def _short(text: str, limit: int = 80) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _spans(text: str, find: str) -> list[tuple[int, int]]:
    """Where ``find`` occurs: exactly if possible, otherwise ignoring differences in whitespace."""
    exact, start = [], text.find(find)
    while start >= 0:
        exact.append((start, start + len(find)))
        start = text.find(find, start + 1)
    if exact:
        return exact
    words = find.split()
    if not words:
        return []
    pattern = r"\s+".join(re.escape(w) for w in words)
    return [m.span() for m in re.finditer(pattern, text)]


def apply_edits(html: str, edits: list[Edit]) -> str:
    """Apply edits in order. All or nothing: if any edit fails, nothing is changed.

    Each ``find`` must match exactly one place (whitespace differences are
    tolerated, because models often re-indent copied text). A ``find`` that
    matches nowhere, or in several places, is reported back to the model.
    """
    result, problems = html, []
    for number, edit in enumerate(edits, start=1):
        if not edit.find.strip():
            problems.append(f"Edit {number}: the find text is empty.")
            continue
        spans = _spans(result, edit.find)
        if not spans:
            problems.append(f'Edit {number}: the find text was not found (it begins "{_short(edit.find)}"). '
                            "Copy it exactly from the HTML shown.")
        elif len(spans) > 1:
            problems.append(f'Edit {number}: the find text occurs {len(spans)} times ("{_short(edit.find)}"). '
                            "Include more of the surrounding HTML so it matches only one place.")
        else:
            start, end = spans[0]
            result = result[:start] + edit.replace + result[end:]
    if problems:
        raise EditError(problems)
    return result


def describe_edits(edits: list[dict] | list[Edit], limit: int = 120) -> str:
    """One line per edit, shortened, for prompts, traces and reports."""
    lines = []
    for e in edits:
        find, replace = (e["find"], e["replace"]) if isinstance(e, dict) else (e.find, e.replace)
        lines.append(f'"{_short(find, limit)}" -> "{_short(replace, limit)}"')
    return "; ".join(lines)


# ---------------------------------------------------------------------------
# Showing long pages in parts
# ---------------------------------------------------------------------------

def omitted_marker(n: int) -> str:
    return f"<!-- … {n} characters not shown … -->"


def locate(html: str, element_html: str) -> int | None:
    """Best guess of where an element reported by axe starts in the source text.

    axe reports each element's HTML as the browser serialised it (at most 300
    characters, else only the opening tag), which may differ from the source
    in quoting or spacing. Try the whole element, then the opening tag, then
    the most distinctive attribute value.
    """
    if not element_html:
        return None
    for probe in (element_html, element_html.split(">", 1)[0] + ">"):
        i = html.find(probe)
        if i >= 0:
            return i
    opening = element_html.split(">", 1)[0]
    values = re.findall(r'="([^"]{4,})"', opening) + re.findall(r"='([^']{4,})'", opening)
    for value in sorted(values, key=len, reverse=True):
        i = html.find(value)
        if i >= 0:
            tag_start = html.rfind("<", 0, i)
            return tag_start if tag_start >= 0 else i
    return None


def _snap(html: str, start: int, end: int) -> tuple[int, int]:
    """Move window edges so they do not cut through a tag."""
    lt = html.rfind("<", 0, start + 1)
    if lt >= 0 and start - lt < 300 and html.rfind(">", lt, start) < 0:
        start = lt
    gt = html.find(">", max(end - 1, 0))
    if gt >= 0 and gt - end < 300 and html.find("<", end, gt) < 0:
        end = gt + 1
    return max(start, 0), min(end, len(html))


def focus_html(html: str, elements: list[str], max_chars: int) -> tuple[str, bool]:
    """The HTML itself if it fits in ``max_chars``; otherwise an excerpt.

    The excerpt keeps the start and the end of the document (its structure)
    and a window around each failing element, joined by "not shown" markers.
    Returns (text, excerpted).
    """
    if len(html) <= max_chars:
        return html, False
    head = int(min(1500, max_chars * 0.15))
    tail = int(min(600, max_chars * 0.05))
    positions = sorted({p for p in (locate(html, e) for e in elements) if p is not None})
    windows = [(0, head), (len(html) - tail, len(html))]
    if positions:
        per = min(4000, (max_chars - head - tail) // len(positions))
        windows += [(p - int(per * 0.3), p + int(per * 0.7)) for p in positions]
    else:
        windows[0] = (0, max_chars - tail)

    merged: list[list[int]] = []
    for start, end in sorted(_snap(html, max(s, 0), min(e, len(html))) for s, e in windows):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    parts, cursor = [], 0
    for start, end in merged:
        if start > cursor:
            parts.append(omitted_marker(start - cursor))
        parts.append(html[start:end])
        cursor = end
    if cursor < len(html):
        parts.append(omitted_marker(len(html) - cursor))
    return "\n".join(parts), True
