"""Comparing methods honestly (Phase 6): confidence intervals and paired tests.

With 40 or 138 items, a handful of items decides a few points of score. On
the dev set, two agent variants with almost the same prompt disagreed on 10
of 40 items. So every comparison in the final report comes with:

- a 95% confidence interval for each method's score, from the bootstrap:
  resample the items with replacement 10,000 times, recompute the score, and
  take the middle 95% of the results;
- a *paired* comparison with a baseline: both methods are resampled on the
  same items each time, so the interval is for the difference itself, and
  item-to-item variation that affects both methods cancels out;
- an exact sign test on the items one method resolved and the other did not.

The score is the AccessGuru violation score decrease, 1 - sum(new) / sum(old),
with deletions counted as failures (a fix that removed content scores as no
fix), exactly as ``evaluate.summarize_rows`` computes it.
"""

from __future__ import annotations

import math

import numpy as np

RESAMPLES = 10_000
SEED = 20261028            # fixed, so a report is reproducible to the last digit


def scores(row: dict, *, deletions_fail: bool = True) -> tuple[int, int]:
    """(old score, new score) of one result row; a deletion keeps the old score when ``deletions_fail``."""
    old = row["old_score"]
    new = old if deletions_fail and row.get("dropped_content") else row["new_score"]
    return old, new


def resolved(row: dict) -> bool:
    """Fixed cleanly, and without deleting content."""
    return bool(row.get("resolved")) and not row.get("dropped_content")


def score_decrease(rows: list[dict], *, deletions_fail: bool = True) -> float:
    pairs = [scores(r, deletions_fail=deletions_fail) for r in rows]
    old = sum(o for o, _ in pairs)
    return 1 - sum(n for _, n in pairs) / old if old else 0.0


def _arrays(rows: list[dict]) -> tuple[np.ndarray, np.ndarray]:
    pairs = [scores(r) for r in rows]
    return np.array([o for o, _ in pairs], dtype=float), np.array([n for _, n in pairs], dtype=float)


def _indices(n: int, resamples: int, seed: int) -> np.ndarray:
    return np.random.default_rng(seed).integers(0, n, size=(resamples, n))


def _interval(values: np.ndarray) -> tuple[float, float]:
    low, high = np.percentile(values, [2.5, 97.5])
    return float(low), float(high)


def bootstrap_interval(rows: list[dict], resamples: int = RESAMPLES, seed: int = SEED) -> tuple[float, float]:
    """95% confidence interval of one method's score decrease."""
    if not rows:
        return 0.0, 0.0
    old, new = _arrays(rows)
    idx = _indices(len(rows), resamples, seed)
    totals = old[idx].sum(axis=1)
    return _interval(1 - new[idx].sum(axis=1) / np.where(totals == 0, 1, totals))


def paired_difference(rows_a: list[dict], rows_b: list[dict], resamples: int = RESAMPLES,
                      seed: int = SEED) -> tuple[float, float, float]:
    """(difference a - b in score decrease, 95% interval) on the same items, in the same order."""
    if not rows_a:
        return 0.0, 0.0, 0.0
    old_a, new_a = _arrays(rows_a)
    old_b, new_b = _arrays(rows_b)
    idx = _indices(len(rows_a), resamples, seed)
    ta, tb = old_a[idx].sum(axis=1), old_b[idx].sum(axis=1)
    diff = (1 - new_a[idx].sum(axis=1) / np.where(ta == 0, 1, ta)) - (1 - new_b[idx].sum(axis=1) / np.where(tb == 0, 1, tb))
    low, high = _interval(diff)
    return score_decrease(rows_a) - score_decrease(rows_b), low, high


def sign_test(wins: int, losses: int) -> float:
    """Exact two-sided sign test: how surprising is a split of wins and losses if both methods were equal?"""
    n = wins + losses
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(wins, losses) + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def paired_flips(rows_a: list[dict], rows_b: list[dict]) -> tuple[int, int]:
    """(items a resolved and b did not, items b resolved and a did not)."""
    wins = sum(resolved(a) and not resolved(b) for a, b in zip(rows_a, rows_b))
    losses = sum(resolved(b) and not resolved(a) for a, b in zip(rows_a, rows_b))
    return wins, losses
