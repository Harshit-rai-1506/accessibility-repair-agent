"""Prompts for the baseline methods, and pulling the fixed HTML out of a reply.

Two prompting methods are defined here:

``zero_shot``
    Our minimal baseline: one message giving the rule's name and axe-core's
    own help text, asking for the corrected HTML in a code block. This is what
    a developer would naturally type into a chat assistant. (Note: the paper's
    "zero-shot" baseline is different and weaker; it only asks "Is the
    following HTML code accessible?" without naming the violation.)

``accessguru``
    The AccessGuru method (Fathallah, Hernández and Staab, ASSETS 2025): an
    expert persona, then a four-step "metacognitive" conversation (understand,
    correct, critique, confirm). If the result still fails, the conversation is
    repeated once with a stricter first step ("corrective re-prompting").

The AccessGuru prompt texts below are copied from the authors' code
(https://github.com/NadeenAhmad/AccessGuruLLM, MIT licence, file
code/AccessGuru/AccessGuruCorrect/syntax_layout_metacognitive_reprompting_gpt4.py),
so that our numbers measure their method and not our paraphrase of it. Their
fifth step (asking the model for a confidence score) is left out: it does not
change the corrected code, and it costs a full extra call.
"""

from __future__ import annotations

import csv
import json
import re
from functools import lru_cache

from .config import settings

# ---------------------------------------------------------------------------
# Rule text from axe-core itself (same version as the scanner)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _axe_messages() -> dict:
    path = settings.corpus_dir / "axe-rules" / "locales" / "_template.json"
    if not path.exists():
        raise FileNotFoundError(f"{path} not found. Run: python scripts/fetch_resources.py --only axe-rules")
    return json.loads(path.read_text(encoding="utf-8")).get("rules", {})


def axe_rule_text(rule_id: str) -> tuple[str, str]:
    """(help, description) for an axe rule, e.g. ("Images must have alternative text", "Ensure <img> ...")."""
    entry = _axe_messages().get(rule_id, {})
    return entry.get("help", rule_id), entry.get("description", "")


# ---------------------------------------------------------------------------
# Method A: zero-shot
# ---------------------------------------------------------------------------

ZERO_SHOT_SYSTEM = "You fix web accessibility problems in HTML."

ZERO_SHOT_USER = """The HTML below fails the axe-core accessibility rule "{rule_id}": {help}.
Rule description: {description}

Return a corrected version of the complete HTML that fixes this violation.
Keep all text and all elements; change only what is needed to fix it.
Reply with the corrected HTML in a single ```html code block.

```html
{html}
```"""


def zero_shot_messages(rule_id: str, html: str) -> list[tuple[str, str]]:
    help_text, description = axe_rule_text(rule_id)
    return [
        ("system", ZERO_SHOT_SYSTEM),
        ("human", ZERO_SHOT_USER.format(rule_id=rule_id, help=help_text,
                                        description=description or help_text, html=html)),
    ]


# ---------------------------------------------------------------------------
# Method B: AccessGuru (prompt texts from the authors' code, see module docstring)
# ---------------------------------------------------------------------------

ACCESSGURU_SYSTEM = """You are a Web accessibility expert with a strong proficiency in HTML and a deep commitment to fixing Web accessibility violations.
You are familiar with different types of disabilities and user needs.
You are familiar with assistive technologies such as screen readers and understand how they interact with the Web.
You specialize in analyzing Web pages, identifying issues, and providing immediate, corrected HTML code solutions that meet WCAG 2.1 standards.
Your expertise includes resolving problems like missing or improper Alt text, insufficient heading structure, non-semantic elements, inaccessible forms, and color contrast issues.
You are adept at transforming flawed code into compliant, clean HTML that works seamlessly with assistive technologies, ensuring that Websites are fully navigable by keyboard and readable by screen readers.
You provide the corrected code necessary for immediate implementation, ensuring that Websites are not only compliant but truly inclusive for users with disabilities.
Your mission is to ensure that every Website and Web application is accessible to all users by providing expertly corrected HTML, making the Web a more inclusive space.
You reflect on your own answers, assess their accuracy, and provide corrections only when necessary."""

CATEGORY_DESCRIPTIONS = {
    "Syntax": """These occur when HTML code lacks essential structural elements or attributes required for accessibility,
        such as missing alt attributes or improper use of heading tags and table elements.
        To correct this type of web accessibility violation, expertise in HTML coding is required.""",
    "Layout": """These relate to design and visual organization issues, such as insufficient color contrast or poor font sizing,
        that reduce readability or usability for visually impaired users.
        Fixes must ensure accessibility without negatively affecting users without impairments or distort the page layout for users without impairments.
        To correct this type of web accessibility violation, knowledge of web design principles and front-end coding is required.""",
    "Semantic": """These involve the misuse or absence of meaningful content or attributes,
        such as vague alt text or improper use of semantic elements like <header> or <section>.
    To correct this type of web accessibility violation, expertise in HTML semantics and coding is required.""",
}

# Step 1 of the first pass
ACCESSGURU_STEP1 = """
    Clarify your understanding of the following web accessibility violation:
    Violation Category: "{category}": {category_description},
    Violation type: "{rule_id}",
    Violation description: "{description}",
    Impact: "{impact}" (Impact is a rating determined by the severity of the violation, indicating the extent to which it hinders user interaction with the Web content. The scale is [cosmetic, minor, moderate, serious, critical]).
    Web page URL: "{url}"
    Affected HTML Element(s): "{html}"
    """

# Step 1 of the corrective re-prompt (second pass)
ACCESSGURU_STEP1_REPROMPT = """
    You are analyzing a web accessibility issue using a snippet of Affected HTML Element(s) and related metadata. Follow these strict rules.

    ### Your Inputs:
    - Violation Category: "{category}": {category_description}
    - Violation Type: "{rule_id}"
    - Violation Description: "{description}"
    - Impact: "{impact}" (Severity: cosmetic, minor, moderate, serious, critical)
    - Web Page URL: "{url}"
    - Affected HTML Element(s): ```html{html}```

    ################  ABSOLUTE RULES  ################
    IGNORE these rules → your answer will be rejected.
    1. **HTML-First Analysis**:
    - Base your analysis strictly on the HTML snippet and metadata provided.
    - Do **not** assume or hallucinate additional structure, styles, or UI elements not present in the HTML.

    2. **No Translation / No Hallucination**:
      - Do **NOT** translate, re‑write, or invent content, CSS, forms, headers, sections, scripts, or attributes that are **not** present in HTML.
      - Modify *only* the minimal code required to fix the violation.

    3. **Minimal Patch**:
      - Return **only** the lines you changed **inside a fenced block**.
      - Leave every other part of the HTML untouched.

    4. **Evidence Driven**:
     - Every accessibility concern must be directly justified by something observable in the HTML.

    ################  TASKS  ################
    2. **Identify UI Element** – name the element that has (or might have) the violation.
    3. **Explain Violation** – why it’s a problem, citing evidence from the HTML.
    4. **Provide Minimal Fix** – only the changed lines, in a fenced code block.
    """

ACCESSGURU_STEP2 = """
    Based on your understanding, provide a preliminary correction for the web accessibility violation based on the following WCAG guideline(s): "{guideline}".
    Make sure your generated code corrects the web accessibility violation without introducing new violations.
    Ensure you generate the complete corrected code, not just a snippet.
    """

ACCESSGURU_STEP3 = """
    Critically assess your preliminary correction, make sure to correct the initial web accessibility violation without introducing new web accessibility violations.
    Only make corrections if the previous answer is incorrect.
    Make sure your generated code corrects the web accessibility violation without introducing new violations.
    """

ACCESSGURU_STEP4 = """
    Confirm your final decision on whether the correction is accurate or not and provide the reasoning for your decision.
    Only suggest further corrections if the initial response contains errors.
    Make sure your generated code corrects the web accessibility violation without introducing new violations.
    Enclose your corrected HTML code to replace the initial code with violations between these two marker strings: "###albidaya###" as first line and "###alnihaya###" as last line.
    """

ACCESSGURU_MARKERS = ("###albidaya###", "###alnihaya###")


@lru_cache(maxsize=1)
def _accessguru_guidelines() -> tuple[dict, dict]:
    support = settings.benchmark_dir / "accessguru" / "data" / "prompts_support"
    mapping = json.loads((support / "mapping_dict_file.json").read_text(encoding="utf-8"))
    with open(support / "WCAGGuidelines.csv", encoding="utf-8", newline="") as f:
        texts = {row["guideId"]: row["description"] for row in csv.DictReader(f)}
    return mapping, texts


def accessguru_guidelines(rule_id: str) -> list[str]:
    """WCAG guideline text for a rule, as AccessGuru's get_guidelines() builds it:
    "1.4.6 Contrast (Enhanced):<description>" for each mapped criterion."""
    mapping, texts = _accessguru_guidelines()
    return [f"{name}:{texts.get(name, '')}" for name in mapping.get(rule_id, [])]


def accessguru_step1(item, reprompt: bool = False) -> str:
    template = ACCESSGURU_STEP1_REPROMPT if reprompt else ACCESSGURU_STEP1
    return template.format(
        category=item.category,
        category_description=CATEGORY_DESCRIPTIONS.get(item.category, "No description available for this category."),
        rule_id=item.rule_id,
        description=item.description,
        impact=item.impact.capitalize(),
        url=item.url,
        html=item.html,
    )


def accessguru_step2(rule_id: str) -> str:
    # The authors pass a Python list here, so the model sees "['1.4.6 ...']".
    return ACCESSGURU_STEP2.format(guideline=accessguru_guidelines(rule_id))


# ---------------------------------------------------------------------------
# Getting the HTML back out of a reply
# ---------------------------------------------------------------------------

_FENCE = re.compile(r"```[a-zA-Z0-9_-]*[ \t]*\n(.*?)```", re.S)


def _strip_fences(text: str) -> str:
    match = _FENCE.search(text)
    return match.group(1).strip() if match else text.strip().strip("`").strip()


def extract_html(reply: str, markers: tuple[str, str] | None = None) -> str | None:
    """Find the corrected HTML in a model reply.

    1. If ``markers`` are given and present, take the text between the last
       pair of them (removing a code fence inside, if any).
    2. Otherwise take the last fenced code block.
    3. Otherwise give up (``None``): the reply is scored as "no fix".

    This is deterministic on purpose. The AccessGuru authors used a second
    model plus manual review to extract code; a fixed rule is reproducible
    and cannot quietly repair a bad answer.
    """
    if not reply:
        return None
    if markers:
        start, end = markers
        s = reply.rfind(start)
        e = reply.find(end, s + len(start)) if s >= 0 else -1
        if s >= 0 and e > s:
            inner = _strip_fences(reply[s + len(start):e])
            if inner:
                return inner
    blocks = [b.strip() for b in _FENCE.findall(reply) if b.strip()]
    if blocks:
        return blocks[-1]
    return None
