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
class Limits:
    """How fast and how big requests to one provider may be (0 = no limit)."""
    tokens_per_minute: int
    requests_per_minute: int
    max_request_tokens: int      # one request (prompt + reply budget) larger than this is skipped as "too_large"
    max_output_tokens: int       # largest reply budget the baselines ask for
    min_interval: float = 0.0    # seconds between requests (Mistral's free tier: 1 request per second)


# Pacing per provider, a little below each free tier, so runs rarely hit a limit.
#   Groq free tier, gpt-oss: 30 requests and 8,000 tokens per minute; 8,000 tokens per request.
#   Mistral free tier: 1 request per second, 500,000 tokens per minute, 1 billion tokens per month.
PROVIDER_LIMITS = {
    "groq": Limits(7000, 25, 8000, 6000),
    "mistralai": Limits(450_000, 50, 120_000, 16_000, min_interval=1.1),
}
NO_LIMITS = Limits(0, 0, 0, 16_000)
# Prefix of the environment variables that override a provider's limits, e.g. MISTRAL_RATE_LIMIT_RPM.
ENV_PREFIX = {"groq": "GROQ", "mistralai": "MISTRAL", "openai": "OPENAI", "ollama": "OLLAMA"}


@dataclass(frozen=True)
class Settings:
    # "provider:model" strings, e.g. "groq:openai/gpt-oss-120b"
    model: str = field(default_factory=lambda: _env("MODEL", "groq:openai/gpt-oss-120b"))
    fallback_model: str = field(default_factory=lambda: _env("FALLBACK_MODEL", "groq:openai/gpt-oss-20b"))
    reasoning_effort: str = field(default_factory=lambda: _env("REASONING_EFFORT", "low"))
    max_retries: int = field(default_factory=lambda: int(_env("MAX_RETRIES", "5")))

    def limits(self, spec: str | None = None) -> Limits:
        """Rate and size limits for the provider of ``spec`` ("provider:model").

        Built-in defaults (PROVIDER_LIMITS) can be overridden per provider, e.g.
        ``MISTRAL_RATE_LIMIT_RPM=0`` or ``GROQ_MAX_REQUEST_TOKENS=60000`` on a paid
        plan. The older names without a prefix (RATE_LIMIT_TPM, RATE_LIMIT_RPM,
        MAX_REQUEST_TOKENS, MAX_OUTPUT_TOKENS) still work and apply to Groq.
        """
        provider = (spec or self.model).split(":", 1)[0].strip().lower()
        provider = {"mistral": "mistralai"}.get(provider, provider)
        base = PROVIDER_LIMITS.get(provider, NO_LIMITS)
        prefix = ENV_PREFIX.get(provider, provider.upper())

        def pick(name: str, default) -> int:
            value = os.getenv(f"{prefix}_{name}", "").strip()
            if not value and provider == "groq":
                value = os.getenv(name, "").strip()       # the original, unprefixed names
            return type(default)(value) if value else default

        return Limits(
            tokens_per_minute=pick("RATE_LIMIT_TPM", base.tokens_per_minute),
            requests_per_minute=pick("RATE_LIMIT_RPM", base.requests_per_minute),
            max_request_tokens=pick("MAX_REQUEST_TOKENS", base.max_request_tokens),
            max_output_tokens=pick("MAX_OUTPUT_TOKENS", base.max_output_tokens),
            min_interval=float(os.getenv(f"{prefix}_MIN_INTERVAL", "").strip() or base.min_interval),
        )

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
