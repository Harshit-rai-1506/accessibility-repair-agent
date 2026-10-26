"""Temporary CI diagnosis: why the first search_wcag call hangs on Windows.

Round 1 found: building the index in a plain child takes 8 s; over MCP stdio the
server's main thread is stuck in an import while another thread waits on stdin.
Round 2 calls search_wcag over MCP three ways, with the server dumping its thread
stacks (innermost frames first) if it is still busy after 45 s:
- as is;
- with the protocol reading its own copy of the stdin handle and fd 0 pointed at
  NUL (if something touches standard input while a read is pending, this fixes it);
- with the retrieval modules imported before the server starts reading.
"""

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path


def note(title: str, text: str, level: str = "warning") -> None:
    text = text.strip()[-3800:] or "(no output)"
    escaped = text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::{level} title={title}::{escaped}", flush=True)


os.environ["A11Y_EMBEDDINGS"] = "hash"
from a11y_agent.mcp_client import connect, server_connection  # noqa: E402

base = server_connection()

DETACH = ("import os, sys; fd = os.dup(0); sys.stdin = open(fd, 'r', encoding='utf-8'); "
          "null = os.open(os.devnull, os.O_RDONLY); os.dup2(null, 0); os.close(null); ")
VARIANTS = {"as is": "", "stdin moved off fd 0": DETACH, "retrieval imported first": "import a11y_agent.retrieval; "}


async def over_mcp(name: str, prelude: str) -> None:
    stacks = Path(f"child-stacks-{len(prelude)}.txt").resolve()
    stacks.unlink(missing_ok=True)
    wrapper = (prelude + f"import faulthandler, runpy; out = open(r'{stacks}', 'w'); "
               "faulthandler.dump_traceback_later(45, repeat=False, file=out); "
               "runpy.run_module('a11y_agent.mcp_server', run_name='__main__', alter_sys=True)")
    connection = dict(base, args=["-c", wrapper])
    started = time.time()
    async with connect(connection) as server:
        try:
            result = await asyncio.wait_for(
                server.session.call_tool("search_wcag", {"query": "alt text for images", "k": 2}), 75)
            note(f"MCP {name}: search_wcag answered", f"after {time.time() - started:.0f} s, "
                 f"{len(result.structuredContent['passages'])} passages", "notice")
        except (asyncio.TimeoutError, TimeoutError):
            text = stacks.read_text(encoding="utf-8", errors="replace") if stacks.exists() else "(no dump)"
            for i, part in enumerate(text.split("\n\n")[:4]):          # one annotation per thread,
                note(f"MCP {name}: server thread {i + 1}", part[:3800], "error")   # innermost frames first
            raise SystemExit(f"{name}: stuck")


if len(sys.argv) > 1:                                    # one variant, in its own process
    asyncio.run(over_mcp(sys.argv[1], VARIANTS[sys.argv[1]]))
    os._exit(0)                                          # skip any slow shutdown

for name in VARIANTS:                                    # a stuck variant cannot block the others
    try:
        subprocess.run([sys.executable, __file__, name], timeout=120)
    except subprocess.TimeoutExpired:
        note(f"MCP {name}", "the variant did not finish within 120 s", "error")
