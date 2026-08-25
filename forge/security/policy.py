"""The policy engine (spec §13).

Sits between PROPOSE and DISPATCH. Every proposal is evaluated; every decision
- allow, deny or escalate - is returned as a `PolicyDecision` and written to
the event log by the caller. There is no code path that dispatches without one.

Ordering of checks is deliberate: cheapest and most categorical first, so a
denial reason names the *first* thing that was wrong rather than an incidental
downstream symptom.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from forge.core.contracts import PolicyDecision
from forge.core.enums import Decision, RiskClass, SideEffect
from forge.errors import BudgetExhausted
from forge.security.budget import Budget
from forge.security.capabilities import CapabilityGrant
from forge.tools.registry import ToolSpec

__all__ = ["PolicyBundle", "PolicyEngine"]

_EFFECT_BY_NAME = {e.value: e for e in SideEffect}


class PolicyBundle:
    """A versioned set of grants and limits.

    Versioned because §9 requires reproducibility: a run records which bundle
    authorized it, so a later "why was that allowed?" has an exact answer.
    """

    def __init__(
        self,
        *,
        version: str,
        capabilities: dict[str, CapabilityGrant],
        budget: Budget | None = None,
        require_approval_for: frozenset[SideEffect] = frozenset({SideEffect.IRREVERSIBLE_WRITE}),
        allow_dry_run_bypass: bool = True,
    ) -> None:
        self.version = version
        self.capabilities = capabilities
        self.budget = budget or Budget()
        self.require_approval_for = require_approval_for
        self.allow_dry_run_bypass = allow_dry_run_bypass

    # -- factories ---------------------------------------------------------

    @classmethod
    def baseline(cls, *, granted: list[str] | None = None, **budget_kwargs: Any) -> PolicyBundle:
        """A conservative starting bundle: inference on, every tool opt-in.

        Mirrors `forge/security/policies/default.yaml`. Intended as the base a
        deployment narrows or extends, not as a permissive fallback - a
        capability that is not named here is not granted.
        """
        caps: dict[str, CapabilityGrant] = {
            "INFERENCE": CapabilityGrant(name="INFERENCE", granted=True),
        }
        for name in granted or []:
            caps[name] = CapabilityGrant(
                name=name,
                granted=True,
                allowed_effects=frozenset(
                    {SideEffect.READ, SideEffect.REVERSIBLE_WRITE, SideEffect.IRREVERSIBLE_WRITE}
                ),
            )
        return cls(
            version="baseline/1.0.0",
            capabilities=caps,
            budget=Budget(**budget_kwargs),
        )

    @classmethod
    def from_yaml(cls, path: str | Path) -> PolicyBundle:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        caps: dict[str, CapabilityGrant] = {}
        for name, spec in (raw.get("capabilities") or {}).items():
            spec = spec or {}
            effects = spec.get("allowed_effects") or ["READ"]
            caps[name] = CapabilityGrant(
                name=name,
                granted=bool(spec.get("granted", False)),
                max_invocations=spec.get("max_invocations"),
                allowed_effects=frozenset(_EFFECT_BY_NAME[e] for e in effects),
                requires_approval=bool(spec.get("requires_approval", False)),
            )
        budget_spec = raw.get("budget") or {}
        budget = Budget(
            max_steps=int(budget_spec.get("max_steps", 24)),
            max_tool_calls=int(budget_spec.get("max_tool_calls", 32)),
            max_tokens=int(budget_spec.get("tokens_per_run", 250_000)),
            max_wall_clock_s=float(budget_spec.get("wall_clock_seconds", 1800)),
            max_usd=float(budget_spec.get("usd_ceiling", 0.0)),
        )
        approval = raw.get("require_approval_for") or ["IRREVERSIBLE_WRITE"]
        return cls(
            version=str(raw.get("version", "unversioned")),
            capabilities=caps,
            budget=budget,
            require_approval_for=frozenset(_EFFECT_BY_NAME[e] for e in approval),
        )


class PolicyEngine:
    """Evaluates one proposed tool action against a bundle. Deny by default."""

    def __init__(self, bundle: PolicyBundle) -> None:
        self.bundle = bundle

    @property
    def version(self) -> str:
        return self.bundle.version

    def authorize_tool(
        self,
        *,
        spec: ToolSpec,
        arguments: dict[str, Any],
        task_allow_list: list[str],
        invocations_used: int = 0,
        dry_run: bool = False,
    ) -> PolicyDecision:
        del arguments  # reserved: data-access policies inspect these

        # 1. Task scope. The task must have opted into this tool by name.
        if spec.name not in task_allow_list:
            return self._deny(
                f"tool {spec.name!r} is not in the task's allow-list",
                capability=spec.capability,
                risk=spec.risk,
            )

        # 2. Capability existence. An undeclared capability is not an implicit one.
        grant = self.bundle.capabilities.get(spec.capability)
        if grant is None:
            return self._deny(
                f"capability {spec.capability!r} is not declared in policy "
                f"{self.bundle.version}",
                capability=spec.capability,
                risk=spec.risk,
            )

        # 3. Grant state.
        if not grant.granted:
            return self._deny(
                f"capability {spec.capability!r} is not granted",
                capability=spec.capability,
                risk=spec.risk,
            )

        # 4. Effect class. A read capability cannot authorize a write.
        if not grant.permits(spec.side_effect):
            return self._deny(
                f"capability {spec.capability!r} does not permit "
                f"{spec.side_effect.value}",
                capability=spec.capability,
                risk=spec.risk,
            )

        # 5. Invocation ceiling.
        if grant.max_invocations is not None and invocations_used >= grant.max_invocations:
            return self._deny(
                f"capability {spec.capability!r} exhausted "
                f"({invocations_used}/{grant.max_invocations})",
                capability=spec.capability,
                risk=spec.risk,
            )

        # 6. Budget.
        try:
            self.bundle.budget.check()
        except BudgetExhausted as exc:
            return self._deny(exc.reason or exc.message, capability=spec.capability, risk=spec.risk)

        # 7. Human approval for irreversible effects - unless this is a dry run,
        #    which by definition produces no effect to approve.
        needs_approval = (
            spec.side_effect in self.bundle.require_approval_for or grant.requires_approval
        )
        if needs_approval and not (dry_run and self.bundle.allow_dry_run_bypass):
            return PolicyDecision(
                decision=Decision.REQUIRE_APPROVAL,
                reason=f"{spec.side_effect.value} requires human approval",
                policy_version=self.bundle.version,
                capability=spec.capability,
                risk=RiskClass.HIGH,
                obligations=["human_approval"],
            )

        return PolicyDecision(
            decision=Decision.ALLOW,
            reason="all checks passed",
            policy_version=self.bundle.version,
            capability=spec.capability,
            risk=spec.risk,
            obligations=["dry_run"] if dry_run else [],
        )

    def authorize_inference(self, *, projected_usd: float = 0.0) -> PolicyDecision:
        """Gate a model call on capability and remaining spend.

        Cost is evaluated *before* the request leaves the process, so a run
        cannot discover it is over budget by having already gone over.
        """
        grant = self.bundle.capabilities.get("INFERENCE")
        if grant is None or not grant.granted:
            return self._deny("capability 'INFERENCE' is not granted", capability="INFERENCE")

        budget = self.bundle.budget
        if round(budget.usd + projected_usd, 10) > budget.max_usd:
            return self._deny(
                f"projected spend ${budget.usd + projected_usd:.4f} exceeds the run "
                f"ceiling of ${budget.max_usd:.2f}",
                capability="INFERENCE",
            )
        return PolicyDecision(
            decision=Decision.ALLOW,
            reason="inference permitted",
            policy_version=self.bundle.version,
            capability="INFERENCE",
        )

    def _deny(
        self, reason: str, *, capability: str | None = None, risk: RiskClass = RiskClass.LOW
    ) -> PolicyDecision:
        return PolicyDecision(
            decision=Decision.DENY,
            reason=reason,
            policy_version=self.bundle.version,
            capability=capability,
            risk=risk,
        )
