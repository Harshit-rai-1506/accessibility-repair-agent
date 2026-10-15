from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def examples_dir() -> Path:
    return ROOT / "examples"


def pytest_collection_modifyitems(config, items):
    """Skip tests that need downloaded resources, with the command that fixes it."""
    from a11y_agent.config import settings

    missing = {
        "browser": not settings.axe_path.exists(),
        "data": not (settings.corpus_dir / "wcag" / "guidelines" / "index.html").exists()
                or not (settings.benchmark_dir / "accessguru").exists(),
    }
    for item in items:
        for marker, is_missing in missing.items():
            if is_missing and marker in item.keywords:
                item.add_marker(pytest.mark.skip(reason="resources missing: run python scripts/fetch_resources.py"))
