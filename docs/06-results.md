# Phase 6: The final runs: how they were done, and what they showed

**Goal of this phase:** answer the project's question on data nobody tuned
on. Does the agent fix more than the baselines? Safely? At what cost? And
where does it fail?

**The one idea to take away:** decide everything before you look. The code
is frozen, the test set is run once, and how the numbers will be compared
is fixed in advance. Otherwise the result partly measures our own choices
rather than the method.

**The short answer (§5):** yes. On 238 held-out items from two benchmarks,
the agent with tools and guardrails removes about three quarters of the
violation score (0.78 and 0.77), with no unsafe fix. The published
AccessGuru method, on the same model, safely removes 0.29 and 0.42.

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

## 2. Improve on dev, then freeze

Every decision is made on the 40 dev items: prompts, attempt limits, which
guardrails reject and which only flag. The last round came from the Phase 5
dev runs (docs/05-guardrails.md §10). The guarded agent lost 7 page-level
items, because every way the model wrapped a page in `<main>` put it before
`<html>` (or left a mistyped `<</end>>`). axe-core accepts those. So:

- `<<start>>` and `<<end>>` now insert inside the page's content, so the usual
  wrap is valid HTML by construction; mistyped anchors are removed;
- the prompt is word for word the Phase 5 prompt, so the dev re-run reuses the
  cached replies and measures this change alone;
- the guard's feedback says how to fix broken markup;
- retrieval returns one passage per page.

**The gate.** The test set is only touched once the dev re-run shows the
guarded agent close to `agent_tools` on the safe score. If it does not, the
next round of fixes is again made on dev. Then the code is frozen: the commit
is tagged **`test-freeze`**, and every result row records the commit that
produced it (the `code` field). A run that was resumed after a code change
therefore shows up in the report as using two versions, instead of silently
mixing them.

**Where things run.** The evaluations run on your laptop, as they always have,
because they need your Mistral key. GitHub's CI only runs the automated tests
(does the code work on Linux and Windows); while the repository is private it
needs Actions minutes, so `python -m pytest` on your laptop does that job.

## 3. How the numbers are read

**The main score is the safe score**: the AccessGuru violation score decrease,
counting only fixes the guardrails accept. A fix that removed or hid content,
added a script or a link, or broke the markup counts as no fix, whichever
method made it. Two dev-set lessons are behind this:

- the plain metric made AccessGuru look best (0.75) only because 11 of its 40
  fixes deleted the broken element;
- `agent_tools`' 0.78 included 7 fixes that were broken HTML.

The plain score and "deletions count as failures" are still in every table.

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

**Step 0: get the code and check it on your machine**

```bash
git pull
pip install -e ".[dev]"
python scripts/check_setup.py
python -m pytest
```

`python -m pytest` should end with "253 passed, 2 skipped" or similar, with
no failures. (While the repository is private, GitHub's CI needs Actions
minutes on your account, so this run is the check on Windows.)

**Step 1: re-run the agent methods on dev** (about half an hour: the prompts
are unchanged, so most replies come from your cache; only `agent_rag` asks
the model afresh, because its passages changed). `--fresh`
replaces the earlier results.

```bash
python -m a11y_agent.evaluate run --method agent agent_tools agent_guarded agent_rag --fresh
python -m a11y_agent.evaluate report --save
python -m a11y_agent.evaluate compare --save
```

The table now has a "Safe fixes only" column; `compare` uses it.

Expect, from replaying the model's recorded attempts:
- `agent_guarded` should get back at least 6 of the 7 lost items;
- `agent_tools` should keep its score with valid HTML, so its safe score
  should match its plain score.

Commit `results/` and send me the output. Only then: Step 2.

*Done 28 Oct: passed (docs/05-guardrails.md §10). The guarded agent matches
`agent_tools` on every dev item, 0.76 on the safe score.*

**The freeze** (after I have confirmed Step 1):

```bash
git tag -a test-freeze -m "Code frozen for the test-set runs"
git push origin test-freeze
```

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
python -m a11y_agent.evaluate failures --dataset access --split test --method agent_guarded --save
python -m a11y_agent.evaluate failures --dataset access --split test --method agent_tools --save
```

*Steps 2 to 4 done 28 Oct (commit `fec5b70`, every row from code `1075c0e`).
After them, `failures` was corrected to list a fix that passes the scanner but
fails a guardrail check as a failure ("fixed for the scanner, but fails the
guardrail checks"), as the safe score does. Before, `agent_tools`' 11 such
fixes were missing from its list. The four `failures` commands above only
read saved results (no model calls, a few seconds), so run them once more
with the current code.*

**Step 5: real public pages.** The home pages to test are listed in
`scripts/public_pages.txt`: 12 Indian sites, 5 government, 3 e-commerce, 2
education and 2 hospitals. Edit the list freely: one address per line, then
a category, and `#` switches a line off.

```bash
python scripts/public_pages.py                       # every page in the list
python scripts/public_pages.py https://some.site/    # or just these
```

What it does for each page:

- loads it once in Chromium, as a visitor's browser would, and gives a slow
  site up to a minute;
- skips it, saying why, if the site answers with an error or a bot check;
- runs the guarded agent on the snapshot, fixing up to 15 rules, most severe
  first.

Budget roughly 3 to 10 minutes and a few hundred thousand tokens per page,
so 1 to 2 hours for the list. A page that is done is not redone when you run
the command again, and one that failed to load is tried again, so it can be
split over sittings. Add `--fresh` to redo everything.

Results go to `results/public-pages/`:

- `README.md`: one table over all pages;
- `<site>.md`: the before/after for each page;
- `<site>.json`: the numbers.

The snapshots and the fixed pages go to `runs/public/`. Only your local copy
is changed; nothing is sent to the sites.

*Not run yet. §5.8 is filled in from `results/public-pages/README.md` once it
is.*

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

## 5. The results

Run 28 Oct 2026 on your laptop, with Mistral Small 4 (`mistral-small-2603`),
one run per method. Every row was produced by the frozen code (`1075c0e`,
tag `test-freeze`). The source files are in `results/accessguru-test/` and
`results/access-test/`: `comparison.md` (intervals, paired tests, cost),
`README.md` (all score variants, guardrails), `items__*.csv` (every item,
with the agent's trace) and `failures__*.md`.

### 5.1 The answer in one table

Safe score, with its 95% interval:

| Method | AccessGuru test (138 items) | ACCESS (100 items) | Dev (40), for reference |
|---|---|---|---|
| `zero_shot` | 0.47 (0.38–0.57) | 0.62 (0.51–0.72) | 0.56 |
| `accessguru` (the published method) | 0.29 (0.21–0.37) | 0.42 (0.32–0.52) | 0.37 |
| `agent` (the loop) | 0.69 (0.61–0.76) | – | 0.61 |
| `agent_tools` | 0.75 (0.68–0.82) | 0.77 (0.69–0.85) | 0.76 |
| `agent_rag` | 0.73 (0.66–0.80) | – | 0.68 |
| **`agent_guarded`** | **0.78 (0.70–0.84)** | **0.77 (0.69–0.85)** | 0.76 |

- **The guarded agent is the best method on both held-out sets.** It removes
  about three quarters of the violation score, and none of its fixes on the
  238 test items deleted, hid or broke anything.
- **The dev results carried over.** The order of the methods is the same on
  all three sets, and the guarded agent's score barely moved: 0.76 on dev,
  0.78 and 0.77 on the test sets. Tuning on 40 dev items did not overfit
  them.
- **ACCESS confirms it on pages nobody had looked at.** The agent's score is
  the same there. Zero-shot does better on ACCESS (0.62) than on AccessGuru
  (0.47). ACCESS snippets are shorter (zero-shot used 727 tokens per item
  there, against 2,056 on AccessGuru), so a one-shot rewrite goes wrong less
  often.

### 5.2 Plain score against safe score

| Method | Plain score | Deletions count as failures | Safe score | Fixes that deleted, hid or broke something |
|---|---|---|---|---|
| *AccessGuru test* | | | | |
| `zero_shot` | 0.55 | 0.47 | 0.47 | 24 |
| `accessguru` | 0.75 | 0.36 | **0.29** | **68** |
| `agent` | 0.73 | 0.73 | 0.69 | 8 |
| `agent_tools` | **0.81** | 0.81 | 0.75 | 11 |
| `agent_rag` | 0.77 | 0.77 | 0.73 | 6 |
| `agent_guarded` | 0.78 | 0.78 | **0.78** | **0** |
| *ACCESS* | | | | |
| `zero_shot` | 0.67 | 0.64 | 0.62 | 6 |
| `accessguru` | **0.81** | 0.51 | 0.42 | 43 |
| `agent_tools` | **0.81** | 0.81 | 0.77 | 5 |
| `agent_guarded` | 0.77 | 0.77 | **0.77** | **0** |

The last column counts items whose final page removed more than a tenth of
the content, or failed a guardrail check.

**On the plain score, the published method looks as good as the agent.**
It scores 0.75 against 0.78 on AccessGuru test, and 0.81 against 0.77 on
ACCESS. The difference is in *how* the fixes are made:

- AccessGuru's conversation rewrites the whole snippet. In about half the
  items (68 of 138) the rewrite drops, hides or breaks something, and
  axe-core then has less to complain about. The guardrail checks found, on
  43 of those items (one item can have several): removed content 27, added
  URLs 12, broken markup 8, hidden content 2 and added scripts 2.
- The agent can only make small edits, and every edit is checked for lost
  content.

Even with the most lenient count that still means something ("deletions
count as failures"), the published method scores 0.36 and 0.51, against
0.78 and 0.77.

**Not all unsafe fixes are equally bad.** Every unsafe fix the agent
variants made, except one, is *invalid HTML*. Examples are a second `<html>`
tag or a `<main>` that is never closed. Browsers repair that kind of thing
when they display the page, so a user loses nothing, but a developer would
not merge it and it can display differently. Deletions are worse: a blind
user gets nothing from an image that is no longer there. The safe score
treats both as no fix. That is the strict choice, and it is the one fixed in
advance (§3). Under the plain score, `agent_tools` would come first on
AccessGuru test (0.81). This is said here rather than hidden.

### 5.3 What each part added (AccessGuru test, paired)

| Change | Difference in safe score | 95% interval | Items won / lost | Sign test p |
|---|---|---|---|---|
| `zero_shot` → `agent` (verify and retry) | **+0.21** | +0.11 to +0.31 | 39 / 12 | < 0.001 |
| `agent` → `agent_tools` (MCP tools) | +0.06 | −0.02 to +0.14 | 24 / 16 | 0.27 |
| `agent_tools` → `agent_rag` (WCAG retrieval) | −0.02 | −0.08 to +0.05 | 12 / 15 | 0.70 |
| `agent_tools` → `agent_guarded` (guardrails) | **+0.03** | +0.01 to +0.05 | 4 / 0 | 0.125 |
| `accessguru` → `agent_guarded` | **+0.49** | +0.39 to +0.58 | 68 / 4 | < 0.001 |
| `zero_shot` → `agent_guarded` | **+0.30** | +0.20 to +0.41 | 49 / 10 | < 0.001 |

On ACCESS:

- `accessguru` → `agent_guarded`: +0.35 (+0.24 to +0.47), 41 / 5;
- `zero_shot` → `agent_guarded`: +0.15 (+0.05 to +0.26), 23 / 5, p 0.001;
- `agent_tools` → `agent_guarded`: 0.00. They resolved the same 75 items.

What each part contributed:

- **The loop is the biggest single gain.** Here the scanner, not the model,
  says whether a fix worked, and the exact remaining failure goes into the
  next attempt.
- **The tools' gain is real where they apply, but not proven overall.** On
  the 25 items of `color-contrast-enhanced` (the AAA level, 7:1), the agent
  without tools fixed 19 and the agent with tools fixed all 25.
  `contrast_fix` computes the nearest colours that pass, instead of the model
  guessing. Zero-shot fixed 4 of them. On plain `color-contrast` (AA, 4.5:1)
  the tools did not help: zero-shot and every agent variant fixed 5 of 10,
  and the published method fixed 2 (§5.5).
- **Retrieval did nothing measurable, as on dev.** It scored +0.02 on
  element-level rules and −0.08 on page-level ones, where the longer prompt
  seems to distract. The failures are mechanical (a mistyped edit, a second
  `<html>`), not a lack of WCAG knowledge (docs/04-retrieval.md §11).
- **The guardrails cost nothing and gained a little.** The 4 items they won
  came from rejecting a bad attempt:
  - on 118 and 194, `agent_tools`' accepted fix was invalid HTML, and the
    guarded agent's next attempt was valid;
  - on 149 and 150, `agent_tools` failed outright. After the rejection ("a
    second `<html>`… change the existing `<html>` instead"), the model edited
    the existing tag and passed.

  They never cost an item. The sign test cannot reach 0.05 on 4 disagreements
  (4 out of 4 is p = 0.125, its smallest value), so the interval is the
  evidence: the difference is small, positive, and consistent across
  resamples.

Seven comparisons are listed. The three large ones (against `zero_shot` and
`accessguru`, and the loop) would survive any correction for multiple
comparisons. The small ones should be read as "no evidence of a difference",
or for the guardrails "no cost".

### 5.4 Element-level and page-level rules

Safe score, with resolved items in brackets:

| Method | AccessGuru test: element (92) | AccessGuru test: page (46) | ACCESS: element (71) | ACCESS: page (29) |
|---|---|---|---|---|
| `zero_shot` | 0.52 (47) | 0.33 (18) | 0.67 (42) | 0.46 (15) |
| `accessguru` | 0.28 (26) | 0.30 (14) | 0.45 (30) | 0.31 (9) |
| `agent` | 0.73 (66) | 0.57 (26) | – | – |
| `agent_tools` | 0.82 (75) | 0.54 (25) | 0.79 (54) | 0.72 (21) |
| `agent_rag` | 0.84 (76) | 0.46 (21) | – | – |
| `agent_guarded` | **0.84 (76)** | **0.61 (28)** | **0.79 (54)** | **0.72 (21)** |

Page-level rules (`region`, `landmark-one-main`, `page-has-heading-one`) are
a third of the AccessGuru test items but more than half of the guarded
agent's failures.

### 5.5 Why the guarded agent failed (AccessGuru test, 34 items)

From `failures__agent_guarded__…md`:

| Why | Items |
|---|---|
| the edits did not match the page | 11 |
| fixed the target but broke another rule | 10 |
| the target still fails | 7 |
| every attempt rejected by the guardrails | 6 |

By rule:

- page-has-heading-one: 8 of 13;
- region: 6 of 20;
- color-contrast: 5 of 10;
- landmark-one-main: 4 of 13;
- link-name: 3 of 14;
- 8 rules with a single test item each, all failed (aria-required-parent,
  aria-required-children, aria-allowed-role, aria-text, aria-tooltip-name,
  aria-treeitem-name, aria-progressbar-name, meta-refresh).

28 of the 41 rules were fixed on every item.

Reading the traces gives four patterns:

1. **The model writes a whole document skeleton.** To wrap a page in
   `<main>`, it sends `<<start>>` → `<html lang="en"><body><main>`. Since
   Phase 6, `<<start>>` already inserts inside the body, so this makes a
   second `<html>`. The guard rejects it and says to change the existing tag,
   but the model usually sends the same edit again. This happened on items
   130, 132 and 133 here, and on ACCESS 030 and 086. It is why the guardrails
   turned only 2 of `agent_tools`' 11 invalid fixes into valid ones: they
   stopped the broken HTML, but did not lead to a working alternative.
2. **Fixing one page-level rule breaks another.** An `<h1>` added outside any
   landmark makes `region` fail (139, 147). On a snippet without landmarks,
   everything new has to go inside one.
3. **Long pages.** The model miscopies the text it wants to replace, so the
   find text is not in the page (043, 051), or picks text that occurs several
   times (ACCESS 137).
4. **Contrast fixed to the minimum.** On `color-contrast` the model asks
   `contrast_fix` for 4.5:1 (AA) and uses the closest colour that passes,
   for example `#777777` → `#757575`. The benchmark scans also run the AAA
   rule, so the same text then fails `color-contrast-enhanced` (7:1), which
   counts as breaking another rule (065, 067, 074). On other pages the edited
   colour was not the one the failing element uses (072).

What would likely fix these:

- for the first pattern, strip `<html>`, `<head>` and `<body>` from what is
  inserted at an anchor;
- for page-level rules, a tool that wraps the content in `<main>`
  deterministically;
- for contrast, aim for 7:1 when the page is checked against AAA.

None of these is done. They would
have to be built and measured on dev, and these test sets cannot be used
again to measure them honestly, now that we have read their failures. A new
held-out set (another benchmark, or a fresh set of public pages) would be
needed.

### 5.6 The guardrails in numbers

| | AccessGuru test | ACCESS |
|---|---|---|
| Pages blocked at the input screen | 0 / 138 | 0 / 100 |
| Items with at least one rejected attempt | 17 / 138 | 9 / 100 |
| Rejected attempts by reason | broken markup 24, removed content 2 | broken markup 11, removed content 1 |
| Final changes flagged for a person | 95 / 138 | 62 / 100 |
| Flags by kind | landmarks 32, text alternatives 31, colours 30, visible text 12, marked decorative 4, language 1 | text alternatives 30, landmarks 29, visible text 8, language 4, marked decorative 2 |

- **No false alarms at the input.** The input screen blocked none of the 238
  pages. The benchmarks contain no attacks, so this measures false alarms
  only. Attacks were measured in Phase 5 (docs/05-guardrails.md).
- **The output checks did the work.** Almost every rejection was broken
  markup.
- **Flags are a checklist, not errors.** About two thirds of the final
  changes carry one, mostly new alt text, landmarks and colours. The scanner
  cannot tell whether `alt="photo"` describes the image, so a person should
  look.

### 5.7 Cost and speed

AccessGuru test, per item:

| Method | Model calls | Tokens | Model time (median) | Wall time (median / 90th percentile) | Paid plan, per 100 pages |
|---|---|---|---|---|---|
| `zero_shot` | 1.0 | 2,056 | 2.5 s | 2.8 s / 11.9 s | $0.08 |
| `accessguru` | 6.2 | 28,898 | 32.4 s | 33.2 s / 73.2 s | $0.78 |
| `agent` | 2.0 | 5,658 | 3.2 s | 4.2 s / 10.9 s | $0.13 |
| `agent_tools` | 3.5 | 11,624 | 4.4 s | 5.4 s / 17.8 s | $0.22 |
| `agent_rag` | 3.3 | 14,391 | 5.0 s | 8.2 s / 18.9 s | $0.27 |
| `agent_guarded` | 3.6 | 11,532 | as `agent_tools`* | as `agent_tools`* | $0.22 |

\* `agent_guarded` reused 463 of its 493 replies from `agent_tools`' cache,
so its own timings (0.0 s model time, 1.4 s wall) measure the cache. It sends
the same requests, so its real speed is that of `agent_tools`.

- The runs used Mistral's free tier, so they cost nothing. The last column
  is what the paid plan would charge.
- The guarded agent costs **22 cents per 100 pages**. That is 3.5 times less
  than the published method, with 2.5 times fewer tokens and about 7 times
  less model time, while fixing far more items safely.
- On ACCESS (smaller pages): $0.16 per 100 pages for the agent and $0.54
  for the published method.

### 5.8 Real public pages

Not run yet (Step 5). The 12 home pages in `scripts/public_pages.txt`
cover Indian government, e-commerce, education and hospital sites.
Government sites are meant to meet WCAG 2.1 AA under GIGW.

When `results/public-pages/README.md` exists, this section summarises it:

- the score before and after on each page;
- which rules were fixed across pages;
- the review flags and rejected attempts;
- the cost per page;
- which sites could not be tested, and why.

### 5.9 How this compares with the paper

The AccessGuru paper reports a plain score of 0.84 with GPT-4 and 0.82 with
Mistral-7B. Our reimplementation of its method on Mistral Small 4 gets 0.75
(AccessGuru test) and 0.81 (ACCESS) on the plain score. That is close, so
the baseline is a fair reproduction, not a weakened one. The numbers are not
directly comparable:

- the paper used axe-core 4.4.1 (we use 4.12.1);
- it scored rows that do not reproduce with hand-written rules (we drop them);
- it did not check for deleted content.

### 5.10 Limitations

- **Snippets, not whole sites.** The benchmarks store a fragment around each
  violation. Page-level rules on a fragment are partly artificial, and real
  pages also have stylesheets, scripts and several violations at once.
- **The scanner checks only what can be automated.** Passing axe-core is not
  the same as being usable with a screen reader. Nobody judged the quality of
  the new alt text or labels; the review flags mark where a person should.
- **One model, one run per method.** Another model could rank the methods
  differently. The model's randomness was not measured with repeated runs;
  the bootstrap measures item-to-item variation only.
- **The guarded run reused `agent_tools`' replies.** That isolates the effect
  of the guardrails exactly (same model replies until a rejection), but the
  two runs are not independent samples of the model.
- **The safe score is our definition.** The plain score and "deletions count
  as failures" are reported beside it in every table, so the reader can
  choose.
- **The input screen was not tested against attacks on the test sets**,
  because they contain none. Phase 5's small set of attacks is the only
  evidence there, and Prompt Guard 2 caught little that the rules missed.
- **Seven comparisons.** Read the intervals, not single p-values.

---

## What you can now explain

- **Why a held-out test set** and a freeze: tuning on the test set makes its
  numbers meaningless.
- **Recording code versions** with results, and why resumable runs need it.
- **Bootstrap confidence intervals**, paired differences and the sign test,
  in words.
- **Multiple comparisons**: why one p-value near 0.05 is weak evidence.
- **Error analysis by category**, from traces.
- **How a metric gets gamed**: on the plain score the published method looks
  as good as the agent, because deleting a broken element also "fixes" it.
  Reporting the strict and the lenient score side by side shows the reader
  why the ranking changes.
- **Reporting cost and latency honestly**, including what caching does to
  timings.
