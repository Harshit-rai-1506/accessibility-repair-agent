from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def examples_dir() -> Path:
    return ROOT / "examples"


def pytest_collection_modifyitems(config, items):
    """Skip browser tests with a clear reason when axe-core has not been downloaded yet."""
    from a11y_agent.config import settings

    if settings.axe_path.exists():
        return
    skip = pytest.mark.skip(reason="axe-core missing: run python scripts/fetch_resources.py --only axe")
    for item in items:
        if "browser" in item.keywords:
            item.add_marker(skip)
