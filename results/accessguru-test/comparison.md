# Comparison: accessguru test (mistralai:mistral-small-2603)

Made with `python -m a11y_agent.evaluate compare --save`. Items every run scored; safe fixes only (a fix that removed or hid content, added a script or URL, or broke the markup counts as no fix); 95% intervals from 10,000 bootstrap resamples of the items (paired for differences); sign test on items one method resolved and the other did not.

| Method | Model | Items | Safe score | 95% interval | vs agent_tools | 95% interval of the difference | Resolved here, not there / there, not here | Sign test p |
|---|---|---|---|---|---|---|---|---|
| noop | - | 138 | 0.00 | 0.00 to 0.00 | -0.75 | -0.82 to -0.68 | 0 / 100 | 0.00 |
| accessguru | mistral-small-2603 | 138 | 0.29 | 0.21 to 0.37 | -0.46 | -0.55 to -0.37 | 5 / 65 | 0.00 |
| agent_guarded | mistral-small-2603 | 138 | 0.78 | 0.70 to 0.84 | +0.03 | +0.01 to +0.05 | 4 / 0 | 0.12 |
| agent_rag | mistral-small-2603 | 138 | 0.73 | 0.66 to 0.80 | -0.02 | -0.08 to +0.05 | 12 / 15 | 0.70 |
| agent_tools | mistral-small-2603 | 138 | 0.75 | 0.68 to 0.82 | - | - | - | - |
| agent | mistral-small-2603 | 138 | 0.69 | 0.61 to 0.76 | -0.06 | -0.14 to +0.02 | 16 / 24 | 0.27 |
| zero_shot | mistral-small-2603 | 138 | 0.47 | 0.38 to 0.57 | -0.28 | -0.38 to -0.17 | 12 / 47 | 0.00 |

Cost and speed:

| Method | Model | Model calls per item | Tokens per item | Model time per item (median) | Wall time per item (median / 90th percentile) | Replies from the cache | Paid plan, per 100 pages |
|---|---|---|---|---|---|---|---|
| noop | - | 0.0 | 0 | 0.0 s | 0.3 s / 0.5 s | 0/0 | - |
| accessguru | mistral-small-2603 | 6.2 | 28,898 | 32.4 s | 33.2 s / 73.2 s | 0/856 | $0.78 |
| agent | mistral-small-2603 | 2.0 | 5,658 | 3.2 s | 4.2 s / 10.9 s | 7/275 | $0.13 |
| agent_guarded | mistral-small-2603 | 3.6 | 11,532 | 0.0 s | 1.4 s / 4.1 s | 463/493 | $0.22 |
| agent_rag | mistral-small-2603 | 3.3 | 14,391 | 5.0 s | 8.2 s / 18.9 s | 8/458 | $0.27 |
| agent_tools | mistral-small-2603 | 3.5 | 11,624 | 4.4 s | 5.4 s / 17.8 s | 16/486 | $0.22 |
| zero_shot | mistral-small-2603 | 1.0 | 2,056 | 2.5 s | 2.8 s / 11.9 s | 4/138 | $0.08 |

Code versions: 1075c0e.
