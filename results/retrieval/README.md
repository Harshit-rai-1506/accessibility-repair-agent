# Retrieval results

Made with `python -m a11y_agent.retrieval eval --save`.

75 queries, one per axe-core rule that maps to a WCAG success criterion (the rule's help text and description). A result counts as relevant if it belongs to one of the rule's criteria. 2357 chunks; embeddings: bge-small-en-v1.5-int8; no criteria filter.

| Search | hit@1 | hit@3 | hit@10 | MRR@10 |
|---|---|---|---|---|
| bm25 | 0.59 | 0.80 | 0.91 | 0.71 |
| dense | 0.60 | 0.77 | 0.89 | 0.70 |
| hybrid | 0.64 | 0.84 | 0.91 | 0.74 |
