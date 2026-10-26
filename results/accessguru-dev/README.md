# Results: accessguru dev split

Made with `python -m a11y_agent.evaluate report --save`. One JSON summary per run.

All items:

| Method | Model | Items | Score decrease | Element-level only | Resolved | Broke something | Dropped content | Too large | Tokens per item | Paid cost |
|---|---|---|---|---|---|---|---|---|---|---|
| accessguru | openai/gpt-oss-120b | 14/40 (partial) | 0.40 | 0.25 | 6/14 | 0 | 3 | 7 | 12647 | $0.05 |
| zero_shot | openai/gpt-oss-120b | 40/40 | 0.51 | 0.53 | 21/40 | 11 | 5 | 3 | 930 | $0.01 |

Only the 7 items every run could send (the fairest comparison on the free tier):

| Method | Model | Items | Score decrease | Element-level only | Resolved | Broke something | Dropped content | Too large | Tokens per item | Paid cost |
|---|---|---|---|---|---|---|---|---|---|---|
| accessguru | openai/gpt-oss-120b | 7/7 | 0.87 | 1.00 | 6/7 | 0 | 3 | 0 | 17725 | $0.03 |
| zero_shot | openai/gpt-oss-120b | 7/7 | 0.83 | 0.50 | 6/7 | 0 | 3 | 0 | 432 | $0.00 |
