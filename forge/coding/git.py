"""Git as the safety net.

Other coding agents edit your files and hope. FORGE treats git as the
compensation layer the runtime already assumes exists:

* a run works on its own branch, never on yours;
* every committed step becomes a git commit, so the agent's history is the
  event log made inspectable with tools you already have;
* `edit_file` is a REVERSIBLE_WRITE whose compensator is a git restore, which
  means a failed or mismatched edit is undone by the runtime rather than left
  for you to find;
* the whole run is one `git branch -D` away from never having happened.

That is the honest answer to "how do I trust a small model with my code": you
do not have to. You review a branch.

Refusing to start on a dirty tree is deliberate. If the agent's changes are
mixed with yours, none of the above works - you can no longer tell whose edit
broke the build.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from forge.errors import DeterministicError, ForgeError
from forge.telemetry.logging import get_logger

__all__ = ["GitError", "GitRepo", "GitSession", "NotARepository"]

log = get_logger("forge.coding.git")


class GitError(ForgeError):
    """A git command failed."""


class NotARepository(DeterministicError):
    """The workspace is not a git repository."""


@dataclass
class GitRepo:
    """Thin, synchronous git wrapper.

    Uses the `git` binary rather than a library: it is what the user already
    has, it behaves identically to what they will type when reviewing, and it
    keeps the dependency list at four packages (ADR-0002).
    """

    root: Path
    timeout_s: float = 30.0

    def __post_init__(self) -> None:
        self.root = Path(self.root).resolve()

    # -- plumbing ----------------------------------------------------------

    def run(self, *args: str, check: bool = True) -> str:
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
            )
        except FileNotFoundError as exc:
            raise GitError(
                "git is not installed or not on PATH; the coding agent needs it "
                "for its safety net"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise GitError(f"git {args[0]} timed out after {self.timeout_s}s") from exc

        if check and proc.returncode != 0:
            raise GitError(
                f"git {' '.join(args)} failed: {proc.stderr.strip()[:400]}",
                returncode=proc.returncode,
            )
        return proc.stdout

    # -- state -------------------------------------------------------------

    @property
    def is_repo(self) -> bool:
        try:
            return self.run("rev-parse", "--is-inside-work-tree").strip() == "true"
        except GitError:
            return False

    def toplevel(self) -> Path | None:
        """The root of the repository this directory belongs to, if any."""
        try:
            out = self.run("rev-parse", "--show-toplevel").strip()
        except GitError:
            return None
        return Path(out).resolve() if out else None

    @property
    def is_repo_root(self) -> bool:
        top = self.toplevel()
        return top is not None and top == self.root

    def require_repo(self) -> None:
        """Demand a repository *rooted here*, not merely one somewhere above.

        `--is-inside-work-tree` is true for any descendant of a repository, so
        checking only that would let the agent branch and commit a parent repo
        the user never pointed at - sweeping unrelated work into its commits.
        A scratch directory inside a monorepo, or anywhere under a home
        directory that happens to be versioned, hits this immediately.
        """
        top = self.toplevel()
        if top is None:
            raise NotARepository(
                f"{self.root} is not a git repository. Run `git init` first - the "
                "coding agent uses git to make its edits reversible, and without "
                "it there is no safety net."
            )
        if top != self.root:
            raise NotARepository(
                f"{self.root} is inside the repository at {top}, but is not its "
                f"root.\n\nThe agent branches and commits whole repositories, so "
                f"running here would sweep unrelated work into its commits. "
                f"Either:\n"
                f"  run from the repository root:  forge code --repo {top} \"...\"\n"
                f"  or make this its own repo:     git init {self.root}"
            )

    def current_branch(self) -> str:
        return self.run("rev-parse", "--abbrev-ref", "HEAD").strip()

    def head(self) -> str:
        return self.run("rev-parse", "HEAD").strip()

    def is_clean(self) -> bool:
        return not self.run("status", "--porcelain").strip()

    def dirty_files(self) -> list[str]:
        return [
            line[3:].strip()
            for line in self.run("status", "--porcelain").splitlines()
            if line.strip()
        ]

    def has_commits(self) -> bool:
        try:
            self.run("rev-parse", "HEAD")
        except GitError:
            return False
        return True

    def diff(self, *, staged: bool = False, base: str | None = None) -> str:
        args = ["diff"]
        if staged:
            args.append("--cached")
        if base:
            args.append(base)
        return self.run(*args)

    def diff_stat(self, base: str) -> str:
        return self.run("diff", "--stat", base)

    # -- mutation ----------------------------------------------------------

    def create_branch(self, name: str) -> None:
        self.run("checkout", "-b", name)

    def checkout(self, ref: str) -> None:
        self.run("checkout", ref)

    def delete_branch(self, name: str, *, force: bool = True) -> None:
        self.run("branch", "-D" if force else "-d", name)

    def commit_all(self, message: str) -> str | None:
        """Stage and commit everything. Returns the sha, or None if nothing changed."""
        self.run("add", "-A")
        if not self.run("diff", "--cached", "--name-only").strip():
            return None
        # No hooks and no signing: a commit made by the agent must not be able
        # to run arbitrary hook scripts, and must not carry the user's
        # signature as if they had authored it.
        self.run(
            "-c", "user.name=forge-agent",
            "-c", "user.email=forge-agent@localhost",
            "commit", "--no-verify", "--no-gpg-sign", "-m", message,
        )
        return self.head()

    def exclude_runtime_state(self) -> None:
        """Keep FORGE's own state out of the user's commits.

        The event store lives at `.forge/` by default, and `git add -A` would
        otherwise sweep the database into the agent's commits - a binary blob
        in every diff, growing every step. Written to `.git/info/exclude`
        rather than `.gitignore`: it is this checkout's business, and editing
        a tracked file to make room for ourselves would be rude.
        """
        exclude = self.root / ".git" / "info" / "exclude"
        # `.forge/` is our event store. The cache directories are here because
        # the agent's own `run_tests` creates them: leaving them out would mean
        # running the tests once makes the tree dirty and blocks the next run.
        # Scoped to `.git/info/exclude`, so the user's `.gitignore` is untouched.
        entries = (
            "/.forge/", "*.db-wal", "*.db-shm",
            "__pycache__/", ".pytest_cache/", ".ruff_cache/", ".mypy_cache/",
        )
        try:
            exclude.parent.mkdir(parents=True, exist_ok=True)
            current = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
            missing = [e for e in entries if e not in current]
            if missing:
                suffix = "" if current.endswith("\n") or not current else "\n"
                exclude.write_text(
                    current + suffix + "\n# added by forge: agent runtime state\n"
                    + "\n".join(missing) + "\n",
                    encoding="utf-8",
                )
        except OSError as exc:  # pragma: no cover - a read-only .git is unusual
            log.warning("could not write .git/info/exclude", error=str(exc))

    def restore(self, relative: str) -> None:
        """Undo working-tree changes to one path. The compensator's mechanism."""
        self.run("checkout", "--", relative, check=False)

    def reset_hard(self, ref: str) -> None:
        self.run("reset", "--hard", ref)


@dataclass
class GitSession:
    """One agent run's git lifecycle.

    Start on a fresh branch off the current HEAD, commit each committed step,
    and leave the branch behind for review. Nothing here touches the user's
    branch, and nothing is pushed.
    """

    repo: GitRepo
    run_id: str
    branch_prefix: str = "forge"
    base_ref: str = field(default="", init=False)
    branch: str = field(default="", init=False)
    original_branch: str = field(default="", init=False)
    commits: list[str] = field(default_factory=list, init=False)
    started: bool = field(default=False, init=False)

    def start(self, *, allow_dirty: bool = False) -> None:
        self.repo.require_repo()
        # Before the cleanliness check, not after: the runtime state this
        # excludes is exactly what would otherwise make the tree look dirty.
        self.repo.exclude_runtime_state()

        if not self.repo.has_commits():
            raise NotARepository(
                "this repository has no commits yet. Make one first - the agent "
                "branches from HEAD, and there is nothing to branch from."
            )

        if not self.repo.is_clean() and not allow_dirty:
            dirty = self.repo.dirty_files()[:8]
            raise DeterministicError(
                "the working tree has uncommitted changes: "
                + ", ".join(dirty)
                + ". Commit or stash them first, so the agent's edits stay "
                "separable from yours. (--allow-dirty to override.)"
            )

        self.original_branch = self.repo.current_branch()
        self.base_ref = self.repo.head()
        self.branch = f"{self.branch_prefix}/{self.run_id}"
        self.repo.create_branch(self.branch)
        self.started = True
        log.info(
            "git session started",
            branch=self.branch, base=self.base_ref[:8], from_branch=self.original_branch,
        )

    def commit_step(self, step_index: int, summary: str) -> str | None:
        """Commit whatever the step changed. Returns the sha, or None."""
        if not self.started:
            return None
        subject = summary.strip().splitlines()[0][:72] if summary.strip() else "agent step"
        message = f"forge step {step_index}: {subject}\n\nrun: {self.run_id}"
        sha = self.repo.commit_all(message)
        if sha:
            self.commits.append(sha)
            log.info("step committed", step=step_index, sha=sha[:8])
        return sha

    def summary(self) -> dict[str, object]:
        if not self.started:
            return {"branch": None, "commits": 0}
        return {
            "branch": self.branch,
            "base": self.base_ref,
            "commits": len(self.commits),
            "diff_stat": self.repo.diff_stat(self.base_ref).strip(),
        }

    def abandon(self) -> None:
        """Throw the whole run away: back to the original branch, branch deleted."""
        if not self.started:
            return
        self.repo.reset_hard(self.base_ref)
        self.repo.checkout(self.original_branch)
        self.repo.delete_branch(self.branch)
        log.info("run abandoned", branch=self.branch)
        self.started = False

    def review_hint(self) -> str:
        if not self.started:
            return ""
        return (
            f"    git diff {self.base_ref[:8]}..{self.branch}      review the changes\n"
            f"    git checkout {self.original_branch} && git merge {self.branch}"
            "      keep them\n"
            f"    git checkout {self.original_branch} && "
            f"git branch -D {self.branch}      discard them"
        )
