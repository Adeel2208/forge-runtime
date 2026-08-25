"""Execution plane: the lifecycle machine and the loop that drives it."""

from __future__ import annotations

from forge.runtime.loop import AgentRuntime, RunResult, RuntimeConfig
from forge.runtime.machine import TRANSITIONS, assert_transition, is_terminal
from forge.runtime.reconcile import Verdict, reconcile

__all__ = [
    "TRANSITIONS",
    "AgentRuntime",
    "RunResult",
    "RuntimeConfig",
    "Verdict",
    "assert_transition",
    "is_terminal",
    "reconcile",
]
