# Comparison: access test (mistralai:mistral-small-2603)

Made with `python -m a11y_agent.evaluate compare --save`. Items every run scored; safe fixes only (a fix that removed or hid content, added a script or URL, or broke the markup counts as no fix); 95% intervals from 10,000 bootstrap resamples of the items (paired for differences); sign test on items one method resolved and the other did not.

| Method | Model | Items | Safe score | 95% interval | vs agent_tools | 95% interval of the difference | Resolved here, not there / there, not here | Sign test p |
|---|---|---|---|---|---|---|---|---|
| noop | - | 100 | 0.00 | 0.00 to 0.00 | -0.77 | -0.85 to -0.69 | 0 / 75 | 0.00 |
| accessguru | mistral-small-2603 | 100 | 0.42 | 0.32 to 0.52 | -0.35 | -0.47 to -0.24 | 5 / 41 | 0.00 |
| agent_guarded | mistral-small-2603 | 100 | 0.77 | 0.69 to 0.85 | +0.00 | +0.00 to +0.00 | 0 / 0 | 1.00 |
| agent_tools | mistral-small-2603 | 100 | 0.77 | 0.69 to 0.85 | - | - | - | - |
| zero_shot | mistral-small-2603 | 100 | 0.62 | 0.51 to 0.72 | -0.15 | -0.26 to -0.05 | 5 / 23 | 0.00 |

Cost and speed:

| Method | Model | Model calls per item | Tokens per item | Model time per item (median) | Wall time per item (median / 90th percentile) | Replies from the cache | Paid plan, per 100 pages |
|---|---|---|---|---|---|---|---|
| noop | - | 0.0 | 0 | 0.0 s | 0.2 s / 0.4 s | 0/0 | - |
| accessguru | mistral-small-2603 | 5.8 | 19,392 | 27.6 s | 28.5 s / 49.6 s | 0/584 | $0.54 |
| agent_guarded | mistral-small-2603 | 3.3 | 7,732 | 0.0 s | 0.7 s / 1.6 s | 326/333 | $0.16 |
| agent_tools | mistral-small-2603 | 3.3 | 7,646 | 3.8 s | 5.2 s / 16.9 s | 29/328 | $0.16 |
| zero_shot | mistral-small-2603 | 1.0 | 727 | 1.0 s | 1.6 s / 5.8 s | 9/100 | $0.03 |

Code versions: 1075c0e.
