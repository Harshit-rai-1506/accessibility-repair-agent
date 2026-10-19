"""The benchmark: which rows we evaluate on, and the fixed dev/test split.

Two ideas live here.

1. **Only score what a machine can check.** A benchmark row is usable only if
   the violation it claims actually shows up when we scan its stored HTML
   snippet. Otherwise a "fix" could never be verified. We scan every row once
   and keep the ones that reproduce.

2. **Split once, and never tune on the test set.** The reproducible rows are
   divided into a small *dev* set, used while building and tuning, and a larger
   *test* set, run only at the end. The split is made once with a fixed random
   seed, stratified by rule (so both sets contain the same mix of violation
   types), and saved to ``splits/<dataset>.json`` in the repository. Every
   later run, on any machine, uses exactly these rows.

Commands (run from the repository root):

    python -m a11y_agent.benchmark stats          # what the split contains
    python -m a11y_agent.benchmark make-splits    # re-create the split files (only if the data changes)
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import json
import random
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from langchain_core.documents import Document

from .config import ROOT, settings
from .loaders import BenchmarkLoader
from .scanner import DEFAULT_TAGS, Scanner

SPLITS_DIR = ROOT / "splits"
SPLIT_SEED = 20261020
DEV_SIZE = 40

# Rules that fire on almost any snippet that is not wrapped in a full page
# (no <main>, no <h1>, content outside landmarks). They are real violations,
# but much easier to "fix" than element-level ones, so results are also
# reported without them.
PAGE_LEVEL_RULES = frozenset({"region", "landmark-one-main", "page-has-heading-one"})


@dataclass(frozen=True)
class BenchmarkItem:
    """One violation to fix: a broken HTML snippet and the rule it breaks."""
    id: str             # e.g. "accessguru-012"
    dataset: str
    rule_id: str        # axe rule, e.g. "image-alt"
    category: str       # "Syntax" | "Layout" | "" (ACCESS has no categories)
    impact: str         # as recorded in the dataset
    url: str            # page the snippet came from
    description: str    # the rule's description, as recorded in the dataset
    html: str           # the snippet to fix

    @property
    def page_level(self) -> bool:
        return self.rule_id in PAGE_LEVEL_RULES

    @classmethod
    def from_document(cls, doc: Document) -> "BenchmarkItem":
        m = doc.metadata
        return cls(id=doc.id, dataset=m["dataset"], rule_id=m["rule_id"], category=m["category"],
                   impact=m["impact"], url=m["url"], description=m.get("description", ""),
                   html=doc.page_content)


# Which rows of each dataset are candidates for the split.
SPLIT_SOURCES = {
    # Semantic rows are excluded: their violations are judgements (is this alt
    # text meaningful?) that a rule engine cannot verify.
    "accessguru": {"categories": ["Syntax", "Layout"], "dev_size": DEV_SIZE},
    # ACCESS is kept whole as a second, independent test set.
    "access": {"categories": None, "dev_size": 0},
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_items(dataset: str) -> list[BenchmarkItem]:
    """All candidate rows of a dataset, before the reproducibility filter."""
    source = SPLIT_SOURCES[dataset]
    docs = BenchmarkLoader(dataset, categories=source["categories"]).load()
    return [BenchmarkItem.from_document(d) for d in docs]


async def find_reproducible(items: list[BenchmarkItem]) -> tuple[list[BenchmarkItem], Counter]:
    """Scan every snippet; keep rows whose stated rule is actually violated."""
    kept, dropped = [], Counter()
    async with Scanner() as scanner:
        for item in items:
            result = await scanner.scan_html(item.html)
            if item.rule_id in result.rule_ids:
                kept.append(item)
            else:
                dropped[item.rule_id] += 1
    return kept, dropped


def stratified_split(items: list[BenchmarkItem], dev_size: int, seed: int) -> tuple[list[str], list[str]]:
    """Choose ``dev_size`` items for dev so each rule keeps its share of the total.

    Every rule gets ``floor(share)`` dev items; the remaining dev places go to
    the rules with the largest leftover fractions (ties broken by the seeded
    random generator). This hits the dev size exactly and keeps common rules
    proportionally represented, while rare rules land in dev or test at random.
    """
    if dev_size <= 0:
        return [], sorted(i.id for i in items)
    rng = random.Random(seed)
    by_rule: dict[str, list[str]] = defaultdict(list)
    for item in sorted(items, key=lambda i: i.id):
        by_rule[item.rule_id].append(item.id)

    total = len(items)
    quotas, remainders = {}, []
    for rule in sorted(by_rule):
        share = len(by_rule[rule]) * dev_size / total
        quotas[rule] = int(share)
        remainders.append((share - int(share), rng.random(), rule))
    for _, _, rule in sorted(remainders, reverse=True)[: dev_size - sum(quotas.values())]:
        quotas[rule] += 1

    dev = []
    for rule in sorted(by_rule):
        ids = list(by_rule[rule])
        rng.shuffle(ids)
        dev += ids[: quotas[rule]]
    dev_set = set(dev)
    test = [i.id for i in items if i.id not in dev_set]
    return sorted(dev), sorted(test)


def make_split(dataset: str, seed: int = SPLIT_SEED) -> dict:
    source = SPLIT_SOURCES[dataset]
    loader = BenchmarkLoader(dataset)
    items = load_items(dataset)
    kept, dropped = asyncio.run(find_reproducible(items))
    dev, test = stratified_split(kept, source["dev_size"], seed)
    return {
        "dataset": dataset,
        "source": loader.path.relative_to(settings.data_dir).as_posix(),
        "source_sha256": _sha256(loader.path),
        "axe_version": settings.axe_version,
        "scan_tags": list(DEFAULT_TAGS),
        "categories": source["categories"],
        "seed": seed,
        "created": dt.date.today().isoformat(),
        "counts": {"rows": len(items), "reproducible": len(kept), "dev": len(dev), "test": len(test)},
        "not_reproducible_by_rule": dict(dropped.most_common()),
        "dev": dev,
        "test": test,
    }


def split_path(dataset: str) -> Path:
    return SPLITS_DIR / f"{dataset}.json"


def read_split(dataset: str) -> dict:
    path = split_path(dataset)
    if not path.exists():
        raise FileNotFoundError(f"{path} not found. Run: python -m a11y_agent.benchmark make-splits")
    return json.loads(path.read_text(encoding="utf-8"))


def load_split(dataset: str, split: str) -> list[BenchmarkItem]:
    """The items of one split, e.g. ``load_split("accessguru", "dev")``.

    Refuses to run if the downloaded CSV differs from the one the split was
    made from, because the row ids would then point at different rows.
    """
    info = read_split(dataset)
    if split not in ("dev", "test"):
        raise ValueError("split must be 'dev' or 'test'")
    csv_path = settings.data_dir / info["source"]
    if not csv_path.exists():
        raise FileNotFoundError(f"{csv_path} not found. Run: python scripts/fetch_resources.py --only benchmark")
    if _sha256(csv_path) != info["source_sha256"]:
        raise RuntimeError(f"{csv_path.name} differs from the file the split was made from; "
                           "re-download it with: python scripts/fetch_resources.py --only benchmark --force")
    wanted = set(info[split])
    items = [i for i in load_items(dataset) if i.id in wanted]
    missing = wanted - {i.id for i in items}
    if missing:
        raise RuntimeError(f"split {dataset}/{split} lists ids not in the data: {sorted(missing)[:5]}")
    return items


def describe(dataset: str) -> str:
    info = read_split(dataset)
    c = info["counts"]
    lines = [f"{dataset}: {c['rows']} rows, {c['reproducible']} reproduce with axe-core {info['axe_version']}"
             f" -> dev {c['dev']}, test {c['test']}"]
    for split in ("dev", "test"):
        if not info[split]:
            continue
        items = load_split(dataset, split)
        rules = Counter(i.rule_id for i in items)
        cats = Counter(i.category or "uncategorised" for i in items)
        page = sum(i.page_level for i in items)
        lines.append(f"  {split:4} {len(items):4} items, {len(rules)} rules, page-level {page}; "
                     + ", ".join(f"{k} {v}" for k, v in sorted(cats.items())))
        lines.append("       top rules: " + ", ".join(f"{r} {n}" for r, n in rules.most_common(6)))
    if info["not_reproducible_by_rule"]:
        top = list(info["not_reproducible_by_rule"].items())[:6]
        lines.append("  not reproducible: " + ", ".join(f"{r} {n}" for r, n in top))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark splits.")
    sub = parser.add_subparsers(dest="command", required=True)
    make = sub.add_parser("make-splits", help="scan every row and write splits/<dataset>.json")
    make.add_argument("--dataset", choices=sorted(SPLIT_SOURCES), nargs="+", default=sorted(SPLIT_SOURCES))
    sub.add_parser("stats", help="describe the saved splits")
    args = parser.parse_args(argv)

    if args.command == "make-splits":
        SPLITS_DIR.mkdir(exist_ok=True)
        for dataset in args.dataset:
            info = make_split(dataset)
            split_path(dataset).write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
            c = info["counts"]
            print(f"{dataset}: {c['reproducible']}/{c['rows']} reproduce -> dev {c['dev']}, test {c['test']}"
                  f"  ({split_path(dataset).relative_to(ROOT)})")
        return 0
    for dataset in sorted(SPLIT_SOURCES):
        if split_path(dataset).exists():
            print(describe(dataset))
    return 0


if __name__ == "__main__":
    sys.exit(main())
