# Phase 3: MCP tools — what we built and why

**Goal of this phase:** move the agent's tools (scanner, fix checker, rule
explanations, a contrast calculator) into an **MCP server**, make the agent
use them as an MCP client, and let the model decide for itself when to call
two of them.

**The one idea to take away:** a tool is a contract (a name, a description,
an input schema, an output) that an agent can call without knowing how it is
implemented. MCP makes that contract a standard, so the same tools work for
our agent, for Claude's desktop app, for an IDE, or for someone else's agent,
without changing a line.

---

## 1. What MCP is

The Model Context Protocol connects an AI application (the **host**) to
**servers** that provide capabilities. Inside the host, a **client** keeps one
connection per server. A server can offer three kinds of things:

| Kind | What it is | Ours |
|---|---|---|
| **Tools** | Functions a model or program can call, with a JSON schema for the inputs | `scan_html`, `scan_url`, `verify_fix`, `explain_rule`, `contrast_fix` |
| **Resources** | Read-only data, fetched by URI | `axe-rule://{rule_id}` |
| **Prompts** | Ready-made instructions a chat app can offer its user | `repair_violation` |

Messages are JSON-RPC, over one of two **transports**:

- **stdio**: the client starts the server as a child process and talks over
  its standard input and output. Simple and private; used by our agent, by
  Claude's desktop app and by the MCP Inspector.
- **Streamable HTTP**: the server runs on its own and clients connect by URL
  (`http://127.0.0.1:8765/mcp`). Needed when the server lives on another
  machine or serves many clients.

**Why bother, when the agent could import the functions?** Separation. The
server owns the browser and the data; the agent owns the reasoning. Each can
be tested, replaced or reused alone: in this phase the agent switches from
in-process scanning to MCP scanning without any change to the graph.

---

## 2. The server

```bash
python -m a11y_agent.mcp_server                      # stdio
python -m a11y_agent.mcp_server --http --port 8765   # HTTP
python scripts/try_mcp.py                            # a small client: lists everything and calls each tool
```

`src/a11y_agent/mcp_server.py` uses **FastMCP** from the official MCP Python
SDK. A tool is a decorated function; FastMCP builds its schema from the type
hints and docstring:

```python
@mcp.tool(annotations=READ_ONLY)
def contrast_fix(
    foreground: Annotated[str, Field(description="Text colour, e.g. '#767676' ...")],
    background: Annotated[str, Field(description="Background colour behind the text.")],
    target_ratio: Annotated[float, Field(description="Required ratio: 4.5 for normal text (AA), ...")] = 4.5,
) -> ContrastReport:
    """Compute the contrast ratio of two colours and suggest the closest colours that reach the target ratio.

    Use this instead of guessing colours: it applies the exact WCAG formula."""
```

Things worth copying into any tool server:

- **Descriptions are prompts.** The model reads the docstring and field
  descriptions to decide when and how to call a tool. "Use this instead of
  guessing colours" and "7 for normal text (AAA, rule color-contrast-enhanced)"
  are there for the model, not for people.
- **Output schemas.** Tools return Pydantic models (`ScanReport`,
  `FixVerdict`, `ContrastReport`), so clients receive typed, structured
  results (`structuredContent`) as well as a text copy.
- **Annotations** tell clients a tool only reads (`readOnlyHint`), and whether
  it reaches the outside world (`scan_url` does).
- **Lifespan.** One headless browser serves every call. It starts on the
  first scan (so `explain_rule` alone stays instant) and closes with the server.
- **Errors are results, not crashes.** An unknown rule, an unreadable colour
  or oversized HTML raises an exception inside the tool; FastMCP returns it
  as an error result with the message, e.g. `Unknown axe-core rule
  'image-alts'. Did you mean: image-alt, input-image-alt, svg-img-alt?`. A
  model can read that and try again.
- **stdout belongs to the protocol.** Over stdio, anything printed to
  standard output corrupts the messages, so the server only logs (to stderr,
  warnings only).
- **So does stdin** (found in Phase 4, on Windows only). One thread of a
  stdio server is always blocked reading the next message. On Windows a
  pending read blocks every other operation on that pipe, and numpy's DLL
  touches standard input when it loads. The first `search_wcag` call imported
  numpy, the import waited for the read, the read waited for the client, and
  the client waited for the answer: a deadlock that never happened on Linux.
  CI found it by having the hung server dump its own thread stacks (Python's
  `faulthandler`). The fix, `own_stdin()`: the protocol reads a private copy
  of the stdin handle, and file descriptor 0 points at an empty file, so
  nothing else can reach the pipe. Importing numpy at start-up would also
  have worked, but only for numpy; the general fix also covers onnxruntime,
  which loads later.

**Safety, because a server can be called by anything that connects to it:**

- `scan_html` loads HTML with the network blocked (as since Phase 0).
- `scan_url` accepts only public http(s) addresses: a host that resolves to
  this machine or the local network is refused, so the server cannot be used
  to probe internal services ("server-side request forgery"). Redirects are
  not re-checked yet; Phase 5 tightens network guardrails.
- Inputs over 2 million characters are refused.
- In HTTP mode the server listens on `127.0.0.1` only, and the SDK's
  DNS-rebinding protection is on.

---

## 3. The contrast tool: arithmetic belongs in code

Colour contrast is the most common violation in the benchmark (11 of 40 dev
items, 35 of 138 test items). Fixing it means finding a colour whose ratio
against the background reaches 4.5:1 or 7:1, using WCAG's relative-luminance
formula. A language model can only estimate that; `contrast.py` computes it:

- the formula, with hex, `rgb()`/`rgba()` and basic colour names, and
  transparency blended onto the background;
- for a failing pair, the closest passing text colour and background colour:
  keep the hue and saturation, binary-search the lightness until the ratio
  passes, then check the rounded hex value still passes.

For benchmark item accessguru-004 (`#005ea2` on `#f1f1f1`, 5.95:1, needs 7:1)
it suggests `#00538f` at 7.06:1. A test applies the suggestion to a page and
scans it with axe-core: the violation is gone. That test is the point: the
tool is checked against the same ground truth as everything else.

---

## 4. One judgement, two callers

`verify_fix` (for any MCP client) and the agent's `verify` node must agree on
what "fixed" means, so both call `verification.judge_fix`: target rule gone,
no rule newly failing, 90% of words and elements kept, with the problems
written as sentences the model can act on. Deleting an image, for example,
fails with "Your change removed content ...".

---

## 5. The agent as an MCP client

`src/a11y_agent/mcp_client.py` starts the server over stdio and keeps **one
session** (one server process) open for the whole run. It uses that session
in two ways:

**Deterministic steps go through MCP directly.** `McpScanner` has the same
`scan_html` method as the in-process `Scanner`, but calls the server's
`scan_html` tool and converts the structured result back into a
`ScanResult`. The graph's `scan` and `verify` nodes do not change; only the
object in the runtime context does:

```python
async with connect() as server:
    context = AgentContext(scanner=server.scanner, model=model, tools=server.model_tools())
```

A test runs the same scripted fix with both scanners and checks the results
are identical. That is the Phase 3 "done when" for the plumbing: same graph,
same answers, tools now behind a protocol.

**Model tools go through LangChain.** `langchain-mcp-adapters` turns each MCP
tool into a LangChain tool (`load_mcp_tools(session)`), with its schema, so it
can be bound to a chat model. The agent offers the model only `explain_rule`
and `contrast_fix`. Scanning and verifying stay with the graph, because those
decisions must not depend on the model.

**Least privilege for the child process.** The server is started with MCP's
minimal default environment plus the few variables it needs (browser path,
data folder). Your API key is not passed to a process that has no use for it.

---

## 6. Function calling inside `propose`

With tools, one attempt becomes a small loop (`ask_with_tools` in `agent.py`):

1. Send the prompt with the tool definitions. The model replies with either
   tool calls or its answer.
2. For each tool call, run the tool on the MCP server and append the result
   to the conversation (an error result is appended too, marked as an error).
3. Send the conversation again. Repeat.

**The final answer is a tool too.** Groq cannot enforce a JSON schema on the
reply and offer tools in the same request. So the answer becomes a tool,
`submit_fix`, whose parameters are the same `FixProposal` schema as in Phase
2. The loop ends when the model calls it; Pydantic validates the arguments.
LangChain's `with_structured_output(method="function_calling")` uses the same trick.

**Bounded, like every loop here.**
- `tool_choice="required"` makes the model call *something* every round.
- After 3 rounds, or when the next round would not fit the request limit,
  only `submit_fix` is offered and the model must call it
  (`tool_choice="submit_fix"`).

**Fallbacks, because providers differ:**

| If the provider or model... | then... |
|---|---|
| refuses `tool_choice="required"` | the client notices once and uses `"auto"` from then on |
| answers in plain text instead of calling `submit_fix` | the text is parsed as JSON edits |
| produces a malformed tool call (Groq: `tool_use_failed`) | the attempt fails with feedback and the next attempt tries again |

The trace shows what the model did, e.g.:

```
propose  [color-contrast-enhanced #1] 1 edit(s); tools: contrast_fix({'foreground': '#005ea2', ...}) -> {"ratio":5.95, ... "color":"#00538f" ...}: "color: #005ea2;" -> "color: #00538f;"
verify   [color-contrast-enhanced #1] passed -> accept
```

**Caching still works.** A tool conversation is cached call by call: each
request (messages, including earlier tool calls and results, plus the tool
definitions) is hashed, and the reply, including its tool calls, is stored.
Re-running an evaluation replays the whole loop without spending tokens; a
test checks this.

---

## 7. The graph did not change

`python -m a11y_agent.agent --graph` prints the same diagram as in Phase 2.
Phase 3 changed two things passed in through the runtime context (the
scanner, and the tool list) and one node's internals. That is the payoff of
keeping dependencies out of the state and the control flow in the graph.

---

## 8. Measuring it: method `agent_tools`

```bash
python -m a11y_agent.evaluate run --method agent_tools
python -m a11y_agent.evaluate report --save
```

`agent_tools` is the Phase 2 agent with the MCP server attached: its scans
run on the server, and the model may call `explain_rule` and `contrast_fix`.
The harness starts the server once for the whole run (the method is an async
context manager) and still scores every fix with **its own** scanner, so the
system being tested never grades itself.

Cost: the tool definitions add about 500 tokens to each request, and a round
with a tool call resends the conversation. Expect roughly 100,000–180,000
tokens for the dev set, so give it a day of quota to itself, after the
`agent` run.

What to look for: the contrast rows (`color-contrast`, `color-contrast-enhanced`).
If the tool helps, they should improve most, at the price of more tokens per item.

---

## 9. Using the server from other clients

**The MCP Inspector** (needs Node.js) is a web page for calling any MCP server
by hand:

```bash
npx @modelcontextprotocol/inspector .venv/Scripts/python.exe -m a11y_agent.mcp_server    # Windows
npx @modelcontextprotocol/inspector .venv/bin/python -m a11y_agent.mcp_server            # macOS / Linux
```

**Claude's desktop app** can use local MCP servers listed in
`claude_desktop_config.json` (Windows: `%APPDATA%\Claude\claude_desktop_config.json`;
macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`).
Add an entry with the full path to your virtual environment's Python, then
restart the app:

```json
{
  "mcpServers": {
    "accessibility": {
      "command": "C:\\Users\\<you>\\accessibility-repair-agent\\.venv\\Scripts\\python.exe",
      "args": ["-m", "a11y_agent.mcp_server"]
    }
  }
}
```

Then ask it to "scan this HTML for accessibility problems and fix them",
and it calls `scan_html`, `contrast_fix` and `verify_fix` itself. The
`repair_violation` prompt gives it a step-by-step recipe.

**Our own agent's CLI:**

```bash
python -m a11y_agent.agent examples/broken_page.html --tools
```

---

## 10. How it is tested

`tests/test_mcp.py` starts the real server as a separate process, the way
every client does, and uses scripted fake models:

- the server lists its 5 tools (with schemas and annotations), the resource
  and the prompt; each tool returns what it should; errors are readable;
  private URLs are refused; deleting content fails `verify_fix`;
- `McpScanner` returns the same scan as the in-process scanner;
- the agent through MCP produces exactly the same result as in-process;
- a model that calls `contrast_fix` and submits the suggested colour gets a
  fix that axe-core accepts, in one attempt and two model calls;
- tool errors reach the model; tool rounds stop at 3 and then `submit_fix` is
  required; a plain-text answer is accepted; a provider refusing `"required"`
  falls back to `"auto"`;
- `agent_tools` works inside the evaluation harness, and a second run is
  answered entirely from the cache;
- the same server works over streamable HTTP;
- over stdio the server keeps standard input to itself (see section 2).

`tests/test_contrast.py` checks the formula against known values and that
every suggestion really passes, including with axe-core itself.

---

## 11. Running it

```bash
git pull
pip install -e ".[dev]"
python scripts/check_setup.py     # new line: "MCP server  OK  5 tools over stdio: ..."
python scripts/try_mcp.py         # see the protocol in action, no API key needed
```

Order of evaluation runs on the free tier (one per day): `agent` (Phase 2's
number), then `agent_tools`, then the rest of `accessguru`.

---

## What you can now explain

- **MCP's parts**: host, client, server; tools, resources, prompts; stdio versus HTTP.
- **Designing a tool for a model**: descriptions that tell the model when to
  use it, typed input and output schemas, read-only annotations, errors as results.
- **Why arithmetic is a tool**: deterministic code for what models only approximate,
  checked against the same ground truth.
- **Server lifecycle**: a lifespan that owns expensive resources (one browser).
- **Two ways an agent uses tools**: deterministic calls from the graph versus
  calls the model chooses.
- **Function calling**: tool definitions, `tool_choice`, the tool loop, results
  sent back as tool messages, and the final answer as a tool.
- **Bounding and budgeting a tool loop**, and caching it.
- **Security basics for tool servers**: SSRF, input limits, local binding, least privilege.

## Next: Phase 4

Retrieval over the official WCAG documents: chunk the 581 pages loaded in
Phase 0, index them (local embeddings plus BM25 keywords, filtered to the
violated rule's success criteria), measure retrieval on its own, then serve
it as an MCP tool (`search_wcag`) so the model can look up the W3C's own
techniques and failure examples. (Built: [docs/04-retrieval.md](04-retrieval.md).)
