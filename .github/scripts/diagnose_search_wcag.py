"""Temporary CI diagnosis: why the first search_wcag call hangs on Windows.

Runs the same work three ways and reports each as an annotation on the run page:
1. building the index in a plain child process, with the MCP server's minimal
   environment and with the full one (is it the environment, or the work?);
2. calling search_wcag over MCP stdio, with the server dumping its own thread
   stacks if it is still busy after 60 s (where exactly is it stuck?).
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
envs = {"minimal env": base["env"], "full env": dict(os.environ)}

BUILD = ("import time; t = time.time(); from a11y_agent.retrieval import shared_index; "
         "print('imported in', round(time.time() - t, 1), 's', flush=True); index = shared_index(); "
         "print('built', len(index.chunks), 'chunks in', round(time.time() - t, 1), 's', flush=True)")
for name, env in envs.items():
    started = time.time()
    try:
        done = subprocess.run([sys.executable, "-c", BUILD], env=env, cwd=base["cwd"],
                              capture_output=True, text=True, timeout=150)
        note(f"index build, plain child, {name}",
             f"exit {done.returncode} after {time.time() - started:.0f} s\n{done.stdout}\n{done.stderr}")
    except subprocess.TimeoutExpired as stuck:
        note(f"index build, plain child, {name}", f"TIMED OUT after 150 s\n{stuck.stdout}\n{stuck.stderr}", "error")


async def over_mcp(name: str, env: dict) -> None:
    stacks = Path(f"child-stacks-{name.split()[0]}.txt").resolve()
    stacks.unlink(missing_ok=True)
    wrapper = (f"import faulthandler, runpy; out = open(r'{stacks}', 'w'); "
               "faulthandler.dump_traceback_later(60, repeat=False, file=out); "
               "runpy.run_module('a11y_agent.mcp_server', run_name='__main__', alter_sys=True)")
    connection = dict(base, env=env, args=["-c", wrapper])
    started = time.time()
    async with connect(connection) as server:
        note(f"MCP {name}: connected", f"after {time.time() - started:.0f} s", "notice")
        try:
            result = await asyncio.wait_for(
                server.session.call_tool("search_wcag", {"query": "alt text for images", "k": 2}), 120)
            note(f"MCP {name}: search_wcag answered", f"after {time.time() - started:.0f} s, "
                 f"{len(result.structuredContent['passages'])} passages", "notice")
        except (asyncio.TimeoutError, TimeoutError):
            text = stacks.read_text(encoding="utf-8", errors="replace") if stacks.exists() else "(no dump)"
            for i, part in enumerate(text.split("\n\n")[:6]):          # one annotation per thread
                note(f"MCP {name}: server stack {i + 1} after 60 s", part, "error")
            os._exit(1)                                                 # closing the session may hang too


for name, env in envs.items():
    asyncio.run(over_mcp(name, env))
