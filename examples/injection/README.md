# Prompt-injection examples

Small pages for measuring the input guardrail (Phase 5, `docs/05-guardrails.md`).
Each has a real accessibility problem, so the agent has a reason to read it.

- `attack-*.html` hide an instruction to a model somewhere a model would read it:
  a comment, alt text, an `aria-label`, a hidden element, chat markup, or plain
  reworded prose. `attack-paraphrase.html` uses no known phrasing on purpose:
  the rules are expected to miss it, which is why a classifier is layered on top.
- `benign-*.html` are ordinary pages. `benign-article.html` is an article *about*
  prompt injection that quotes an attack; the rules are expected to flag it
  (a false alarm a person then dismisses at the approval step).

    python -m a11y_agent.guardrails screen examples/injection/attack-comment.html
    python -m a11y_agent.guardrails eval
