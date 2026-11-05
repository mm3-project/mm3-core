"""Rebase pull requests that were stacked on a pull request merged with squash.

Implements:
    CORE-75: Core spec 0.6, §10.3 Documentation and quality.

A code PR is often opened on top of its spec sync PR (CORE-75). When the spec
PR is merged with Squash and merge, main gets one new commit, and the code
PR still carries the spec PR's original commits, so GitHub reports a
conflict. For every open PR whose branch contains the merged PR's last
commit, this tool moves only the PR's own commits onto main::

    git rebase --onto origin/main <merged head> <branch>

Conflicts in generated files (``docs/spec/coverage.md``) are resolved by
regenerating them; any other conflict aborts the rebase and is reported, so
a person (or Claude) resolves it by hand. After a rebase the generated files
are regenerated once more and committed when they changed, then the branch
is pushed with ``--force-with-lease`` against the commit it had before.

Usage (the Restack workflow)::

    python tools/restack.py --merged-sha SHA --merged-number N --prs prs.txt --push --comment

``prs.txt`` has one ``<number> <branch>`` line per open pull request on main.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

GENERATED = ("docs/spec/coverage.md",)
REGENERATE: tuple[tuple[str, ...], ...] = (
    ("uv", "run", "python", "tools/trace_report.py", "--out", "docs/spec/coverage.md"),
)


@dataclass
class Outcome:
    """What happened to one pull request."""

    number: int
    branch: str
    status: str  # rebased | not_stacked | conflict | push_failed
    conflicts: list[str] = field(default_factory=list)
    regenerated: bool = False
    head: str | None = None


class Git:
    """Run git in one working tree."""

    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        """Run ``git args`` and capture its output."""
        env = {**os.environ, "GIT_EDITOR": "true"}
        return subprocess.run(  # noqa: S603 - fixed git arguments
            ["git", *args],  # noqa: S607 - git from PATH, as in CI
            cwd=self.cwd,
            env=env,
            check=check,
            capture_output=True,
            text=True,
        )

    def out(self, *args: str) -> str:
        """Run git and return its standard output, stripped."""
        return self.run(*args).stdout.strip()

    def ok(self, *args: str) -> bool:
        """Run git and tell whether it succeeded."""
        return self.run(*args, check=False).returncode == 0

    def conflicts(self) -> list[str]:
        """Files with unresolved conflicts."""
        return [p for p in self.out("diff", "--name-only", "--diff-filter=U").splitlines() if p]


def regenerate(git: Git, commands: Sequence[Sequence[str]]) -> None:
    """Rebuild the generated files in the working tree."""
    for cmd in commands:
        subprocess.run(list(cmd), cwd=git.cwd, check=True, capture_output=True)  # noqa: S603


def restack(
    git: Git,
    number: int,
    branch: str,
    merged_sha: str,
    *,
    base: str = "origin/main",
    remote: str = "origin",
    commands: Sequence[Sequence[str]] = REGENERATE,
    push: bool = False,
    regen: Callable[[Git, Sequence[Sequence[str]]], None] | None = None,
) -> Outcome:
    """Move the commits of ``branch`` that are not in the merged PR onto ``base``.

    Args:
        git: The working tree (fetched, clean).
        number: The pull request number.
        branch: Its head branch on ``remote``.
        merged_sha: Head commit of the pull request that was merged.
        base: What to rebase onto.
        remote: The remote of the branches.
        commands: How to rebuild the generated files.
        push: Push the result with ``--force-with-lease``.
        regen: Runs ``commands``; :func:`regenerate` by default (replaced in tests).

    Returns:
        The outcome.
    """
    rebuild = regen or regenerate
    ref = f"{remote}/{branch}"
    old = git.out("rev-parse", ref)
    if old == merged_sha or not git.ok("merge-base", "--is-ancestor", merged_sha, ref):
        return Outcome(number, branch, "not_stacked")
    git.run("checkout", "-q", "-B", branch, ref)
    outcome = Outcome(number, branch, "rebased")
    result = git.run("rebase", "--onto", base, merged_sha, check=False)
    while result.returncode != 0:
        conflicts = git.conflicts()
        if not conflicts or any(p not in GENERATED for p in conflicts):
            git.run("rebase", "--abort", check=False)
            outcome.status = "conflict"
            outcome.conflicts = conflicts
            git.run("checkout", "-q", "--detach", base)
            return outcome
        rebuild(git, commands)
        git.run("add", *conflicts)
        outcome.regenerated = True
        result = git.run("-c", "core.editor=true", "rebase", "--continue", check=False)
    rebuild(git, commands)
    if git.out("status", "--porcelain", "--", *GENERATED):
        git.run("add", *GENERATED)
        git.run("commit", "-q", "-m", "chore(spec): regenerate coverage after restack")
        outcome.regenerated = True
    outcome.head = git.out("rev-parse", "HEAD")
    if push:
        pushed = git.run(
            "push", f"--force-with-lease={branch}:{old}", remote, f"HEAD:{branch}", check=False
        )
        if pushed.returncode != 0:
            outcome.status = "push_failed"
    git.run("checkout", "-q", "--detach", base)
    return outcome


def comment_text(o: Outcome, merged_number: int) -> str | None:
    """The comment for a pull request, or ``None`` when nothing happened."""
    if o.status == "rebased":
        extra = " The generated spec files were rebuilt." if o.regenerated else ""
        return (
            f"Rebased onto main after #{merged_number} was merged with squash: only this "
            f"pull request's own commits are left.{extra} CI runs again on the new head."
        )
    if o.status == "conflict":
        files = ", ".join(f"`{p}`" for p in o.conflicts) or "unknown files"
        return (
            f"#{merged_number} was merged, but the automatic rebase onto main stopped on a "
            f"conflict in {files}. Rebase by hand (CONTRIBUTING.md, *Stacked pull requests*)."
        )
    if o.status == "push_failed":
        return (
            f"#{merged_number} was merged and this branch was rebased, but the push was "
            "refused (the branch changed meanwhile). Rebase by hand."
        )
    return None


def post_comment(repo: str, number: int, body: str) -> None:
    """Comment on a pull request with the GitHub CLI."""
    subprocess.run(  # noqa: S603
        ["gh", "api", f"repos/{repo}/issues/{number}/comments", "-f", f"body={body}"],  # noqa: S607
        check=True,
        capture_output=True,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the tool."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--merged-sha", required=True)
    parser.add_argument("--merged-number", type=int, required=True)
    parser.add_argument("--prs", type=Path, required=True, help="lines of '<number> <branch>'")
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("--push", action="store_true")
    parser.add_argument("--comment", action="store_true")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    args = parser.parse_args(argv)
    git = Git(Path.cwd())
    outcomes: list[Outcome] = []
    for line in args.prs.read_text().splitlines():
        if not line.strip():
            continue
        number, branch = line.split(maxsplit=1)
        o = restack(
            git, int(number), branch.strip(), args.merged_sha, base=args.base, push=args.push
        )
        outcomes.append(o)
        body = comment_text(o, args.merged_number)
        if args.comment and body and args.repo:
            post_comment(args.repo, o.number, body)
    print(json.dumps([asdict(o) for o in outcomes], indent=2))
    return 1 if any(o.status in ("conflict", "push_failed") for o in outcomes) else 0


if __name__ == "__main__":
    sys.exit(main())
