# Third-party resources

This repository contains original code, plus the AccessGuru prompt texts
described at the end of this file. The resources below are downloaded at
setup time by `scripts/fetch_resources.py` and are not redistributed here.
Each is pinned to an exact version or commit, listed in `data/MANIFEST.json`
after download.

| Resource | Used for | Source (pinned) | Licence |
|---|---|---|---|
| AccessGuru benchmark and prompt-support files | Evaluation data, WCAG mapping | [NadeenAhmad/AccessGuruLLM](https://github.com/NadeenAhmad/AccessGuruLLM) @ `bf45666` | Dataset CC BY 4.0, code MIT |
| ACCESS violations dataset (Huang et al., 2024), as distributed in the AccessGuru repository | Second evaluation set | same as above | See the original paper |
| axe-core 4.12.1 | Accessibility rule engine (ground truth for every fix) | npm `axe-core@4.12.1`, SHA-256 verified | MPL-2.0, © Deque Systems |
| axe-core rule metadata (`lib/rules`, `locales/_template.json`) | Rule → WCAG mapping, help text for retrieval | [dequelabs/axe-core](https://github.com/dequelabs/axe-core) @ `v4.12.1` | MPL-2.0, © Deque Systems |
| WCAG 2.2 Understanding documents, success criteria and Techniques | Retrieval corpus (RAG) | [w3c/wcag](https://github.com/w3c/wcag) @ `23bad59` | [W3C Document License](https://www.w3.org/Consortium/Legal/copyright-documents) |

## Citations

If you use the benchmark results from this project, please also cite the work
that created the data:

> Nadeen Fathallah, Daniel Hernández, Steffen Staab. 2025. *AccessGuru:
> Leveraging LLMs to Detect and Correct Web Accessibility Violations in HTML
> Code.* In Proceedings of the 27th International ACM SIGACCESS Conference on
> Computers and Accessibility (ASSETS '25). https://doi.org/10.1145/3663547.3746360

> Calista Huang, Alyssa Ma, Suchir Vyasamudri, Eugenie Puype, Sayem Kamal,
> Juan Belza Garcia, Salar Cheema, Michael Lutz. 2024. *ACCESS: Prompt
> Engineering for Automated Web Accessibility Violation Corrections.*
> arXiv:2401.16450.

The evaluation metric (violation score decrease) follows the AccessGuru paper;
the implementation in `src/a11y_agent/scoring.py` is written from scratch.

## AccessGuru prompt texts

`src/a11y_agent/prompts.py` contains prompt texts (the expert persona, the
violation-category descriptions and the four metacognitive steps, including
the corrective re-prompt) copied from
`code/AccessGuru/AccessGuruCorrect/syntax_layout_metacognitive_reprompting_gpt4.py`
in [NadeenAhmad/AccessGuruLLM](https://github.com/NadeenAhmad/AccessGuruLLM),
so that our evaluation runs their method as published. They are used under the
MIT licence:

```
MIT License

Copyright (c) 2025 Nadeen Fathallah

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
