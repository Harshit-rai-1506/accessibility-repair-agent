# Phase 6: The final runs — how they are done and how to read them

**Goal of this phase:** answer the project's question on data nobody tuned
on. Does the agent fix more than the baselines? Safely? At what cost? And
where does it fail?

**The one idea to take away:** decide everything before you look. The code
is frozen, the test set is run once, and how the numbers will be compared
is fixed in advance. Otherwise the result partly measures our own choices
rather than the method.

---

## 1. What is compared

| | Method | What it isolates |
|---|---|---|
| floor | `noop` (returns the page unchanged) | that the harness scores "nothing" as 0 |
| A | `zero_shot`: one request, "fix this" | the model alone |
| B | `accessguru`: the published 4-step prompting with one re-prompt | the state of the art, on our model |
| C | `agent`: the LangGraph loop (propose, verify, retry) | the value of checking and retrying |
| C+ | `agent_tools`: the same agent as an MCP client, with model tools | the value of tools (contrast maths, rule explanations) |
| D | `agent_rag`: C+ with WCAG retrieval | the value of WCAG knowledge |
| E | `agent_guarded`: C+ with guardrails | the cost of safety |

All on the same model (Mistral Small 4, open weights) and the same items.
Two test sets:

- **AccessGuru test**: 138 items, 41 rules, split once (seed 20261020) and
  never looked at during development;
- **ACCESS**: 100 items from a second benchmark (26 rules), never used
  before. It shows whether the results carry over to different pages.

## 2. The freeze

Every decision was made on the 40 dev items: prompts, attempt limits, which
guardrails reject and which only flag. The last round came from the Phase 5
dev runs (docs/05-guardrails.md §10):

- the prompt says where new elements go (after `<body>`, never before `<html>`);
- the edit format refuses anchors that would put content outside the page,
  and mistyped anchors such as `<<start>`;
- the guard's feedback says how to fix broken markup;
- retrieval returns one passage per page.

Then the code was frozen. The commit is tagged **`test-freeze`** on GitHub,
and every result row now records the commit that produced it (the `code`
field). A run that was resumed after a code change therefore shows up in the
report as using two versions, instead of silently mixing them.

## 3. How the numbers are read

**The main score** is the AccessGuru violation score decrease, counting a fix
that deleted content as no fix ("deletions count as failures"). On the dev
set the plain metric made AccessGuru look best (0.75) only because 11 of its
40 fixes deleted the broken element.

**A 95% confidence interval for every score.** Imagine the test set had been
a different 138 pages from the same benchmark: how different could the score
have come out? The *bootstrap* answers that by resampling the items with
replacement 10,000 times and recomputing the score each time. The middle 95%
of those scores is the interval. On 40 dev items the intervals were about
±0.14; on 138 they should be roughly half as wide.

**Paired comparisons with `agent_tools`.** Every method is compared with the
best dev method on the same items:

- the *difference* in score, with its own 95% interval from a *paired*
  bootstrap (both methods resampled on the same items each time, so
  differences between easy and hard pages cancel out). If the interval does
  not include 0, the difference is unlikely to be chance at this sample size;
- the items one method resolved and the other did not, with an exact *sign
  test*: if both methods were equally good, how often would a split this
  uneven happen? On the dev set, retrieval's 2-to-7 split happens about 18%
  of the time by chance. Not convincing on its own, which is why the test
  set matters.

Five comparisons are made, so one p-value near 0.05 can still be luck. Read
the intervals, not just the p-values.

**Element-level and page-level separately.** Page-level rules (`region`,
`landmark-one-main`, `page-has-heading-one`) are easy to "fix" on a snippet
(wrap it in `<main>`), so they are reported apart.

**Why items failed.** `python -m a11y_agent.evaluate failures` sorts every
unresolved item by the reason in its trace:

- the edits did not match the page;
- the target still fails;
- the target was fixed but another rule broke;
- every attempt was rejected by the guardrails;
- the request was too large;
- the reply was unusable;
- the page was blocked at the input screen;
- some of the failing elements were fixed.

It then lists 20 failures with their last step, for reading by hand.

**Cost and speed.** Per method: model calls, tokens, model time and wall time
per item (median and 90th percentile), the share of replies that came from
the cache, and what 100 pages would cost on Mistral's paid plan. Timings only
mean something on a first run. `agent_guarded` reuses `agent_tools`' cached
replies, so its own model time is small. The cache column shows when that is
the case.

**The guardrails table:** pages blocked at the input, attempts rejected by
reason, changes flagged for review by kind.

**Real pages.** `scripts/public_pages.py` runs the guarded agent on public
pages and writes a before/after report for each one. That covers the score,
what happened to every rule, the review flags, the cost, and the start of
the diff.

---

## 4. Running it

About 6 to 8 hours of runs in all, mostly unattended, and roughly 20 million
tokens. Mistral's free tier allows 1 billion a month. Every command resumes
where it stopped, so it can be split over several sittings.

```bash
git pull
pip install -e ".[dev]"
python scripts/check_setup.py
```

**Step 1: confirm the last fixes on dev** (about 1 hour; the four agent
methods changed, so `--fresh` re-runs them; unchanged replies come from the
cache):

```bash
python -m a11y_agent.evaluate run --method agent agent_tools agent_rag agent_guarded --fresh
python -m a11y_agent.evaluate compare
```

Expect `agent_guarded` to be much closer to `agent_tools` than the 0.14 gap
before. If it is not, stop and send me the output before Step 2.

**Step 2: the AccessGuru test set** (about 4 to 5 hours). `agent_tools` runs
before `agent_guarded` so the guarded run can reuse its replies:

```bash
python -m a11y_agent.evaluate run --split test --method noop zero_shot agent agent_tools agent_guarded agent_rag accessguru
```

**Step 3: the ACCESS benchmark** (about 2 hours; the methods that answer the
main questions):

```bash
python -m a11y_agent.evaluate run --dataset access --split test --method noop zero_shot agent_tools agent_guarded accessguru
```

**Step 4: reports**

```bash
python -m a11y_agent.evaluate report --save
python -m a11y_agent.evaluate report --split test --save
python -m a11y_agent.evaluate report --dataset access --split test --save
python -m a11y_agent.evaluate compare --split test --save
python -m a11y_agent.evaluate compare --dataset access --split test --save
python -m a11y_agent.evaluate failures --split test --method agent_guarded --save
python -m a11y_agent.evaluate failures --split test --method agent_tools --save
```

**Step 5: a few real pages.** Choose 3 to 5 public pages you are allowed to
test, for example your university's or your city's home page:

```bash
python scripts/public_pages.py https://first.example https://second.example
```

**Step 6: commit and push**

```bash
git add results
git commit -m "Phase 6 results"
git push
```

`results/` then holds everything the write-up needs: summaries, per-item
CSVs with traces, the comparison with intervals, the failure tables, the
guardrails tables and the public-page reports. The test items' HTML stays in
`runs/` on your laptop, as before.

## 5. What comes after the runs

The results section of this guide and of the README are written from those
files:

- the final tables with intervals;
- what each component (the loop, tools, retrieval, guardrails) added or cost;
- the failure analysis;
- cost and speed;
- the public pages;
- the limitations.

---

## What you can now explain

- **Why a held-out test set** and a freeze: tuning on the test set makes its
  numbers meaningless.
- **Recording code versions** with results, and why resumable runs need it.
- **Bootstrap confidence intervals**, paired differences and the sign test,
  in words.
- **Multiple comparisons**: why one p-value near 0.05 is weak evidence.
- **Error analysis by category**, from traces.
- **Reporting cost and latency honestly**, including what caching does to
  timings.
