"""The fixed dev/test split and the rows behind it."""

import asyncio
import json
from collections import Counter

import pytest

from a11y_agent.benchmark import (
    BenchmarkItem,
    PAGE_LEVEL_RULES,
    find_reproducible,
    load_split,
    read_split,
    split_path,
    stratified_split,
)


def fake_items(counts: dict[str, int]) -> list[BenchmarkItem]:
    items, n = [], 0
    for rule, k in counts.items():
        for _ in range(k):
            items.append(BenchmarkItem(id=f"x-{n:03d}", dataset="x", rule_id=rule, category="Syntax",
                                       impact="serious", url="", description="", html=""))
            n += 1
    return items


def test_stratified_split_hits_the_size_and_keeps_proportions():
    items = fake_items({"a": 50, "b": 30, "c": 10, **{f"rare{i}": 1 for i in range(10)}})
    dev, test = stratified_split(items, 20, seed=1)
    assert len(dev) == 20 and len(test) == 80 and not set(dev) & set(test)
    rule_of = {i.id: i.rule_id for i in items}
    dev_rules = Counter(rule_of[i] for i in dev)
    assert dev_rules["a"] == 10 and dev_rules["b"] == 6 and dev_rules["c"] == 2


def test_stratified_split_is_deterministic():
    items = fake_items({"a": 7, "b": 5, "c": 3})
    assert stratified_split(items, 5, seed=3) == stratified_split(items, 5, seed=3)


def test_page_level_rules():
    assert {"region", "landmark-one-main", "page-has-heading-one"} == PAGE_LEVEL_RULES


@pytest.mark.parametrize("dataset, dev, test", [("accessguru", 40, 138), ("access", 0, 100)])
def test_split_files(dataset, dev, test):
    info = json.loads(split_path(dataset).read_text(encoding="utf-8"))
    assert info["counts"]["dev"] == len(info["dev"]) == dev
    assert info["counts"]["test"] == len(info["test"]) == test
    assert info["counts"]["reproducible"] == dev + test
    assert not set(info["dev"]) & set(info["test"])
    assert info["axe_version"] == "4.12.1"


@pytest.mark.data
def test_split_rows_load():
    dev = load_split("accessguru", "dev")
    assert len(dev) == 40 and {i.category for i in dev} <= {"Syntax", "Layout"}
    assert all(i.html.startswith("<") and i.description for i in dev)
    assert len(load_split("access", "test")) == 100


@pytest.mark.data
def test_wrong_split_name():
    with pytest.raises(ValueError):
        load_split("accessguru", "train")


@pytest.mark.browser
@pytest.mark.data
@pytest.mark.slow
def test_every_split_row_reproduces_on_this_machine():
    """Rendering differs slightly between operating systems; CI runs this on
    Linux and Windows so we know every scored violation is real on both."""
    items = []
    for dataset in ("accessguru", "access"):
        info = read_split(dataset)
        for split in ("dev", "test"):
            if info[split]:
                items += load_split(dataset, split)
    kept, dropped = asyncio.run(find_reproducible(items))
    assert not dropped, dict(dropped)
