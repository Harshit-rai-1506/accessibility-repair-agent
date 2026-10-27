"""The accessibility scanner: runs the axe-core rule engine inside headless Chromium.

This is the agent's "ground truth". The language model proposes fixes, but
only this scanner decides whether a violation is gone. Keeping that judgement
in deterministic code (not in the model) is what makes the agent verifiable.

Usage from the command line:

    python -m a11y_agent.scanner examples/broken_page.html
    python -m a11y_agent.scanner https://example.com --allow-network

Usage from code (async, because the agent and MCP server run in an event loop):

    async with Scanner() as scanner:
        result = await scanner.scan_html("<img src='cat.png'>")
        print(result.rule_ids)          # {'image-alt', 'html-has-lang', ...}
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urljoin

from playwright.async_api import Browser, Playwright, async_playwright

from .config import settings

# Which axe rule groups to run. AAA and best-practice rules are included because
# the AccessGuru benchmark contains them (e.g. "color-contrast-enhanced", "region").
# Legal requirements in most countries stop at WCAG AA; see docs/PLAN.md.
DEFAULT_TAGS: tuple[str, ...] = (
    "wcag2a", "wcag2aa", "wcag2aaa",
    "wcag21a", "wcag21aa",
    "wcag22aa",
    "best-practice",
)

# Runs inside the browser page. Returns plain JSON so Python can parse it.
_RUN_AXE = """
async (tags) => {
  const r = await axe.run(document, {
    runOnly: { type: 'tag', values: tags },
    resultTypes: ['violations'],
  });
  return {
    version: axe.version,
    violations: r.violations.map(v => ({
      rule_id: v.id,
      impact: v.impact,
      help: v.help,
      help_url: v.helpUrl,
      tags: v.tags,
      nodes: v.nodes.map(n => ({
        target: n.target.map(t => Array.isArray(t) ? t.join(' >>> ') : String(t)),
        html: n.html,
        failure_summary: n.failureSummary || '',
      })),
    })),
  };
}
"""

_WCAG_TAG = re.compile(r"^wcag(\d)(\d)(\d+)$")  # "wcag1412" -> 1.4.12


@dataclass
class ViolationNode:
    """One element on the page that breaks a rule."""
    target: list[str]          # CSS selector path to the element
    html: str                  # the element's HTML as axe saw it
    failure_summary: str       # axe's explanation of what is missing


@dataclass
class Violation:
    """One broken rule, with every element that breaks it."""
    rule_id: str               # e.g. "image-alt"
    impact: str | None         # "minor" | "moderate" | "serious" | "critical"
    help: str                  # one-line description of the rule
    help_url: str              # Deque's documentation page for the rule
    tags: list[str]
    nodes: list[ViolationNode] = field(default_factory=list)

    @property
    def wcag_criteria(self) -> list[str]:
        """WCAG success criteria this rule maps to, e.g. ["1.1.1"]."""
        out = []
        for tag in self.tags:
            m = _WCAG_TAG.match(tag)
            if m:
                out.append(".".join(m.groups()))
        return out


@dataclass
class ScanResult:
    violations: list[Violation]
    axe_version: str

    @property
    def rule_ids(self) -> set[str]:
        return {v.rule_id for v in self.violations}

    def get(self, rule_id: str) -> Violation | None:
        return next((v for v in self.violations if v.rule_id == rule_id), None)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_axe(cls, raw: dict) -> "ScanResult":
        violations = [
            Violation(
                rule_id=v["rule_id"],
                impact=v.get("impact"),
                help=v.get("help", ""),
                help_url=v.get("help_url", ""),
                tags=list(v.get("tags", [])),
                nodes=[ViolationNode(**n) for n in v.get("nodes", [])],
            )
            for v in raw.get("violations", [])
        ]
        return cls(violations=violations, axe_version=raw.get("version", "unknown"))


class BlockedRequest(Exception):
    """A page asked for an address the scan is not allowed to reach (see ``Scanner.scan_url``)."""


class Scanner:
    """Keeps one Chromium browser open and scans many pages with it.

    Starting a browser takes about a second; a new isolated context per scan
    takes milliseconds. Reusing the browser matters when we scan hundreds of
    benchmark rows on a laptop.
    """

    def __init__(self, axe_path: Path | None = None, tags: tuple[str, ...] = DEFAULT_TAGS,
                 timeout_ms: int = 20_000):
        path = Path(axe_path or settings.axe_path)
        if not path.exists():
            raise FileNotFoundError(
                f"axe-core not found at {path}. Run: python scripts/fetch_resources.py --only axe"
            )
        self._axe_source = path.read_text(encoding="utf-8")
        self._tags = list(tags)
        self._timeout_ms = timeout_ms
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None

    async def __aenter__(self) -> "Scanner":
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=True)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    async def scan_html(self, html: str, *, allow_network: bool = False) -> ScanResult:
        """Scan an HTML string. Network access is blocked by default.

        Blocking the network is a safety choice: the HTML may come from an
        untrusted page, and nothing in it should be able to call out. It also
        makes scans faster and repeatable.
        """
        if self._browser is None:
            raise RuntimeError("use 'async with Scanner() as scanner:'")
        context = await self._browser.new_context()
        try:
            page = await context.new_page()
            page.set_default_timeout(self._timeout_ms)
            if not allow_network:
                await page.route("**/*", lambda route: route.abort())
            await page.set_content(html or "", wait_until="domcontentloaded")
            return await self._run_axe(page)
        finally:
            await context.close()

    async def scan_url(self, url: str, *, allow_url=None) -> ScanResult:
        """Scan a live web page (network allowed, since we must load it).

        ``allow_url(url) -> bool``, if given, is asked about every request the
        page makes *and every redirect*: a refused one is blocked and the scan
        fails with ``BlockedRequest``. The MCP server passes its "public
        addresses only" check, so a public page cannot redirect the server to
        this machine or the local network. (Chromium follows redirects without
        asking, so each request is fetched with redirects off, the target
        checked, and the redirect handed back to the browser.)
        """
        if self._browser is None:
            raise RuntimeError("use 'async with Scanner() as scanner:'")
        context = await self._browser.new_context()
        blocked: list[str] = []
        try:
            page = await context.new_page()
            page.set_default_timeout(self._timeout_ms)
            if allow_url is not None:
                async def check(route):
                    request_url = route.request.url
                    if request_url.startswith(("data:", "blob:")):
                        return await route.continue_()
                    if not allow_url(request_url):
                        blocked.append(request_url)
                        return await route.abort("blockedbyclient")
                    response = await route.fetch(max_redirects=0)
                    location = response.headers.get("location")
                    if 300 <= response.status < 400 and location:
                        target = urljoin(request_url, location)
                        if not allow_url(target):
                            blocked.append(target)
                            return await route.abort("blockedbyclient")
                    await route.fulfill(response=response)
                await page.route("**/*", check)
            try:
                await page.goto(url, wait_until="load")
            except Exception as error:
                if blocked:
                    raise BlockedRequest(f"blocked a request to {blocked[0]}") from error
                raise
            if blocked and page.url in blocked:
                raise BlockedRequest(f"blocked a request to {blocked[0]}")
            return await self._run_axe(page)
        finally:
            await context.close()

    async def _run_axe(self, page) -> ScanResult:
        await page.add_script_tag(content=self._axe_source)
        raw = await page.evaluate(_RUN_AXE, self._tags)
        return ScanResult.from_axe(raw)


async def scan_once(html: str | None = None, url: str | None = None, *, allow_network: bool = False) -> ScanResult:
    """Convenience wrapper for one-off scans (starts and stops a browser)."""
    async with Scanner() as scanner:
        if url:
            return await scanner.scan_url(url)
        return await scanner.scan_html(html or "", allow_network=allow_network)


def _main(argv: list[str] | None = None) -> int:
    from .scoring import violation_score

    parser = argparse.ArgumentParser(description="Scan an HTML file or URL with axe-core.")
    parser.add_argument("target", help="path to an .html file, or a URL")
    parser.add_argument("--allow-network", action="store_true", help="let a local file load external resources")
    parser.add_argument("--json", action="store_true", help="print the raw result as JSON")
    args = parser.parse_args(argv)

    if args.target.startswith(("http://", "https://")):
        result = asyncio.run(scan_once(url=args.target))
    else:
        html = Path(args.target).read_text(encoding="utf-8")
        result = asyncio.run(scan_once(html=html, allow_network=args.allow_network))

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
        return 0
    print(f"axe-core {result.axe_version}: {len(result.violations)} rule(s) violated, "
          f"violation score {violation_score(result)}")
    for v in sorted(result.violations, key=lambda v: v.rule_id):
        criteria = ", ".join(v.wcag_criteria) or "best practice"
        print(f"  [{v.impact or '?':8}] {v.rule_id:28} {len(v.nodes)} element(s)  WCAG {criteria}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
