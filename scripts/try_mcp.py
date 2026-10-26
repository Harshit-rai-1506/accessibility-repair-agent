"""Talk to the MCP server the way any MCP client does, and print what happens.

    python scripts/try_mcp.py

It starts the server over stdio, lists what the server offers, and calls
each kind of tool once. No API key needed: no language model is involved,
only the protocol.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from a11y_agent.mcp_client import connect  # noqa: E402


def show(title: str, text: str, limit: int = 600) -> None:
    text = text if len(text) <= limit else text[:limit] + " …"
    print(f"\n== {title}\n{text}")


async def main() -> int:
    html = (ROOT / "examples" / "broken_page.html").read_text(encoding="utf-8")
    async with connect() as server:
        s = server.session
        tools = (await s.list_tools()).tools
        show("Tools (name: description)", "\n".join(f"{t.name}: {(t.description or '').splitlines()[0]}" for t in tools))
        show("Input schema of contrast_fix", json.dumps(next(t for t in tools if t.name == "contrast_fix").inputSchema,
                                                        indent=1))
        templates = (await s.list_resource_templates()).resourceTemplates
        prompts = (await s.list_prompts()).prompts
        show("Resources and prompts", f"resources: {[t.uriTemplate for t in templates]}\n"
                                      f"prompts: {[p.name for p in prompts]}")

        scan = (await s.call_tool("scan_html", {"html": html, "max_nodes": 1})).structuredContent
        show("scan_html(examples/broken_page.html)",
             f"violation score {scan['violation_score']}\n" +
             "\n".join(f"  [{v['impact']}] {v['rule_id']} ({v['element_count']} element(s))" for v in scan["violations"]))

        result = await s.call_tool("contrast_fix", {"foreground": "#767676", "background": "#ffffff", "target_ratio": 7})
        show("contrast_fix('#767676', '#ffffff', 7)", json.dumps(result.structuredContent, indent=1))

        result = await s.call_tool("explain_rule", {"rule_id": "target-size"})
        show("explain_rule('target-size')", result.content[0].text)

        result = await s.call_tool("explain_rule", {"rule_id": "image-alts"})
        show("explain_rule('image-alts')  <- a mistake, returned as an error result",
             f"isError={result.isError}: {result.content[0].text}")
    print("\nServer stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
