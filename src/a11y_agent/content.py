"""Did a fix keep the page's content?

The accessibility metric only asks "is the violation gone?". Deleting the
offending element makes it go, so every fix is also checked for how much of
the original survives. The evaluation uses this to flag "fixes" that remove
content; the agent uses it to reject them and try again.
"""

from __future__ import annotations

from collections import Counter

from bs4 import BeautifulSoup

DROPPED_CONTENT = 0.9   # below this share of words or elements kept, a fix is flagged


def content_kept(original: str, fixed: str) -> tuple[float, float]:
    """(share of the original's words still present, share of its elements still present).

    Adding words or elements never lowers the score; only removing them does.
    """
    def words(html: str) -> Counter:
        return Counter(BeautifulSoup(html, "html.parser").get_text(" ").lower().split())

    def tags(html: str) -> Counter:
        return Counter(t.name for t in BeautifulSoup(html, "html.parser").find_all(True))

    w0, t0 = words(original), tags(original)
    w1, t1 = words(fixed), tags(fixed)
    text = sum((w0 & w1).values()) / sum(w0.values()) if w0 else 1.0
    elements = sum((t0 & t1).values()) / sum(t0.values()) if t0 else 1.0
    return round(text, 3), round(elements, 3)


def dropped_content(original: str, fixed: str) -> bool:
    """True if the fix removed more than a tenth of the words or elements."""
    return min(content_kept(original, fixed)) < DROPPED_CONTENT
