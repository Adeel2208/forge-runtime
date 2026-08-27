"""A target that drives the coding agent against a throwaway repository.

Coding capability has to be measured, not asserted. This makes a coding task
a first-class case: each one gets a fresh git repository seeded from a
fixture, the agent runs against it, and the observation carries the resulting
diff and test outcome so graders can assert on what actually changed rather
than on what the model said it did.

Hermetic by construction. Every case gets its own repository in a scratch
directory, so a case that corrupts a file cannot affect the next one, and
running the set in any order gives the same answer.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from forge.eval.cases import Case
from forge.eval.targets import Observation, TargetUnavailable

__all__ = ["CodingTarget", "seed_repository"]


def seed_repository(root: Path, files: dict[str, str]) -> None:
    """Write files and make one commit, so the agent has a base to branch from."""
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")

    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=root, check=True, capture_output=True, text=True
        )

    git("init", "-q")
    git("config", "user.email", "eval@forge.local")
    git("config", "user.name", "forge-eval")
    git("add", "-A")
    git("commit", "-q", "-m", "seed")


class CodingTarget:
    """Runs `CodingAgent` against a fresh repository per case."""

    name = "coding"

    def __init__(
        self,
        *,
        config: Any = None,
        fixtures_dir: str | Path = "cases/repos",
        keep_repos: bool = False,
    ) -> None:
        self._config = config
        self._fixtures = Path(fixtures_dir)
        self._keep = keep_repos
        self._scratch: Path | None = None
        self._version = "coding/unknown"

    @property
    def version(self) -> str:
        return self._version

    async def available(self) -> bool:
        return shutil.which("git") is not None

    async def setup(self) -> None:
        if shutil.which("git") is None:
            raise TargetUnavailable(
                "git is not installed; the coding agent needs it for its safety net"
            )
        self._scratch = Path(tempfile.mkdtemp(prefix="forge-coding-eval-"))

        from forge import __version__
        from forge.config import ForgeConfig

        config = self._config if self._config is not None else ForgeConfig.load()
        model = (
            f"{config.providers[0].kind}/{config.providers[0].model}"
            if config.providers else "none"
        )
        self._version = f"forge/{__version__} {model}"

    async def teardown(self) -> None:
        if self._scratch and self._scratch.exists() and not self._keep:
            shutil.rmtree(self._scratch, ignore_errors=True)
        self._scratch = None

    async def execute(self, case: Case, *, seed: int) -> Observation:
        from dataclasses import replace

        from forge.coding.agent import CodingAgent
        from forge.config import ForgeConfig

        if self._scratch is None:
            raise TargetUnavailable("target not set up")

        repo = self._scratch / case.id.replace("/", "_").replace(".", "_")
        repo.mkdir(parents=True, exist_ok=True)
        seed_repository(repo, self._files_for(case))

        base = self._config if self._config is not None else ForgeConfig.load()
        config = replace(
            base, database_url=f"sqlite:///{repo / '.forge' / 'eval.db'}", seed=seed
        )

        started = time.monotonic()
        agent = CodingAgent(repo, config=config)
        result = await agent.run(case.goal, max_steps=case.max_steps or None)

        # The diff is what the case is really about: graders assert on the
        # code that exists afterwards, not on the model's summary of it.
        diff = agent.repo.diff(base=result.base_ref) if result.changed_anything else ""
        final_files = {
            rel: agent.workspace.read(rel)
            for rel in agent.workspace.walk(limit=80)
            if rel.endswith((".py", ".js", ".ts", ".go", ".rs", ".md", ".txt"))
        }

        events = await self._events(config, result.run.run_id)
        return Observation(
            answer=result.run.answer,
            status=result.run.status.value,
            steps=result.run.steps,
            tokens=result.run.usage.total_tokens,
            usd=result.run.usage.usd,
            duration_ms=int((time.monotonic() - started) * 1000),
            duplicate_effects=result.run.duplicate_effects,
            run_id=result.run.run_id,
            events=events,
            raw={
                "diff": diff,
                "files": final_files,
                "files_touched": result.files_touched,
                "commits": result.commits,
                "tests_run": result.tests_run,
                "tests_passed": result.tests_passed,
                "branch": result.branch,
                "run_error": result.run.error,
            },
        )

    def _files_for(self, case: Case) -> dict[str, str]:
        """Seed files: from a JSON fixture, or a tiny default project."""
        if case.fixture:
            path = self._fixtures / case.fixture
            if not path.exists():
                raise TargetUnavailable(f"repo fixture not found: {path}")
            loaded: dict[str, str] = json.loads(path.read_text(encoding="utf-8"))
            return loaded
        return {
            "src/__init__.py": "",
            "src/calc.py": "def add(a, b):\n    return a + b\n",
            "tests/__init__.py": "",
            "tests/test_calc.py": (
                "from src.calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
            ),
        }

    @staticmethod
    async def _events(config: Any, run_id: str) -> list[dict[str, Any]]:
        from forge.state.sqlite_store import SQLiteEventStore

        store = SQLiteEventStore(config.sqlite_path)
        await store.open()
        try:
            events = await store.read(run_id)
        finally:
            await store.close()
        return [
            {
                "seq": e.seq, "type": e.type.value,
                "step_index": e.step_index, "payload": e.payload,
            }
            for e in events
        ]
