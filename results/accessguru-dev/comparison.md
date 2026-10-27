# Comparison: accessguru dev (mistralai:mistral-small-2603)

Made with `python -m a11y_agent.evaluate compare --save`. Items every run scored; safe fixes only (a fix that removed or hid content, added a script or URL, or broke the markup counts as no fix); 95% intervals from 10,000 bootstrap resamples of the items (paired for differences); sign test on items one method resolved and the other did not.

| Method | Model | Items | Safe score | 95% interval | vs agent_tools | 95% interval of the difference | Resolved here, not there / there, not here | Sign test p |
|---|---|---|---|---|---|---|---|---|
| accessguru | mistral-small-2603 | 40 | 0.37 | 0.23 to 0.52 | -0.39 | -0.56 to -0.21 | 2 / 17 | 0.00 |
| agent_guarded | mistral-small-2603 | 40 | 0.76 | 0.62 to 0.89 | +0.00 | +0.00 to +0.00 | 0 / 0 | 1.00 |
| agent_rag | mistral-small-2603 | 40 | 0.68 | 0.53 to 0.82 | -0.08 | -0.25 to +0.08 | 5 / 8 | 0.58 |
| agent_tools | mistral-small-2603 | 40 | 0.76 | 0.62 to 0.89 | - | - | - | - |
| agent | mistral-small-2603 | 40 | 0.61 | 0.46 to 0.76 | -0.15 | -0.32 to +0.01 | 4 / 9 | 0.27 |
| zero_shot | mistral-small-2603 | 40 | 0.56 | 0.41 to 0.71 | -0.20 | -0.36 to -0.03 | 2 / 11 | 0.02 |

Cost and speed:

| Method | Model | Model calls per item | Tokens per item | Model time per item (median) | Wall time per item (median / 90th percentile) | Replies from the cache | Paid plan, per 100 pages |
|---|---|---|---|---|---|---|---|
| accessguru | mistral-small-2603 | 6.1 | 25,510 | 35.6 s | 36.4 s / 81.9 s | 0/244 | $0.68 |
| agent | mistral-small-2603 | 2.0 | 7,721 | 0.0 s | 1.2 s / 3.3 s | 82/82 | $0.17 |
| agent_guarded | mistral-small-2603 | 4.1 | 15,584 | 0.0 s | 1.1 s / 3.4 s | 161/165 | $0.30 |
| agent_rag | mistral-small-2603 | 3.6 | 18,084 | 5.9 s | 7.9 s / 24.2 s | 0/145 | $0.34 |
| agent_tools | mistral-small-2603 | 4.2 | 15,710 | 0.0 s | 1.2 s / 2.1 s | 167/167 | $0.30 |
| zero_shot | mistral-small-2603 | 1.0 | 1,825 | 1.7 s | 2.1 s / 19.3 s | 0/40 | $0.07 |

Code versions: dd2e88d.
