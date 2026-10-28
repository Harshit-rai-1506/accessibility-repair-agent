# Results: accessguru test split

Made with `python -m a11y_agent.evaluate report --save`. One JSON summary per run, and `items__*.csv` with one line per item (outcome, cost, and the agent's step trace).

*Safe fixes only*: a fix that fails a guardrail check (removed or hid content, added a script or URL, broke the markup) counts as no fix, whichever method made it.

*Deletions count as failures*: the same score, but a fix that removed more than a tenth of the content scores as no fix.

### mistral-small-2603 (mistralai)

All items:

| Method | Model | Items | Score decrease | Deletions count as failures | Safe fixes only | Element-level only | Resolved | Broke something | Dropped content | Unsafe fixes | Too large | Tokens per item | Paid cost |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| noop | - | 138/138 | 0.00 | 0.00 | 0.00 | 0.00 | 0/138 | 0 | 0 | 0 | 0 | 0 | - |
| accessguru | mistral-small-2603 | 138/138 | 0.75 | 0.36 | 0.29 | 0.81 | 99/138 | 21 | 56 | 43 | 0 | 28898 | $1.08 |
| agent | mistral-small-2603 | 138/138 | 0.73 | 0.73 | 0.69 | 0.74 | 99/138 | 4 | 0 | 8 | 0 | 5658 | $0.17 |
| agent_guarded | mistral-small-2603 | 138/138 | 0.78 | 0.78 | 0.78 | 0.84 | 104/138 | 2 | 0 | 0 | 0 | 11532 | $0.03 |
| agent_rag | mistral-small-2603 | 138/138 | 0.77 | 0.77 | 0.73 | 0.84 | 103/138 | 2 | 0 | 6 | 0 | 14391 | $0.36 |
| agent_tools | mistral-small-2603 | 138/138 | 0.81 | 0.81 | 0.75 | 0.84 | 110/138 | 2 | 0 | 11 | 0 | 11624 | $0.30 |
| zero_shot | mistral-small-2603 | 138/138 | 0.55 | 0.47 | 0.47 | 0.58 | 77/138 | 31 | 19 | 9 | 0 | 2056 | $0.10 |

Guardrails (Phase 5): what they stopped, rejected and flagged:

| Method | Model | Pages blocked at input | Items with a rejected attempt | Rejected attempts by reason | Changes flagged for review | Flags by kind |
|---|---|---|---|---|---|---|
| agent_guarded | mistral-small-2603 | 0/138 | 17/138 | broken_markup 24, removes_content 2 | 95/138 | adds_landmarks 32, added_text_alternative 31, changed_colours 30, added_visible_text 12, marked_decorative 4, sets_language 1 |
