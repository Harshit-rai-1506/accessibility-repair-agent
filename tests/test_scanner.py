"""The real scanner: headless Chromium + axe-core on the example pages."""

import asyncio

import pytest

from a11y_agent.scanner import Scanner
from a11y_agent.scoring import evaluate_fix, violation_score

pytestmark = pytest.mark.browser


def scan_files(*paths):
    async def run():
        async with Scanner() as scanner:
            return [await scanner.scan_html(p.read_text(encoding="utf-8")) for p in paths]
    return asyncio.run(run())


def test_broken_page_has_expected_violations(examples_dir):
    (result,) = scan_files(examples_dir / "broken_page.html")
    expected = {"image-alt", "button-name", "link-name", "html-has-lang", "document-title",
                "color-contrast", "meta-viewport", "empty-heading", "landmark-one-main"}
    assert expected <= result.rule_ids
    assert violation_score(result) > 30


def test_fixed_page_is_clean(examples_dir):
    (result,) = scan_files(examples_dir / "fixed_page.html")
    assert result.violations == []


def test_violation_details_are_useful(examples_dir):
    (result,) = scan_files(examples_dir / "broken_page.html")
    img = result.get("image-alt")
    assert img.impact == "critical"
    assert "1.1.1" in img.wcag_criteria
    assert all(node.target and "<img" in node.html for node in img.nodes)


def test_fix_verification_end_to_end(examples_dir):
    broken, fixed = scan_files(examples_dir / "broken_page.html", examples_dir / "fixed_page.html")
    for rule in broken.rule_ids:
        outcome = evaluate_fix(broken, fixed, rule)
        assert outcome.resolved, rule


def test_snippet_scan_and_network_blocked():
    async def run():
        async with Scanner() as scanner:
            # The image URL would normally be fetched; with the network blocked it must not hang.
            return await scanner.scan_html('<img src="https://example.com/cat.png">')
    result = asyncio.run(run())
    assert "image-alt" in result.rule_ids


def test_scan_url_checks_every_redirect():
    """A page on an allowed server redirects to a forbidden one: the scan stops before reaching it."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlparse

    from a11y_agent.scanner import BlockedRequest, Scanner

    hits = []

    class Allowed(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/start":
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{forbidden.server_port}/secret")
                self.end_headers()
                return
            body = b'<!doctype html><html lang="en"><head><title>T</title></head><body><main><h1>T</h1>' \
                   b'<img src="/cat.png"></main></body></html>'
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    class Forbidden(Allowed):
        def do_GET(self):
            hits.append(self.path)
            super().do_GET()

    allowed, forbidden = ThreadingHTTPServer(("127.0.0.1", 0), Allowed), ThreadingHTTPServer(("127.0.0.1", 0), Forbidden)
    for server in (allowed, forbidden):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    only_allowed = lambda url: urlparse(url).port == allowed.server_port

    async def go():
        async with Scanner() as scanner:
            ok = await scanner.scan_url(f"http://127.0.0.1:{allowed.server_port}/page", allow_url=only_allowed)
            with pytest.raises(BlockedRequest, match="/secret"):
                await scanner.scan_url(f"http://127.0.0.1:{allowed.server_port}/start", allow_url=only_allowed)
            return ok
    try:
        ok = asyncio.run(go())
    finally:
        allowed.shutdown()
        forbidden.shutdown()
    assert "image-alt" in ok.rule_ids                # pages still load and scan with the check on
    assert hits == []                                # the forbidden server was never contacted


def test_a_slow_page_is_snapshot_with_what_has_arrived():
    """Real sites keep loading (ads, trackers): a patient snapshot scans the page instead of timing out."""
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Slow(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/never.png":
                time.sleep(4)                         # longer than the scanner's timeout below
            status = 404 if self.path == "/missing" else 200
            self.send_response(status)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b'<!doctype html><html lang="en"><head><title>T</title></head><body><main>'
                             b'<h1>Hello</h1><img src="/never.png"></main></body></html>')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"

    async def go():
        async with Scanner(timeout_ms=1_000) as scanner:
            with pytest.raises(Exception, match="Timeout"):
                await scanner.snapshot_url(f"{base}/")             # waits for "load", which never comes in time
            return await scanner.snapshot_url(f"{base}/", patient=True), \
                await scanner.snapshot_url(f"{base}/missing", patient=True)
    try:
        page, missing = asyncio.run(go())
    finally:
        server.shutdown()
    assert "<h1>Hello</h1>" in page.html and "image-alt" in page.scan.rule_ids
    assert page.status == 200 and page.url == f"{base}/"
    assert missing.status == 404


def test_a_page_whose_policy_forbids_inline_scripts_can_still_be_scanned():
    """flipkart.com and passportindia.gov.in send a Content-Security-Policy that blocked our axe-core script."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    body = b'<!doctype html><html lang="en"><head><title>T</title></head><body><main><h1>T</h1>' \
           b'<img src="/cat.png"></main></body></html>'

    class Strict(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Security-Policy", "script-src 'self'")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Strict)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    meta = body.decode().replace("<title>", "<meta http-equiv=\"Content-Security-Policy\" content=\"script-src 'self'\"><title>")

    async def go():
        async with Scanner() as scanner:
            return (await scanner.snapshot_url(f"http://127.0.0.1:{server.server_port}/", patient=True),
                    await scanner.scan_html(meta))
    try:
        live, offline = asyncio.run(go())
    finally:
        server.shutdown()
    assert "image-alt" in live.scan.rule_ids and "image-alt" in offline.rule_ids
