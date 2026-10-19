# Phase 1: Evaluation — what we built and why

**Goal of this phase:** before building the agent, build the thing that tells
us whether the agent is any good: a fixed set of test cases, a harness that
runs any repair method over them the same way, and two baselines to beat.

**The one idea to take away:** an agent change is only an improvement if a
number you trust goes up. That number needs a fixed dataset, a fixed scorer,
and simpler methods to compare against. All three are built here, before a
single line of agent code.

---

## 1. Why evaluation comes before the agent

Agent projects without an evaluation usually end as a demo that works on the
one example its author tried. With an evaluation in place, every later phase
(the LangGraph loop, MCP tools, retrieval, guardrails) gets the same question:
*did the dev-set score go up, and at what cost?* Phase 2 will plug the agent
into this harness as one more method; nothing else changes.

Two words used throughout:

- **Tests** (`pytest`) check the *code* is correct: pass or fail.
- **Evaluations** measure how *good* a method's answers are: a score.

---

## 2. The benchmark split

```bash
python -m a11y_agent.benchmark stats
```

```
accessguru: 250 rows, 178 reproduce with axe-core 4.12.1 -> dev 40, test 138
  dev    40 items, 15 rules, page-level 14; Layout 12, Syntax 28
  test  138 items, 41 rules, page-level 46; Layout 38, Syntax 100
  not reproducible: duplicate-id 20, landmark-unique 14, color-contrast-enhanced 5, ...
access: 171 rows, 100 reproduce with axe-core 4.12.1 -> dev 0, test 100
```

`src/a11y_agent/benchmark.py` made these numbers in three steps.

**Keep only what a machine can check.** Every row was scanned once. A row is
kept only if the rule it claims is actually violated in its stored HTML;
otherwise no fix could ever be verified. 178 of AccessGuru's 250 syntax and
layout rows pass, and 100 of ACCESS's 171. The rest mostly need the whole
page (`landmark-unique` must see the other landmarks) or use a retired rule
(`duplicate-id`). The 55 semantic rows (e.g. "this alt text is meaningless")
are left out because a rule engine cannot judge them.

**Split by rule.** The 178 rows are divided into a **dev set of 40** and a
**test set of 138**. The split is *stratified*: each rule gets dev places in
proportion to how common it is (`color-contrast-enhanced` has 33 of the 178 rows,
so it gets 8 of the 40 dev places), and rare rules land in dev or test at random. Both
sets therefore contain the same mix of violation types.

**Fix it once and commit it.** The split uses a fixed random seed and is saved
as `splits/accessguru.json` and `splits/access.json`: row ids, the checksum of
the CSV they refer to, and the axe-core version. Every run on every machine
uses exactly these rows. If the downloaded CSV ever differs, loading the split
refuses to run rather than silently scoring different rows.

**The rule that makes the final number honest:** build and tune only on dev.
Run test once, at the end. If you tune prompts until the test score looks
good, the test score stops measuring anything.

**Page-level rules.** 14 of the 40 dev items are `region`, `landmark-one-main`
or `page-has-heading-one`. They fire on any snippet not wrapped in a full page
and are easy to "fix" (wrap it in `<main>`). The report shows every score
twice: all items, and element-level items only.

---

## 3. The methods being compared

All methods sit behind one small interface (`methods.py`): given an item,
return a proposed replacement for its HTML. The harness does the scanning and
scoring, identically for everyone.

| Method | What it does | Model calls per item |
|---|---|---|
| `noop` | Returns the HTML unchanged | 0 |
| `zero_shot` | One message: the rule's name and axe-core's help text, "return the corrected HTML in a code block" | 1 |
| `accessguru` | The published AccessGuru method: expert persona, then a 4-turn conversation (explain, correct, critique, confirm). If the scanner says the fix failed, the conversation runs again with a stricter first turn. The best of {original, pass 1, pass 2} is kept | 4 to 8 |

**`noop` is a test of the harness, not a baseline.** Doing nothing must score
exactly 0.00. If it ever scores anything else, the scoring is broken.

**`accessguru` is ported faithfully.** Its prompt texts are copied from the
authors' code (MIT licence), not paraphrased, so the number measures their
method. Two deliberate differences, both documented in `prompts.py`:

- their 5th turn (asking the model for a confidence score) is skipped: it does
  not change the code and costs a whole extra call;
- the fixed HTML is pulled out of the reply by a fixed rule (text between the
  `###albidaya###` markers, else the last code block). The authors used a
  second model plus manual review for this. A fixed rule is reproducible and
  cannot quietly rescue a bad answer.

**Our `zero_shot` is not the paper's zero-shot.** The paper's version only asks
"Is the following HTML code accessible?" without naming the violation. Ours
names the rule and gives its help text, which is what a developer would
actually type into a chat assistant. It is a stronger, fairer baseline.

---

## 4. What the harness does for every item

`evaluate.py`, for each item, whatever the method:

1. **scan** the original snippet (the "before" picture);
2. **ask the method** for a fix;
3. **scan** the fix (the "after" picture);
4. **score** it with the AccessGuru metric: target gone and nothing new broken
   scores 0; anything else keeps a penalty (see `docs/00-setup.md`, step 3);
5. **check how much of the original survived**: share of its words and of its
   elements still present in the fix.

One line per item goes to `runs/<dataset>-<split>/<method>__<model>/results.jsonl`
(scores, status, tokens, the fixed HTML, the full reply), and the totals to
`summary.json` beside it.

**Statuses** say why an item ended as it did:

| Status | Meaning | Scored as |
|---|---|---|
| `ok` | The method returned HTML | Whatever the scan says |
| `no_html` | The reply had no code block or markers | Not fixed |
| `truncated` | The reply hit the output limit before finishing | Not fixed |
| `too_large` | The request exceeds the free tier's per-request limit, so it was never sent | Not fixed (and see §7) |
| `no_improvement` | AccessGuru kept the original: neither pass beat it | Not fixed |

---

## 5. Calling a model on a free tier

Groq's free tier gives gpt-oss-120b **30 requests and 8,000 tokens per minute,
and 200,000 tokens per day**. `model_client.py` is built around those limits:

- **Response cache.** Every request (model, settings, messages) is hashed and
  its reply stored in `runs/cache.sqlite`. Re-running costs nothing and returns
  the same answers, so you can change the report or fix a scoring bug without
  spending quota. The report counts cost only for replies that were not cached.
- **Pacing.** Before each call, a rate limiter checks the last 60 seconds of
  token and request use and waits if the call would not fit
  (`RATE_LIMIT_TPM=7000`, `RATE_LIMIT_RPM=25` in `.env`, a little under the
  limits). Hitting a limit anyway is handled with waits and retries.
- **Requests that can never fit.** One request may not exceed the per-minute
  limit. Anything planned above `MAX_REQUEST_TOKENS=8000` (prompt plus room for
  the answer) is not sent; the item is marked `too_large`.
- **Stopping cleanly.** When the daily quota runs out, or the key is rejected,
  the run stops and says so. Finished items are already saved; running the same
  command again later skips them and continues. Items that failed for a
  temporary reason (network) are not saved, so they are retried next time.

**Output budget.** Each call asks for at most `2 × (snippet tokens) + 1024`
output tokens: room for a corrected copy of the snippet plus the model's
reasoning. gpt-oss thinks before it answers, and thinking counts as output.

---

## 6. The loophole the metric leaves open

The metric asks one question: is the violation gone? Deleting the offending
element makes it go. So does returning only the one line that changed, which
AccessGuru's own re-prompt asks for ("Return only the lines you changed").

The harness therefore measures **how much content each fix kept** (share of
original words and elements still present) and counts fixes that kept less
than 90% as **dropped content**. They are not removed from the score, so our
numbers stay comparable with the paper, but they are reported next to it.
Phase 5's guardrails will reject them outright; this column is how we will
show that the guardrails matter.

---

## 7. Reading the report

```bash
python -m a11y_agent.evaluate report
```

| Column | Meaning |
|---|---|
| Score decrease | The paper's metric over all items: 1.00 = everything fixed, nothing broken |
| Element-level only | The same, without the three page-level rules |
| Resolved | Items fixed with nothing new broken |
| Broke something | Items where the fix introduced a different violation |
| Dropped content | Fixes that kept under 90% of the original words or elements |
| Too large | Items never sent because of the per-request limit |
| Tokens per item | Average input + output tokens (including cached replies) |
| Paid cost | What the non-cached calls would have cost on Groq's paid plan |

**A second table appears when some items were `too_large`.** It re-scores
every method on only the items *all* methods could send. Counting `too_large`
as failures would penalise a method for our quota rather than its quality,
and AccessGuru's growing conversation hits the limit far more often (below).

---

## 8. Budget: what a run costs

Estimated from the actual prompts and snippets (about 4 characters per token,
replies about the snippet's size plus 400 tokens):

| Split | Method | Tokens | Too large on the free tier | Free-tier time | Paid (Groq) |
|---|---|---|---|---|---|
| dev (40) | zero_shot | ~42K | 3 | one sitting | under $0.02 |
| dev (40) | accessguru | ~350K | ~9 | 2 days | about $0.10 |
| test (138) | zero_shot | ~156K | 10 | one day | about $0.05 |
| test (138) | accessguru | ~1.35M | ~29 | 7 days | about $0.30 |

AccessGuru costs about 8 times as much as zero-shot per item, because each of
its four turns resends the whole conversation so far. That ratio is itself a
result worth reporting: does the extra cost buy a better score?

Practical plan: run zero-shot on dev today; run AccessGuru on dev over two days
(the run stops at the daily limit; start it again the next day). For the final
test-set runs, either spread them out, or add a few dollars of credit to Groq
and set `RATE_LIMIT_TPM=0`, `RATE_LIMIT_RPM=0`, `MAX_REQUEST_TOKENS=60000`
(then nothing is `too_large` either).

---

## 9. Running it

**On your laptop:**

```bash
python -m a11y_agent.evaluate run --method noop                 # ~20 s, checks the harness
python -m a11y_agent.evaluate run --method zero_shot --limit 3  # try 3 items first
python -m a11y_agent.evaluate run --method zero_shot            # the whole dev split
python -m a11y_agent.evaluate run --method accessguru
python -m a11y_agent.evaluate report --save                     # writes results/accessguru-dev/
```

Then commit the `results/` folder so the numbers are in the repository.
`runs/` (full replies, fixed HTML, cache) stays on your machine; open
`results.jsonl` in it for error analysis.

**On GitHub instead (no laptop needed):** add your Groq key once as a
repository secret: *Settings → Secrets and variables → Actions → New repository
secret*, name `GROQ_API_KEY`. Secrets are encrypted and never shown in logs or
to visitors; this is the safe way to give a key to a public repository. Then
open the *Actions* tab → **evaluate** → *Run workflow*, choose the method and
split. The headline numbers appear on the run's page; the full output is a
downloadable artifact. Progress and the reply cache are kept between runs, so
starting it again after a quota stop continues where it left off.

---

## 10. How the harness itself is tested

`tests/test_evaluate.py` runs the real harness, real scanner and real prompts
with **a scripted fake model** (`tests/fakes.py`): a LangChain chat model that
reads the snippet from the prompt and applies a known fix, a deliberately
destructive one, or none. No key, no network, no cost, and the expected
scores are known exactly. Among other things the tests check that:

- doing nothing scores 0.00, and two correct fixes score 1.00;
- a second run is served entirely from the cache and makes no model calls;
- an interrupted run resumes without redoing finished items;
- a reply without code counts as "not fixed";
- deleting the content is fixed by the metric but flagged as dropped content;
- AccessGuru re-prompts after a failed first pass, stops after a good one, and
  never returns something worse than the input;
- a daily-quota error stops the run with nothing half-written, and the next
  run completes it; a rejected key stops the run immediately;
- the split files are consistent, and (on Linux and Windows, in CI) every
  split row still reproduces.

---

## What you can now explain

- **Why the evaluation was built before the agent**, and the difference between
  tests and evaluations.
- **Why there is a dev set and a test set**, why the split is stratified, and
  why it is committed rather than re-drawn.
- **How to compare methods fairly:** same items, same scorer, same model,
  ported prompts rather than paraphrases, and a like-for-like table when quota
  limits stop some requests.
- **How to work within API limits:** caching, pacing, resumable runs, and
  telling permanent failures from temporary ones.
- **Where the metric can be gamed** (deleting content) and how it is measured.
- **What extra reasoning costs:** AccessGuru spends about 8 times the tokens
  of a single prompt.

## Next: Phase 2

The LangGraph agent becomes a fourth method in `methods.py`: scan, retrieve
the rule's help, propose a fix, re-scan, and retry with the scanner's feedback
until fixed or out of attempts. The harness, split and report stay exactly as
they are, so its number is directly comparable with the two baselines.
