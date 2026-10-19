"""Project settings, read once from environment variables and the .env file.

Keeping every tunable value in one place means the rest of the code never
calls os.getenv directly, and the README can document all knobs in one table.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# src/a11y_agent/config.py -> parents[2] is the repository root.
# This works because the project is installed in editable mode (pip install -e .).
ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")


def _env(name: str, default: str) -> str:
    value = os.getenv(name, "").strip()
    return value or default


@dataclass(frozen=True)
class Settings:
    # "provider:model" strings, e.g. "groq:openai/gpt-oss-120b"
    model: str = field(default_factory=lambda: _env("MODEL", "groq:openai/gpt-oss-120b"))
    fallback_model: str = field(default_factory=lambda: _env("FALLBACK_MODEL", "groq:openai/gpt-oss-20b"))
    reasoning_effort: str = field(default_factory=lambda: _env("REASONING_EFFORT", "low"))
    max_retries: int = field(default_factory=lambda: int(_env("MAX_RETRIES", "5")))

    # Free-tier limits for gpt-oss on Groq are 30 requests and 8,000 tokens per
    # minute; we stay a little below. Set both to 0 on a paid plan.
    rate_limit_tpm: int = field(default_factory=lambda: int(_env("RATE_LIMIT_TPM", "7000")))
    rate_limit_rpm: int = field(default_factory=lambda: int(_env("RATE_LIMIT_RPM", "25")))
    # A single request larger than this is skipped (counted as "too_large").
    max_request_tokens: int = field(default_factory=lambda: int(_env("MAX_REQUEST_TOKENS", "8000")))
    max_output_tokens: int = field(default_factory=lambda: int(_env("MAX_OUTPUT_TOKENS", "6000")))

    # axe-core is the rule engine that decides what counts as a violation.
    axe_version: str = field(default_factory=lambda: _env("AXE_VERSION", "4.12.1"))
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", str(ROOT / "data"))))

    @property
    def axe_path(self) -> Path:
        return self.data_dir / "vendor" / f"axe-core-{self.axe_version}.min.js"

    @property
    def benchmark_dir(self) -> Path:
        return self.data_dir / "benchmark"

    @property
    def corpus_dir(self) -> Path:
        return self.data_dir / "corpus"

    @property
    def runs_dir(self) -> Path:
        return Path(_env("RUNS_DIR", str(ROOT / "runs")))


settings = Settings()
