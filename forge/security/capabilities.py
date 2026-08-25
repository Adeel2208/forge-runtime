"""Capabilities and permits (spec §12, §13).

A capability is a *standing* grant ("this run may read files"). A permit is a
*single-use* authorization bound to one concrete action hash. Separating them
is what stops a permit issued for `read_note("a.txt")` being replayed against
`delete_everything()`: the hash will not match, and the dispatcher refuses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from forge.core.contracts import Permit
from forge.core.enums import SideEffect
from forge.errors import PolicyDenied

__all__ = ["CapabilityGrant", "PermitBook"]


@dataclass(frozen=True)
class CapabilityGrant:
    """A standing permission, optionally bounded in count and effect class."""

    name: str
    granted: bool = False
    max_invocations: int | None = None
    allowed_effects: frozenset[SideEffect] = field(
        default_factory=lambda: frozenset({SideEffect.READ})
    )
    requires_approval: bool = False

    def permits(self, side_effect: SideEffect) -> bool:
        return self.granted and side_effect in self.allowed_effects


class PermitBook:
    """Issues and redeems single-use permits.

    Redemption is destructive by design: a permit cannot be presented twice,
    so a retry loop that forgets to request a fresh authorization fails loudly
    instead of quietly re-executing a write.
    """

    def __init__(self) -> None:
        self._open: dict[str, Permit] = {}
        self._redeemed: set[str] = set()
        self._issued_count: dict[str, int] = {}

    def issue(
        self,
        *,
        run_id: str,
        step_id: str,
        capability: str,
        action_hash: str,
        side_effect: SideEffect,
    ) -> Permit:
        permit = Permit(
            run_id=run_id,
            step_id=step_id,
            capability=capability,
            action_hash=action_hash,
            side_effect=side_effect,
            issued_at=datetime.now(UTC),
        )
        self._open[permit.id] = permit
        self._issued_count[capability] = self._issued_count.get(capability, 0) + 1
        return permit

    def redeem(self, permit_id: str, *, action_hash: str) -> Permit:
        """Consume a permit. Raises `PolicyDenied` on any mismatch."""
        if permit_id in self._redeemed:
            raise PolicyDenied(
                "permit already redeemed",
                reason="single-use permit replayed",
                permit_id=permit_id,
            )
        permit = self._open.get(permit_id)
        if permit is None:
            raise PolicyDenied(
                "no such permit", reason="permit forged or expired", permit_id=permit_id
            )
        if permit.action_hash != action_hash:
            raise PolicyDenied(
                "permit does not authorize this action",
                reason="action hash mismatch",
                permit_id=permit_id,
                expected=permit.action_hash,
                got=action_hash,
            )
        self._redeemed.add(permit_id)
        del self._open[permit_id]
        return permit

    def invocations(self, capability: str) -> int:
        return self._issued_count.get(capability, 0)

    @property
    def outstanding(self) -> int:
        return len(self._open)

    def expire_step(self, step_id: str) -> int:
        """Drop permits scoped to a finished step. Returns how many were dropped."""
        stale = [pid for pid, p in self._open.items() if p.step_id == step_id and p.expires_after_step]
        for pid in stale:
            del self._open[pid]
        return len(stale)
