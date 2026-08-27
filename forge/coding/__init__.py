"""The coding agent: a local-first agent that edits code under runtime control.

    from forge.coding import CodingAgent

    agent = CodingAgent(".")
    result = await agent.run("Add a --verbose flag to the CLI")
    print(result.review_hint)

Every run works on its own git branch, every committed step is a commit, and
every edit is compensated by a git restore if it goes wrong. The agent cannot
write outside the repository, cannot touch `.git` or `.env`, and cannot run
shell commands without an explicit grant.

The honest framing: this does not make a small model as capable as a frontier
one. It makes a small model's mistakes cheap and reviewable, which is what
makes running one against your repository a reasonable thing to do.
"""

from __future__ import annotations

from forge.coding.agent import CODING_SYSTEM_PROMPT, CodingAgent, CodingResult
from forge.coding.git import GitError, GitRepo, GitSession, NotARepository
from forge.coding.tools import CodingContext, build_coding_registry
from forge.coding.workspace import Workspace, WorkspaceError

__all__ = [  # noqa: RUF022 - grouped by concern, not alphabetised
    # entry point
    "CodingAgent",
    "CodingResult",
    "CODING_SYSTEM_PROMPT",
    # workspace
    "Workspace",
    "WorkspaceError",
    # git
    "GitRepo",
    "GitSession",
    "GitError",
    "NotARepository",
    # tools
    "build_coding_registry",
    "CodingContext",
]
