"""Guardrails (Phase 5): checks that do not trust the model.

The agent already re-scans every fix, but the scanner answers one question
only: is the violation gone? Reading every accepted fix from the Phase 4 runs
showed fixes that pass that question and are still wrong: a page ending in a
literal ``<</end>>``, a heading the model made up, text hidden from sighted
users. Guardrails catch what the scanner cannot, in three layers that are each
simple enough to audit:

1. **Input.** Before any of a page reaches the model, the text a model would
   read (text, comments, alt text and other attributes) is screened for
   instructions aimed at the model ("prompt injection"):
   - rules: a short list of known phrasings (regular expressions);
   - Llama Prompt Guard 2 (86M), a classifier trained on injections and
     jailbreaks, served free on Groq (used when GROQ_API_KEY is set).
2. **Output.** Every proposed fix is checked before it is scanned. A fix that
   removes or hides content, adds scripts or URLs, or breaks the markup is
   rejected, and the reason is fed back for the next attempt.
3. **Action.** A person approves the final diff (the graph's ``approve``
   step). What a scanner cannot judge, such as a heading the model wrote or
   alt text for an image it never saw, is flagged for that person.

All checks compare the page *before* and *after* a change, so a page that
already contains a script or hidden text is never rejected for it.

    python -m a11y_agent.guardrails screen page.html               # input screen for one file
    python -m a11y_agent.guardrails check before.html after.html   # output checks and flags for one fix
    python -m a11y_agent.guardrails eval --save                    # screen the injection examples and dev pages
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from pathlib import Path

from bs4 import BeautifulSoup, Comment
from bs4.element import NavigableString, PreformattedString, Tag

from .config import ROOT, settings

RESULTS_DIR = ROOT / "results" / "guardrails"
EXAMPLES_DIR = ROOT / "examples" / "injection"

# ---------------------------------------------------------------------------
# Reading a page the way the checks need it
# ---------------------------------------------------------------------------

NOT_RENDERED = {"script", "style", "template", "noscript"}
TEXT_ATTRIBUTES = ("alt", "title", "aria-label", "aria-description", "aria-roledescription", "placeholder", "label")
NAME_ATTRIBUTES = ("alt", "aria-label", "aria-description", "title")      # accessible names and descriptions

# Hidden from everyone (and so from screen readers too).
HIDING_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*(hidden|collapse)|"
                          r"opacity\s*:\s*0(\.0*)?\s*(;|$|!)|font-size\s*:\s*0(\.0*)?(px|em|rem|pt|%)?\s*(;|$|!)", re.I)
# Hidden from sighted users only: the usual "screen-reader only" techniques.
SCREEN_READER_ONLY = re.compile(r"(^|\s)(sr-only|visually-hidden|visuallyhidden|screen-reader-text|screen-reader-only|"
                                r"sr_only|a11y-hidden|offscreen|hidden-visually|u-visually-hidden)(\s|$)", re.I)
OFFSCREEN_STYLE = re.compile(r"(left|top|text-indent)\s*:\s*-\d{3,}(px|em|rem)|clip\s*:\s*rect\(\s*0|"
                             r"clip-path\s*:\s*inset\(\s*50%", re.I)
WORD = re.compile(r"\w+")


def _words(text: str) -> list[str]:
    return WORD.findall(text.lower())


def _classes(tag: Tag) -> str:
    value = tag.get("class")
    return " ".join(value) if isinstance(value, list) else str(value or "")


def _is_text(node) -> bool:
    """A text node of the document (not a comment, doctype or other markup)."""
    return isinstance(node, NavigableString) and not isinstance(node, PreformattedString)


@dataclass
class PageView:
    """The parts of a page the checks compare."""
    words: Counter          # every word of the page's text (not scripts or styles)
    visible: Counter        # words a sighted user sees
    visible_texts: Counter  # the same, as whole pieces of text ("Page Content")
    spoken: Counter         # words assistive technology reads out in the page (the <title> is checked as text)
    attribute_words: Counter
    names: Counter          # (attribute, value) of alt, aria-label, ...: text only screen readers get
    controls: Counter       # images, links, buttons, inputs ... by element name
    hidden_controls: Counter   # of those, hidden (from anyone) or stripped of their role
    decorative: list[tuple[str, bool]]   # (image src, the only content of a link or button)
    active: Counter         # scripts, embedded pages, event handlers, javascript: URLs
    urls: set[str]
    colours: Counter
    langs: list[str]
    landmarks: Counter
    stray_brackets: int     # "<" or ">" in text, e.g. a leftover "<</end>>"
    unbalanced: Counter     # elements opened and not closed (or the reverse)
    document_tags: Counter  # html, head, body, title, doctype
    outside: tuple[str, str]   # content before <html> and after </html>


class _Markup(HTMLParser):
    """Counts start and end tags, document-level tags, and angle brackets left in text."""

    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track",
            "wbr", "keygen", "command"}
    OPTIONAL_END = {"p", "li", "dt", "dd", "tr", "td", "th", "thead", "tbody", "tfoot", "colgroup", "caption",
                    "option", "optgroup", "rb", "rt", "rtc", "rp", "html", "head", "body"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.starts, self.ends, self.document = Counter(), Counter(), Counter()
        self.brackets = 0

    def handle_starttag(self, tag, attrs):
        self.starts[tag] += 1
        if tag in ("html", "head", "body", "title"):
            self.document[tag] += 1

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.ends[tag] += 1

    def handle_endtag(self, tag):
        self.ends[tag] += 1

    def handle_decl(self, decl):
        if decl.lower().startswith("doctype"):
            self.document["doctype"] += 1

    def handle_data(self, data):
        if self.cdata_elem is None:                  # not inside <script> or <style>
            self.brackets += data.count("<") + data.count(">")

    def unbalanced(self) -> Counter:
        names = (set(self.starts) | set(self.ends)) - self.VOID - self.OPTIONAL_END
        return Counter({n: abs(self.starts[n] - self.ends[n]) for n in names if self.starts[n] != self.ends[n]})


_DOC_START = re.compile(r"<html[\s>/]", re.I)
_DOC_END = re.compile(r"</html\s*>", re.I)
_NOT_CONTENT = re.compile(r"<!--.*?-->|<!doctype[^>]*>|<\?xml[^>]*\?>|\s+", re.I | re.S)


def _outside(html: str) -> tuple[str, str]:
    """Content before the first <html> tag and after the last </html> (comments and doctype aside)."""
    start = _DOC_START.search(html)
    before = _NOT_CONTENT.sub("", html[:start.start()]) if start else ""
    ends = list(_DOC_END.finditer(html))
    after = _NOT_CONTENT.sub("", html[ends[-1].end():]) if ends else ""
    return before, after


URL_ATTRIBUTES = ("src", "href", "srcset", "action", "formaction", "poster", "data", "background", "cite",
                  "longdesc", "manifest", "ping", "xlink:href", "codebase")
CSS_URL = re.compile(r"url\(\s*['\"]?([^'\")\s]+)", re.I)
COLOUR = re.compile(r"#[0-9a-f]{3,8}\b|rgba?\([^)]*\)|hsla?\([^)]*\)", re.I)
ACTIVE_TAGS = {"script", "iframe", "frame", "frameset", "object", "embed", "applet", "base", "portal"}
CONTROLS_AND_MEDIA = {"img", "svg", "video", "audio", "canvas", "iframe", "object", "embed", "input", "select",
                      "textarea", "button", "a", "area", "picture"}
LANDMARKS = {"main", "header", "footer", "nav", "aside", "section", "form", "search"}


def read_page(html: str) -> PageView:
    soup = BeautifulSoup(html or "", "html.parser")
    state: dict[int, tuple[bool, bool, bool]] = {}      # element -> (not rendered, visually hidden, hidden from AT)

    def flags(tag: Tag) -> tuple[bool, bool, bool]:
        chain, node = [], tag
        while isinstance(node, Tag) and id(node) not in state:
            chain.append(node)
            node = node.parent
        current = state.get(id(node), (False, False, False)) if isinstance(node, Tag) else (False, False, False)
        for t in reversed(chain):
            style = str(t.get("style") or "")
            gone = t.has_attr("hidden") or bool(HIDING_STYLE.search(style)) or \
                (t.name == "input" and str(t.get("type", "")).lower() == "hidden")
            at_hidden = gone or str(t.get("aria-hidden", "")).strip().lower() == "true"
            visual_hidden = gone or bool(SCREEN_READER_ONLY.search(_classes(t))) or bool(OFFSCREEN_STYLE.search(style))
            current = (current[0] or t.name in NOT_RENDERED, current[1] or visual_hidden, current[2] or at_hidden)
            state[id(t)] = current
        return current

    words, visible, visible_texts, spoken = Counter(), Counter(), Counter(), Counter()
    for node in soup.find_all(string=True):
        if not _is_text(node) or not isinstance(node.parent, Tag):
            continue
        skip, visual_hidden, at_hidden = flags(node.parent)
        if skip:
            continue
        found = _words(str(node))
        words.update(found)
        in_head = node.find_parent("head") is not None          # the <title>: read out, not shown on the page
        if not visual_hidden and not in_head and found:
            visible.update(found)
            visible_texts[" ".join(str(node).split())] += 1
        if not at_hidden and not in_head:
            spoken.update(found)

    attribute_words, names, controls, hidden_controls, active = Counter(), Counter(), Counter(), Counter(), Counter()
    urls: set[str] = set()
    colours, landmarks = Counter(), Counter()
    decorative: list[tuple[str, bool]] = []
    for tag in soup.find_all(True):
        for attribute in TEXT_ATTRIBUTES:
            if tag.get(attribute):
                attribute_words.update(_words(str(tag[attribute])))
        for attribute in NAME_ATTRIBUTES:
            value = " ".join(str(tag.get(attribute) or "").split())
            if value:
                names[(attribute, value)] += 1
        role = str(tag.get("role", "")).strip().lower()
        _, visual_hidden, at_hidden = flags(tag)
        if tag.name in CONTROLS_AND_MEDIA:
            controls[tag.name] += 1
            if at_hidden or (visual_hidden and tag.name != "input"):
                hidden_controls[tag.name] += 1
            elif tag.name not in ("img", "svg", "picture") and role in ("presentation", "none"):
                hidden_controls[tag.name] += 1
        if tag.name == "img" and (tag.has_attr("alt") and not str(tag["alt"]).strip() or role in ("presentation", "none")):
            decorative.append((str(tag.get("src", "")), _only_content_of_control(tag)))
        if tag.name in ACTIVE_TAGS:
            active[f"<{tag.name}>"] += 1
        if tag.name == "meta" and str(tag.get("http-equiv", "")).lower() == "refresh":
            active["<meta http-equiv=refresh>"] += 1
        for attribute, value in tag.attrs.items():
            text = " ".join(value) if isinstance(value, list) else str(value)
            if attribute.lower().startswith("on"):
                active[f"{attribute} handler"] += 1
            if re.match(r"\s*javascript:", text, re.I):
                active["javascript: URL"] += 1
            if attribute.lower() in URL_ATTRIBUTES:
                first = text.split(",")[0].split()[0] if attribute.lower() == "srcset" and text.split() else text.strip()
                if first and not first.startswith("#") and first.lower() != "about:blank":
                    urls.add(first)
        style = str(tag.get("style") or "") + (tag.get_text() if tag.name == "style" else "")
        urls.update(CSS_URL.findall(style))
        colours.update(c.lower().replace(" ", "") for c in COLOUR.findall(style))
        if tag.name in LANDMARKS or role in ("main", "banner", "contentinfo", "navigation", "complementary", "region",
                                             "search"):
            landmarks[tag.name if tag.name in LANDMARKS else f"role={role}"] += 1
    langs = [str(t.get("lang")) for t in soup.find_all("html") if t.get("lang") is not None]

    markup = _Markup()
    try:
        markup.feed(html or "")
        markup.close()
    except Exception:                                 # html.parser is lenient; this is only a safety net
        pass
    return PageView(words, visible, visible_texts, spoken, attribute_words, names, controls, hidden_controls,
                    decorative, active, urls, colours, langs, landmarks, markup.brackets, markup.unbalanced(),
                    markup.document, _outside(html or ""))


def _only_content_of_control(img: Tag) -> bool:
    """True if this image is all there is inside a link or button (so it must not be decorative)."""
    control = img.find_parent(lambda t: t.name == "button" or (t.name == "a" and t.has_attr("href")))
    if control is None:
        return False
    if control.get("aria-label") or control.get("aria-labelledby") or control.get("title"):
        return False
    if _words(control.get_text(" ")):
        return False
    others = [i for i in control.find_all("img") if i is not img and str(i.get("alt") or "").strip()]
    return not others


# ---------------------------------------------------------------------------
# Output checks: reject a fix (the model tries again)
# ---------------------------------------------------------------------------

@dataclass
class GuardProblem:
    code: str          # removes_content | hides_content | empties_control | active_content | adds_url | broken_markup
    message: str       # what the model is told on its next attempt
    evidence: str = ""


@dataclass
class GuardFlag:
    code: str          # added_visible_text | added_text_alternative | marked_decorative | changed_colours | ...
    message: str       # what the person approving the fix is told


def _quote(items, limit: int = 8) -> str:
    items = list(items)
    shown = ", ".join(f'"{i}"' for i in items[:limit])
    return shown + (f" and {len(items) - limit} more" if len(items) > limit else "")


def _lost(before: Counter, after: Counter) -> list[str]:
    return sorted((before - after).elements())


def check_fix(before_html: str, after_html: str) -> list[GuardProblem]:
    """Reasons to reject the change from ``before_html`` to ``after_html`` (empty = no objection)."""
    before, after = read_page(before_html), read_page(after_html)
    problems: list[GuardProblem] = []

    removed = _lost(before.words, after.words)
    gone = [f"<{n}> x{c}" if c > 1 else f"<{n}>" for n, c in (before.controls - after.controls).items()]
    if removed or gone:
        parts = ([f"text {_quote(removed)}"] if removed else []) + ([f"elements {', '.join(gone)}"] if gone else [])
        problems.append(GuardProblem(
            "removes_content", f"Your change removed content from the page ({'; '.join(parts)}). Keep every element "
            "and all text; fix the problem by adding or changing attributes, not by deleting.", "; ".join(parts)))

    hidden = [w for w in _lost(before.visible, after.visible) if w not in removed]
    silenced = [w for w in _lost(before.spoken, after.spoken) if w not in removed]
    # Newly hidden controls, not counting new ones the fix added (a visually hidden skip link is fine).
    newly = {n: c - max(0, after.controls[n] - before.controls[n])
             for n, c in (after.hidden_controls - before.hidden_controls).items()}
    controls = [f"<{n}> x{c}" if c > 1 else f"<{n}>" for n, c in newly.items() if c > 0]
    if hidden or silenced or controls:
        parts = ([f"text no longer visible: {_quote(hidden)}"] if hidden else []) + \
                ([f"text hidden from screen readers: {_quote(silenced)}"] if silenced else []) + \
                ([f"elements hidden or stripped of their role: {', '.join(controls)}"] if controls else [])
        problems.append(GuardProblem(
            "hides_content", f"Your change hides content ({'; '.join(parts)}). Never hide content (aria-hidden, "
            "hidden, display:none, role=\"presentation\") to make a problem go away; fix the element instead.",
            "; ".join(parts)))

    newly_decorative = Counter(d for d in after.decorative) - Counter(d for d in before.decorative)
    empty_controls = [src for (src, only), n in newly_decorative.items() if only]
    if empty_controls:
        problems.append(GuardProblem(
            "empties_control", "Your change marks the only image inside a link or button as decorative (alt=\"\"), "
            "so the link or button has no name. Give the image alt text that says what the control does.",
            _quote(empty_controls)))

    added_active = after.active - before.active
    if added_active:
        what = ", ".join(added_active)
        problems.append(GuardProblem(
            "active_content", f"Your change adds active content ({what}). Fixes must not add scripts, event "
            "handlers or embedded pages.", what))

    new_urls = sorted(after.urls - before.urls)
    if new_urls:
        problems.append(GuardProblem(
            "adds_url", f"Your change adds links or resources that were not in the page ({_quote(new_urls, 4)}). "
            "Do not add URLs.", _quote(new_urls, 4)))

    broken, how = [], []
    if after.stray_brackets > before.stray_brackets or after_html.count("<<") + after_html.count(">>") > \
            before_html.count("<<") + before_html.count(">>"):
        broken.append("stray \"<\" or \">\" left in the text (for example \"<</end>>\")")
        how.append("the markers \"<<start>>\" and \"<<end>>\" go only in find, written exactly")
    opened = [n for n, c in (after.unbalanced - before.unbalanced).items() if c > 0]
    if opened:
        tags = ", ".join(f"<{n}>" for n in sorted(opened))
        broken.append(f"tags opened or closed without their pair: {tags}")
        how.append(f"close every tag you open ({tags})")
    doubled = [n for n, c in after.document_tags.items() if c > max(1, before.document_tags[n])]
    if doubled:
        tags = ", ".join(f"<{n}>" for n in sorted(doubled))
        broken.append(f"a second {tags}")
        how.append(f"change the existing {tags} instead of adding another")
    if after.outside[0] and after.outside[0] != before.outside[0]:
        broken.append("content placed before <html>")
        how.append("put new content right after the opening <body> tag, or right after the opening <html ...> tag "
                   "if there is no <body>")
    if after.outside[1] and after.outside[1] != before.outside[1]:
        broken.append("content placed after </html>")
        how.append("put new content right before </body> (or </html> if there is no </body>)")
    if broken:
        problems.append(GuardProblem(
            "broken_markup", f"Your change leaves the HTML broken: {'; '.join(broken)}. To fix that: "
            f"{'; '.join(how)}.", "; ".join(broken)))
    return problems


# ---------------------------------------------------------------------------
# Review flags: accepted, but a person should look (shown at approval, counted in evaluation)
# ---------------------------------------------------------------------------

def review_flags(original_html: str, final_html: str) -> list[GuardFlag]:
    """What a person should check in the change from the original page to the final one."""
    before, after = read_page(original_html), read_page(final_html)
    known = before.words + before.attribute_words
    flags: list[GuardFlag] = []

    added = list((after.visible_texts - before.visible_texts).elements())
    new_words = [w for w in dict.fromkeys((after.visible - before.visible).elements()) if w not in known]
    if added and new_words:
        flags.append(GuardFlag("added_visible_text",
                               f"adds visible text: {_quote(added, 5)}; the words {_quote(new_words)} appear nowhere "
                               "in the original page: they were written for this fix. Only you know if they are right."))
    elif added and sum((after.visible - before.visible).values()):
        flags.append(GuardFlag("added_visible_text",
                               f"adds visible text: {_quote(added, 5)} (its words appear elsewhere in the page)."))

    new_names = [f'{attribute}="{value}"' for (attribute, value) in (after.names - before.names)]
    sr_only = sorted(set((after.spoken - after.visible - (before.spoken - before.visible)).elements()))
    if new_names or sr_only:
        parts = new_names[:6] + ([f"screen-reader-only text {_quote(sr_only, 6)}"] if sr_only else [])
        flags.append(GuardFlag("added_text_alternative",
                               "adds text only screen readers hear, written without seeing the page: "
                               + "; ".join(parts) + (" ..." if len(new_names) > 6 else "")))

    decorative = Counter(after.decorative) - Counter(before.decorative)
    if decorative:
        srcs = [src or "(no src)" for (src, _), n in decorative.items() for _ in range(n)]
        flags.append(GuardFlag("marked_decorative",
                               f"marks {len(srcs)} image(s) as decorative (alt=\"\"): {_quote(srcs, 3)}. Right only "
                               "if they carry no information."))

    removed, added = before.colours - after.colours, after.colours - before.colours
    if removed or added:
        flags.append(GuardFlag("changed_colours", f"changes colours: {_quote(removed, 4)} -> {_quote(added, 4)}. "
                               "Check the design still works."))

    if after.langs != before.langs and after.langs:
        flags.append(GuardFlag("sets_language", f"sets the page language to {_quote(after.langs)}. Check it is the "
                               "language the page is written in."))

    landmarks = after.landmarks - before.landmarks
    if landmarks:
        flags.append(GuardFlag("adds_landmarks", "adds page regions: " + ", ".join(f"<{n}>" for n in landmarks)))
    return flags


# ---------------------------------------------------------------------------
# Input screen: prompt injection
# ---------------------------------------------------------------------------

# Known phrasings of instructions aimed at a model. Easy to evade by rewording,
# which is why the classifier below exists; and the classifier can be evaded
# too, which is why the output checks and the approval step exist.
INJECTION_PATTERNS: list[tuple[str, re.Pattern]] = [(name, re.compile(pattern, re.I | re.M)) for name, pattern in [
    ("ignore earlier instructions",
     r"\b(ignore|disregard|forget|override|bypass)\b\W+(?:\w+\W+){0,3}?(previous|prior|above|earlier|preceding|your|"
     r"system|original)\W+(?:\w+\W+){0,2}?(instructions?|prompts?|rules|directions|guidelines|commands|programming)\b"),
    ("new instructions", r"\b(new|updated|real|actual|true|secret|hidden)\s+(instructions?|task|orders|system prompt)"
                         r"\s*[:\-–]"),
    ("your real task", r"\byour\s+(real|actual|true|new|only)\s+(task|job|goal|instructions?|purpose)\b"),
    ("addresses the AI", r"\b(attention|note to|message (for|to)|dear|hey|hello)\s+(the\s+|any\s+|all\s+)?"
                         r"(ai|llms?|language models?|chatgpt|gpt|claude|gemini|copilot|chatbots?|"
                         r"ai\s+(assistants?|agents?|models?|systems?|tools?))\b"),
    ("instructs the AI", r"\b(ai|llms?|language models?|chatbots?|ai\s+(assistants?|agents?|models?))\s*[,:]\s*"
                         r"(you\s+)?(must|should|shall|please|do not|don't|never|always|ignore|delete|remove|add|"
                         r"replace|insert)\b"),
    ("reassigns the AI's role", r"\bfrom now on,?\s+you\s+(will|must|are|should)\b|\bpretend\s+(to be|you are)\s+"
                                r"(an?\s+)?(ai|assistant|model|chatbot|system)\b|\byou are no longer\s+(an?\s+)?"
                                r"(ai|assistant|bound|restricted)\b"),
    ("mentions a system prompt", r"\b(system|developer)\s+prompt\b|\bsystem\s+(message|instructions?)\s*[:\-]"),
    ("chat markup", r"<\|(im_start|im_end|system|user|assistant|endoftext|begin_of_text|eot_id)\|>|\[/?INST\]|"
                    r"<<SYS>>|^\s*#{2,}\s*(system|instruction|assistant)\b"),
    ("asks to hide problems", r"\b(do not|don't|never)\s+(fix|report|mention|flag|repair|reveal|tell)\b\W+(?:\w+\W+)"
                              r"{0,6}?(accessibility|violations?|the user|this (page|content|text))\b"),
]]


def page_texts(html: str) -> list[tuple[str, str]]:
    """(where, text) for everything in a page a model would read as language.

    Text (visible or hidden), comments, the text attributes (alt, title,
    aria-label, ...), and the content of <meta> tags. Script and style text is
    included for the rules but marked, since it is code, not prose.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    out: list[tuple[str, str]] = []
    for node in soup.find_all(string=True):
        text = " ".join(str(node).split())
        if not text:
            continue
        if isinstance(node, Comment):
            out.append(("comment", text))
        elif _is_text(node) and isinstance(node.parent, Tag):
            out.append(("script or style" if node.parent.name in ("script", "style") else "text", text))
    for tag in soup.find_all(True):
        for attribute in TEXT_ATTRIBUTES + ("aria-placeholder", "data-tooltip"):
            value = " ".join(str(tag.get(attribute) or "").split())
            if value:
                out.append((f"{attribute} attribute", value))
        if tag.name == "meta" and tag.get("content") and not tag.get("http-equiv"):
            out.append(("meta content", " ".join(str(tag["content"]).split())))
    seen, unique = set(), []
    for where, text in out:
        if (where, text) not in seen:
            seen.add((where, text))
            unique.append((where, text))
    return unique


@dataclass
class Finding:
    source: str        # "rules" or "model"
    where: str         # comment, text, alt attribute, ...
    reason: str        # which pattern, or the classifier's score
    excerpt: str


@dataclass
class Screening:
    findings: list[Finding] = field(default_factory=list)
    model: str | None = None          # the classifier used, if any
    segments: int = 0                 # pieces of text the classifier scored
    note: str = ""                    # e.g. "prompt guard unavailable: ..."

    @property
    def suspicious(self) -> bool:
        return bool(self.findings)

    def describe(self) -> str:
        if not self.findings:
            checked = f"rules and {self.model} ({self.segments} piece(s) of text)" if self.model else "rules only"
            return f"no instructions to a model found ({checked})" + (f"; {self.note}" if self.note else "")
        shown = "; ".join(f"{f.source}: {f.reason} in {f.where}: \"{f.excerpt}\"" for f in self.findings[:3])
        more = f" (+{len(self.findings) - 3} more)" if len(self.findings) > 3 else ""
        return f"text that looks like instructions to a model: {shown}{more}"

    def as_dict(self) -> dict:
        return {"suspicious": self.suspicious, "findings": [asdict(f) for f in self.findings], "model": self.model,
                "segments": self.segments, "note": self.note}


def _excerpt(text: str, match: re.Match | None = None, width: int = 160) -> str:
    if match is None:
        return text if len(text) <= width else text[: width - 1] + "…"
    start = max(0, match.start() - 40)
    piece = text[start:start + width]
    return ("…" if start else "") + piece + ("…" if start + width < len(text) else "")


def screen_with_rules(texts: list[tuple[str, str]]) -> list[Finding]:
    findings = []
    for where, text in texts:
        for name, pattern in INJECTION_PATTERNS:
            match = pattern.search(text)
            if match:
                findings.append(Finding("rules", where, name, _excerpt(text, match)))
                break
    return findings


class GuardUnavailable(Exception):
    """The classifier could not be used (no key, an error, or a reply we cannot read)."""


def parse_guard_reply(text: str) -> float:
    """The probability of an attack from Prompt Guard's reply.

    Groq returns the classifier's output as the message text. This accepts a
    number ("0.9993"), a label ("MALICIOUS", "BENIGN", "LABEL_1"), or JSON with
    a score; anything else raises GuardUnavailable rather than guessing.
    """
    raw = (text or "").strip()
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if isinstance(data, list) and data:
        data = max(data, key=lambda d: d.get("score", 0) if isinstance(d, dict) else 0)
    if isinstance(data, dict):
        label = str(data.get("label", "")).lower()
        score = data.get("score", data.get("probability", data.get("malicious")))
        if isinstance(score, (int, float)):
            return float(score) if not label or re.search(r"malicious|injection|jailbreak|unsafe|label_1", label) \
                else 1.0 - float(score)
    if isinstance(data, (int, float)) and 0 <= float(data) <= 1:
        return float(data)
    number = re.fullmatch(r"\s*(0(\.\d+)?|1(\.0+)?)\s*", raw)
    if number:
        return float(number.group(1))
    lowered = raw.lower()
    if re.search(r"malicious|injection|jailbreak|unsafe|label_1", lowered):
        return 1.0
    if re.search(r"benign|\bsafe\b|label_0", lowered):
        return 0.0
    raise GuardUnavailable(f"cannot read the classifier's reply {raw[:80]!r}")


class PromptGuard:
    """Llama Prompt Guard 2 on Groq: the probability that a piece of text is a prompt attack.

    The model reads at most 512 tokens, so a page's text is cut into pieces of
    about 1,200 characters and each piece is scored. Replies are cached like
    every other model call, so re-running an evaluation costs nothing.
    """

    SEGMENT_CHARS = 1200
    MAX_SEGMENTS = 40

    def __init__(self, spec: str, *, threshold: float = 0.5, cache=None, llm=None, limiter=None):
        from .model_client import ModelClient
        self.spec, self.threshold = spec, threshold
        self.client = ModelClient(spec, llm=llm, cache=cache, limiter=limiter, max_request_tokens=0)

    @classmethod
    def from_settings(cls, cache=None) -> "PromptGuard | None":
        """The configured classifier, or None (no PROMPT_GUARD_MODEL, or its API key is missing)."""
        from .llm import missing_key
        spec = settings.prompt_guard_model
        if not spec or spec.lower() in ("none", "off", "false", "0"):
            return None
        try:
            if missing_key(spec):
                return None
        except ValueError:
            return None
        return cls(spec, threshold=settings.prompt_guard_threshold, cache=cache)

    async def score(self, text: str) -> float:
        reply, _ = await self.client.chat([("human", text)], max_tokens=16)
        return parse_guard_reply(reply)

    @classmethod
    def segments(cls, texts: list[tuple[str, str]]) -> list[tuple[str, str]]:
        """Join short texts and cut long ones into pieces the classifier can read."""
        pieces: list[tuple[str, str]] = []
        current, kinds = "", []
        for where, text in texts:
            if where == "script or style":
                continue
            while len(text) > cls.SEGMENT_CHARS:              # cut at a space near the limit
                cut = text.rfind(" ", 0, cls.SEGMENT_CHARS)
                cut = cut if cut > cls.SEGMENT_CHARS // 2 else cls.SEGMENT_CHARS
                pieces.append((where, text[:cut]))
                text = text[cut:].lstrip()
            if current and len(current) + len(text) + 1 > cls.SEGMENT_CHARS:
                pieces.append((", ".join(dict.fromkeys(kinds)), current))
                current, kinds = "", []
            current = f"{current}\n{text}" if current else text
            kinds.append(where)
        if current:
            pieces.append((", ".join(dict.fromkeys(kinds)), current))
        return pieces

    async def screen(self, texts: list[tuple[str, str]]) -> tuple[list[Finding], int, str]:
        pieces = self.segments(texts)
        note = ""
        if len(pieces) > self.MAX_SEGMENTS:
            note = f"only the first {self.MAX_SEGMENTS} of {len(pieces)} pieces of text were scored"
            pieces = pieces[: self.MAX_SEGMENTS]
        findings = []
        for where, text in pieces:
            score = await self.score(text)
            if score >= self.threshold:
                findings.append(Finding("model", where, f"attack probability {score:.2f}", _excerpt(text)))
        return findings, len(pieces), note


async def screen_page(html: str, prompt_guard: PromptGuard | None = None) -> Screening:
    """Rules always; the classifier too when one is configured. A classifier failure is noted, not fatal."""
    texts = page_texts(html)
    result = Screening(findings=screen_with_rules(texts))
    if prompt_guard is not None:
        try:
            findings, result.segments, result.note = await prompt_guard.screen(texts)
            result.findings += findings
            result.model = prompt_guard.spec.split(":", 1)[-1]
        except Exception as e:                    # rules still apply; the trace says the classifier did not
            result.note = f"prompt guard unavailable ({type(e).__name__}: {str(e).splitlines()[0][:120]}); rules only"
    return result


# ---------------------------------------------------------------------------
# What the agent carries in its runtime context
# ---------------------------------------------------------------------------

@dataclass
class Guardrails:
    """Guardrail settings for one run (``AgentContext.guard``).

    ``approval``: "auto" approves the final diff and records its flags (the
    evaluation, where no person is present); "ask" pauses the graph so a person
    approves, edits or rejects it (``python -m a11y_agent.agent --approve``).
    """
    prompt_guard: PromptGuard | None = None
    approval: str = "auto"

    async def screen(self, html: str) -> Screening:
        return await screen_page(html, self.prompt_guard)


# ---------------------------------------------------------------------------
# Measuring the input screen: injection examples and real pages
# ---------------------------------------------------------------------------

def labelled_pages() -> list[tuple[str, str, bool]]:
    """(name, html, is_attack): the injection examples, the two example pages, and the dev split's pages."""
    from .benchmark import load_split
    pages = [(p.name, p.read_text(encoding="utf-8"), p.name.startswith("attack-"))
             for p in sorted(EXAMPLES_DIR.glob("*.html"))]
    pages += [(p.name, p.read_text(encoding="utf-8"), False) for p in sorted((ROOT / "examples").glob("*.html"))]
    pages += [(item.id, item.html, False) for item in load_split("accessguru", "dev")]
    return pages


async def evaluate_screen(prompt_guard: PromptGuard | None) -> dict:
    rows = []
    for name, html, attack in labelled_pages():
        texts = page_texts(html)
        by_rules = screen_with_rules(texts)
        by_model, note = None, ""
        if prompt_guard is not None:
            try:
                found, _, note = await prompt_guard.screen(texts)
                by_model = bool(found)
            except Exception as e:
                note = f"{type(e).__name__}: {str(e).splitlines()[0][:100]}"
        rows.append({"page": name, "attack": attack, "rules": bool(by_rules), "model": by_model,
                     "rule_reasons": sorted({f.reason for f in by_rules}), "note": note})
    return {"model": prompt_guard.spec if prompt_guard else None,
            "threshold": prompt_guard.threshold if prompt_guard else None, "pages": rows}


def screen_table(report: dict) -> str:
    def rate(rows, key):
        values = [r[key] for r in rows if r[key] is not None]
        return f"{sum(values)}/{len(values)}" if values else "-"
    groups = [("Injection examples (attacks)", [r for r in report["pages"] if r["attack"]]),
              ("Benign examples (incl. an article about injection)",
               [r for r in report["pages"] if not r["attack"] and not r["page"].startswith("accessguru")]),
              ("Dev pages (real websites, benign)", [r for r in report["pages"] if r["page"].startswith("accessguru")])]
    lines = ["| Pages | Count | Flagged by rules | Flagged by Prompt Guard | Flagged by either |", "|---|---|---|---|---|"]
    for label, rows in groups:
        either = [r["rules"] or bool(r["model"]) for r in rows]
        lines.append(f"| {label} | {len(rows)} | {rate(rows, 'rules')} | {rate(rows, 'model')} | "
                     f"{sum(either)}/{len(rows)} |")
    detail = ["", "| Example | Attack? | Rules | Prompt Guard |", "|---|---|---|---|"]
    for r in report["pages"]:
        if r["page"].startswith("accessguru"):
            continue
        model = "-" if r["model"] is None else ("flagged" if r["model"] else "no")
        detail.append(f"| {r['page']} | {'yes' if r['attack'] else 'no'} | "
                      f"{', '.join(r['rule_reasons']) or 'no'} | {model} |")
    false_alarms = [r["page"] for r in report["pages"] if not r["attack"] and r["page"].startswith("accessguru")
                    and (r["rules"] or r["model"])]
    tail = [f"\nDev pages flagged: {', '.join(false_alarms)}"] if false_alarms else []
    return "\n".join(lines + detail + tail)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Guardrails: input screening and output checks.")
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("screen", help="screen one HTML file for prompt injection")
    s.add_argument("file")
    c = sub.add_parser("check", help="check one fix: the HTML before and after")
    c.add_argument("before")
    c.add_argument("after")
    e = sub.add_parser("eval", help="screen the injection examples and the dev pages")
    e.add_argument("--save", action="store_true", help="write results/guardrails/")
    e.add_argument("--no-model", action="store_true", help="rules only, even if GROQ_API_KEY is set")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    from .model_client import ResponseCache
    cache = ResponseCache(settings.runs_dir / "cache.sqlite")
    if args.command == "check":
        before = Path(args.before).read_text(encoding="utf-8")
        after = Path(args.after).read_text(encoding="utf-8")
        problems, flags = check_fix(before, after), review_flags(before, after)
        for p in problems:
            print(f"REJECT  {p.code:16} {p.evidence}")
        for f in flags:
            print(f"REVIEW  {f.code:22} {f.message}")
        if not problems and not flags:
            print("No objections and nothing to review.")
        return 1 if problems else 0

    guard = None if getattr(args, "no_model", False) else PromptGuard.from_settings(cache)
    if args.command == "screen":
        result = asyncio.run(screen_page(Path(args.file).read_text(encoding="utf-8"), guard))
        print(result.describe())
        for f in result.findings:
            print(f"  {f.source:5} {f.where:20} {f.reason}: {f.excerpt}")
        return 1 if result.suspicious else 0

    if guard is None:
        print("Rules only (set GROQ_API_KEY in .env to add Llama Prompt Guard 2).")
    report = asyncio.run(evaluate_screen(guard))
    table = screen_table(report)
    print(table)
    if args.save:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        (RESULTS_DIR / "screen.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        (RESULTS_DIR / "README.md").write_text(
            "# Input screen: prompt injection\n\nMade with `python -m a11y_agent.guardrails eval --save`. "
            f"Classifier: {report['model'] or 'none (rules only)'}"
            + (f", threshold {report['threshold']}" if report["threshold"] is not None else "") + ".\n\n"
            + table + "\n", encoding="utf-8")
        print(f"\nSaved to {RESULTS_DIR.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
