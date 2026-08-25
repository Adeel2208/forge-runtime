"""Deployment configuration (12-factor).

Precedence, lowest to highest: built-in defaults, a `forge.toml` file, then
environment variables. Nothing here reads a network or a secret store; secrets
arrive as environment variables and are never written to the event log.

    from forge import ForgeConfig
    config = ForgeConfig.load()                    # ./forge.toml + FORGE_*
    config = ForgeConfig.load("deploy/prod.toml")

The point of this module is that changing a deployment must never require
editing Python. Model choice, provider order, spend ceilings, database URL and
policy bundle are all operational settings, not code.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

__all__ = ["ForgeConfig", "ProviderConfig", "BudgetConfig", "TelemetryConfig"]

ENV_PREFIX = "FORGE_"


@dataclass(frozen=True)
class ProviderConfig:
    """One entry in the routing chain, tried in declaration order."""

    kind: str = "mock"
    """`mock`, `ollama`, or `openai` (any OpenAI-compatible endpoint)."""

    model: str = "mock-1"
    base_url: str | None = None
    api_key_env: str | None = None
    """Name of the env var holding the key. Never the key itself."""

    input_per_1k: float = 0.0
    output_per_1k: float = 0.0
    timeout_s: float = 60.0
    num_ctx: int = 8192

    @property
    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env) if self.api_key_env else None


@dataclass(frozen=True)
class BudgetConfig:
    max_usd: float = 5.0
    max_tokens: int = 250_000
    max_steps: int = 24
    max_tool_calls: int = 32
    max_wall_clock_s: float = 1800.0


@dataclass(frozen=True)
class TelemetryConfig:
    otel: bool = False
    """Mirror spans to an installed OpenTelemetry SDK."""

    service_name: str = "forge"
    redact: bool = True


@dataclass(frozen=True)
class ForgeConfig:
    database_url: str = "sqlite:///.forge/forge.db"
    policy_bundle: str | None = None
    """Path to a policy YAML. None uses the packaged default bundle."""

    providers: tuple[ProviderConfig, ...] = (ProviderConfig(),)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)

    tools: tuple[str, ...] = ()
    """Default tool allow-list for tasks that do not specify one."""

    max_concurrent_runs: int = 4
    checkpoint_every: int = 1
    seed: int = 1729

    # -- loading -----------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path | None = None, *, env: dict[str, str] | None = None) -> ForgeConfig:
        """Build a config from an optional TOML file, overlaid with env vars."""
        config = cls()
        toml_path = Path(path) if path else Path("forge.toml")
        if toml_path.exists():
            config = config.merge(tomllib.loads(toml_path.read_text(encoding="utf-8")))
        return config.with_env(env if env is not None else dict(os.environ))

    def merge(self, data: dict[str, Any]) -> ForgeConfig:
        """Overlay a parsed TOML mapping. Unknown keys are ignored, not fatal:
        a config written for a newer FORGE must not break an older one."""
        budget = self.budget
        if isinstance(data.get("budget"), dict):
            budget = replace(budget, **_known(BudgetConfig, data["budget"]))

        telemetry = self.telemetry
        if isinstance(data.get("telemetry"), dict):
            telemetry = replace(telemetry, **_known(TelemetryConfig, data["telemetry"]))

        providers = self.providers
        raw_providers = data.get("providers") or data.get("provider")
        if isinstance(raw_providers, list) and raw_providers:
            providers = tuple(
                ProviderConfig(**_known(ProviderConfig, p))
                for p in raw_providers
                if isinstance(p, dict)
            )

        top = _known(ForgeConfig, data, skip={"budget", "telemetry", "providers", "provider"})
        if "tools" in top and isinstance(top["tools"], list):
            top["tools"] = tuple(top["tools"])

        return replace(self, budget=budget, telemetry=telemetry, providers=providers, **top)

    def with_env(self, env: dict[str, str]) -> ForgeConfig:
        """Apply `FORGE_*` overrides.

        Only scalars are settable this way; provider chains belong in a file,
        because an ordered list of dictionaries encoded into an env var is a
        configuration format nobody should have to debug at 3am.
        """
        scalars: dict[str, Any] = {}
        if v := env.get(f"{ENV_PREFIX}DATABASE_URL"):
            scalars["database_url"] = v
        if v := env.get(f"{ENV_PREFIX}POLICY_BUNDLE"):
            scalars["policy_bundle"] = v
        if v := env.get(f"{ENV_PREFIX}TOOLS"):
            scalars["tools"] = tuple(t.strip() for t in v.split(",") if t.strip())
        if v := env.get(f"{ENV_PREFIX}SEED"):
            scalars["seed"] = int(v)
        if v := env.get(f"{ENV_PREFIX}MAX_CONCURRENT_RUNS"):
            scalars["max_concurrent_runs"] = int(v)

        budget = self.budget
        for key, cast in (
            ("MAX_USD", float), ("MAX_TOKENS", int), ("MAX_STEPS", int),
            ("MAX_TOOL_CALLS", int), ("MAX_WALL_CLOCK_S", float),
        ):
            if v := env.get(f"{ENV_PREFIX}{key}"):
                budget = replace(budget, **{key.lower(): cast(v)})

        telemetry = self.telemetry
        if v := env.get(f"{ENV_PREFIX}OTEL"):
            telemetry = replace(telemetry, otel=v.strip().lower() in ("1", "true", "yes", "on"))
        if v := env.get(f"{ENV_PREFIX}SERVICE_NAME"):
            telemetry = replace(telemetry, service_name=v)

        # A single provider can be set from the environment - the common case
        # for a container that runs one model.
        providers = self.providers
        if kind := env.get(f"{ENV_PREFIX}PROVIDER"):
            providers = (
                ProviderConfig(
                    kind=kind,
                    model=env.get(f"{ENV_PREFIX}MODEL", "mock-1"),
                    base_url=env.get(f"{ENV_PREFIX}BASE_URL"),
                    api_key_env=env.get(f"{ENV_PREFIX}API_KEY_ENV"),
                ),
            )

        return replace(self, budget=budget, telemetry=telemetry, providers=providers, **scalars)

    # -- derived -----------------------------------------------------------

    @property
    def sqlite_path(self) -> Path:
        """Filesystem path for a `sqlite://` URL."""
        url = self.database_url
        if not url.startswith("sqlite:"):
            raise ValueError(f"not a sqlite url: {url!r}")
        return Path(url.split("///", 1)[-1] if "///" in url else url.split(":", 1)[-1])

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite:")

    def describe(self) -> dict[str, Any]:
        """Redacted summary, safe to log or serve from an endpoint."""
        return {
            "database_url": self.database_url,
            "policy_bundle": self.policy_bundle or "packaged default",
            "providers": [
                {"kind": p.kind, "model": p.model, "priced": bool(p.input_per_1k or p.output_per_1k)}
                for p in self.providers
            ],
            "budget": {
                "max_usd": self.budget.max_usd,
                "max_tokens": self.budget.max_tokens,
                "max_steps": self.budget.max_steps,
            },
            "tools": list(self.tools),
            "max_concurrent_runs": self.max_concurrent_runs,
        }


def _known(cls: type, data: dict[str, Any], *, skip: set[str] | None = None) -> dict[str, Any]:
    """Keep only keys that are real fields of `cls`."""
    import dataclasses

    fields = {f.name for f in dataclasses.fields(cls)} - (skip or set())
    return {k: v for k, v in data.items() if k in fields}
