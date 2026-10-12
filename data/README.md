# data/

Everything in this folder except this README is downloaded by

    python scripts/fetch_resources.py

and ignored by git. After a successful run it looks like this:

```
data/
├── MANIFEST.json                  what was downloaded, from where, which version, checksums
├── vendor/
│   └── axe-core-4.12.1.min.js     the accessibility rule engine (injected into each page we scan)
├── benchmark/
│   └── accessguru/data/
│       ├── accessguru_dataset/
│       │   ├── accessguru_sampled_syntax_layout_dataset.csv   305 rows: 195 syntax, 55 layout, 55 semantic
│       │   └── accessguru_sampled_semantic_violations.csv     the 55 semantic rows with human context
│       ├── baseline_two_dataset.csv                          171 rows from Huang et al. (2024)
│       └── prompts_support/                                  WCAG text, rule -> WCAG mapping, taxonomy
└── corpus/
    ├── wcag/            ~860 W3C pages: Understanding docs, success criteria, Techniques (RAG corpus)
    └── axe-rules/       axe-core rule definitions: impact, tags (WCAG criteria), help text
```

Add `--full-benchmark` to also download the full 3,524-row dataset (20 MB).
