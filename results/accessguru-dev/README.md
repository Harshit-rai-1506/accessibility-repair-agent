# Results: accessguru dev split

Made with `python -m a11y_agent.evaluate report --save`. One JSON summary per run, and `items__*.csv` with one line per item (outcome, cost, and the agent's step trace).

*Deletions count as failures*: the same score, but a fix that removed more than a tenth of the content scores as no fix.

### openai/gpt-oss-120b (groq)

All items:

| Method | Model | Items | Score decrease | Deletions count as failures | Element-level only | Resolved | Broke something | Dropped content | Too large | Tokens per item | Paid cost |
|---|---|---|---|---|---|---|---|---|---|---|---|
| accessguru | openai/gpt-oss-120b | 14/40 (partial) | 0.40 | 0.20 | 0.25 | 6/14 | 0 | 3 | 7 | 12647 | $0.05 |
| agent | openai/gpt-oss-120b | 1/40 (partial) | 1.00 | 1.00 | 1.00 | 1/1 | 0 | 0 | 0 | 893 | $0.00 |
| zero_shot | openai/gpt-oss-120b | 40/40 | 0.51 | 0.45 | 0.53 | 21/40 | 11 | 5 | 3 | 930 | $0.01 |

Only the 1 items every run could send (the fairest comparison when some requests were too large):

| Method | Model | Items | Score decrease | Deletions count as failures | Element-level only | Resolved | Broke something | Dropped content | Too large | Tokens per item | Paid cost |
|---|---|---|---|---|---|---|---|---|---|---|---|
| accessguru | openai/gpt-oss-120b | 1/1 | 1.00 | 1.00 | 1.00 | 1/1 | 0 | 0 | 0 | 12643 | $0.00 |
| agent | openai/gpt-oss-120b | 1/1 | 1.00 | 1.00 | 1.00 | 1/1 | 0 | 0 | 0 | 893 | $0.00 |
| zero_shot | openai/gpt-oss-120b | 1/1 | 0.00 | 0.00 | 0.00 | 0/1 | 0 | 0 | 0 | 415 | $0.00 |

### mistral-small-2603 (mistralai)

All items:

| Method | Model | Items | Score decrease | Deletions count as failures | Element-level only | Resolved | Broke something | Dropped content | Too large | Tokens per item | Paid cost |
|---|---|---|---|---|---|---|---|---|---|---|---|
| accessguru | mistral-small-2603 | 40/40 | 0.75 | 0.47 | 0.70 | 29/40 | 5 | 11 | 0 | 25510 | $0.27 |
| agent | mistral-small-2603 | 40/40 | 0.68 | 0.68 | 0.66 | 28/40 | 0 | 0 | 0 | 7721 | $0.07 |
| agent_rag | mistral-small-2603 | 40/40 | 0.64 | 0.64 | 0.64 | 26/40 | 1 | 0 | 0 | 19760 | $0.15 |
| agent_tools | mistral-small-2603 | 40/40 | 0.78 | 0.78 | 0.72 | 31/40 | 2 | 0 | 0 | 15710 | $0.12 |
| zero_shot | mistral-small-2603 | 40/40 | 0.61 | 0.60 | 0.62 | 23/40 | 7 | 2 | 0 | 1825 | $0.03 |
