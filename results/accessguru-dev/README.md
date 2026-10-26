# Results: accessguru dev split

Made with `python -m a11y_agent.evaluate report --save`. One JSON summary per run.

All items:

| Method | Model | Items | Score decrease | Element-level only | Resolved | Broke something | Dropped content | Too large | Tokens per item | Paid cost |
|---|---|---|---|---|---|---|---|---|---|---|
| accessguru | openai/gpt-oss-120b | 14/40 (partial) | 0.40 | 0.25 | 6/14 | 0 | 3 | 7 | 12647 | $0.05 |
| agent | openai/gpt-oss-120b | 1/40 (partial) | 1.00 | 1.00 | 1/1 | 0 | 0 | 0 | 893 | $0.00 |
| zero_shot | openai/gpt-oss-120b | 40/40 | 0.51 | 0.53 | 21/40 | 11 | 5 | 3 | 930 | $0.01 |

Only the 1 items every run could send (the fairest comparison on the free tier):

| Method | Model | Items | Score decrease | Element-level only | Resolved | Broke something | Dropped content | Too large | Tokens per item | Paid cost |
|---|---|---|---|---|---|---|---|---|---|---|
| accessguru | openai/gpt-oss-120b | 1/1 | 1.00 | 1.00 | 1/1 | 0 | 0 | 0 | 12643 | $0.00 |
| agent | openai/gpt-oss-120b | 1/1 | 1.00 | 1.00 | 1/1 | 0 | 0 | 0 | 893 | $0.00 |
| zero_shot | openai/gpt-oss-120b | 1/1 | 0.00 | 0.00 | 0/1 | 0 | 0 | 0 | 415 | $0.00 |
