# Results: access test split

Made with `python -m a11y_agent.evaluate report --save`. One JSON summary per run, and `items__*.csv` with one line per item (outcome, cost, and the agent's step trace).

*Safe fixes only*: a fix that fails a guardrail check (removed or hid content, added a script or URL, broke the markup) counts as no fix, whichever method made it.

*Deletions count as failures*: the same score, but a fix that removed more than a tenth of the content scores as no fix.

### mistral-small-2603 (mistralai)

All items:

| Method | Model | Items | Score decrease | Deletions count as failures | Safe fixes only | Element-level only | Resolved | Broke something | Dropped content | Unsafe fixes | Too large | Tokens per item | Paid cost |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| noop | - | 100/100 | 0.00 | 0.00 | 0.00 | 0.00 | 0/100 | 0 | 0 | 0 | 0 | 0 | - |
| accessguru | mistral-small-2603 | 100/100 | 0.81 | 0.51 | 0.42 | 0.82 | 76/100 | 12 | 31 | 30 | 0 | 19392 | $0.54 |
| agent_guarded | mistral-small-2603 | 100/100 | 0.77 | 0.77 | 0.77 | 0.79 | 75/100 | 1 | 0 | 0 | 0 | 7732 | $0.01 |
| agent_tools | mistral-small-2603 | 100/100 | 0.81 | 0.81 | 0.77 | 0.79 | 79/100 | 1 | 0 | 5 | 0 | 7646 | $0.15 |
| zero_shot | mistral-small-2603 | 100/100 | 0.67 | 0.64 | 0.62 | 0.71 | 62/100 | 26 | 4 | 3 | 0 | 727 | $0.03 |

Guardrails (Phase 5): what they stopped, rejected and flagged:

| Method | Model | Pages blocked at input | Items with a rejected attempt | Rejected attempts by reason | Changes flagged for review | Flags by kind |
|---|---|---|---|---|---|---|
| agent_guarded | mistral-small-2603 | 0/100 | 9/100 | broken_markup 11, removes_content 1 | 62/100 | added_text_alternative 30, adds_landmarks 29, added_visible_text 8, sets_language 4, marked_decorative 2 |
