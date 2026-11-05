"""tools/restack.py: rebase pull requests stacked on a PR merged with squash."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import restack


def sh(cwd: Path, *args: str) -> str:
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "HOME": str(cwd),
        "PATH": "/usr/bin:/bin:/usr/local/bin",
    }
    return subprocess.run(  # noqa: S603 - test git calls
        ["git", *args],  # noqa: S607
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def commit(repo: Path, files: dict[str, str], message: str) -> str:
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        sh(repo, "add", name)
    sh(repo, "commit", "-q", "-m", message)
    return sh(repo, "rev-parse", "HEAD")


@pytest.fixture
def repos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """An origin with main, a spec branch and a code branch stacked on it."""
    for k, v in {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }.items():
        monkeypatch.setenv(k, v)
    origin = tmp_path / "origin.git"
    sh(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    work = tmp_path / "work"
    sh(tmp_path, "clone", "-q", str(origin), str(work))
    sh(work, "checkout", "-q", "-b", "main")
    commit(work, {"README": "x\n", "docs/spec/coverage.md": "v0\n"}, "base")
    sh(work, "push", "-q", "origin", "main")
    sh(work, "checkout", "-q", "-b", "spec")
    commit(work, {"docs/spec/core.md": "spec 1\n", "docs/spec/coverage.md": "v1\n"}, "spec 1")
    spec_head = commit(work, {"docs/spec/core.md": "spec 1 wording\n"}, "spec 2")
    sh(work, "push", "-q", "origin", "spec")
    sh(work, "checkout", "-q", "-b", "code")
    commit(work, {"code.py": "print(1)\n", "docs/spec/coverage.md": "v2\n"}, "code")
    sh(work, "push", "-q", "origin", "code")
    # Squash and merge of the spec branch.
    sh(work, "checkout", "-q", "main")
    sh(work, "merge", "-q", "--squash", "spec")
    sh(work, "commit", "-q", "-m", "spec (#1)")
    sh(work, "push", "-q", "origin", "main")
    sh(work, "fetch", "-q", "origin")
    (tmp_path / "spec_head").write_text(spec_head)
    return origin, work


def fake_regen(git: restack.Git, _commands: Sequence[Sequence[str]]) -> None:
    (git.cwd / "docs/spec/coverage.md").write_text("regenerated\n")


def test_core_75_stacked_pr_keeps_only_its_own_commits(repos: tuple[Path, Path]) -> None:
    _origin, work = repos
    spec_head = (work.parent / "spec_head").read_text()
    git = restack.Git(work)
    o = restack.restack(git, 2, "code", spec_head, push=True, regen=fake_regen)
    assert o.status == "rebased"
    assert o.regenerated  # coverage.md conflicted and was rebuilt
    sh(work, "fetch", "-q", "origin")
    log = sh(work, "log", "--format=%s", "origin/main..origin/code").splitlines()
    assert log[-1] == "code"  # the spec commits are gone
    assert "spec 1" not in log
    assert "spec 2" not in log
    assert sh(work, "show", "origin/code:code.py") == "print(1)"
    assert sh(work, "show", "origin/code:docs/spec/core.md") == "spec 1 wording"
    assert sh(work, "show", "origin/code:docs/spec/coverage.md") == "regenerated"
    assert sh(work, "merge-base", "--is-ancestor", "origin/main", "origin/code") == ""
    assert o.head == sh(work, "rev-parse", "origin/code")


def test_core_75_unrelated_pr_is_left_alone(repos: tuple[Path, Path]) -> None:
    _origin, work = repos
    sh(work, "checkout", "-q", "-b", "other", "origin/main")
    commit(work, {"other.txt": "o\n"}, "other")
    sh(work, "push", "-q", "origin", "other")
    sh(work, "fetch", "-q", "origin")
    before = sh(work, "rev-parse", "origin/other")
    spec_head = (work.parent / "spec_head").read_text()
    o = restack.restack(restack.Git(work), 3, "other", spec_head, push=True, regen=fake_regen)
    assert o.status == "not_stacked"
    sh(work, "fetch", "-q", "origin")
    assert sh(work, "rev-parse", "origin/other") == before


def test_core_75_other_conflicts_stop_and_change_nothing(repos: tuple[Path, Path]) -> None:
    _origin, work = repos
    # main changes code.py meanwhile: the stacked commit no longer applies.
    sh(work, "checkout", "-q", "main")
    sh(work, "reset", "-q", "--hard", "origin/main")
    commit(work, {"code.py": "print(0)\n"}, "elsewhere")
    sh(work, "push", "-q", "origin", "main")
    sh(work, "fetch", "-q", "origin")
    before = sh(work, "rev-parse", "origin/code")
    spec_head = (work.parent / "spec_head").read_text()
    o = restack.restack(restack.Git(work), 2, "code", spec_head, push=True, regen=fake_regen)
    assert o.status == "conflict"
    assert "code.py" in o.conflicts
    sh(work, "fetch", "-q", "origin")
    assert sh(work, "rev-parse", "origin/code") == before
    assert "conflict" in (restack.comment_text(o, 1) or "")


def test_core_75_comments() -> None:
    o = restack.Outcome(5, "b", "rebased", regenerated=True)
    assert "#1" in (restack.comment_text(o, 1) or "")
    assert restack.comment_text(restack.Outcome(5, "b", "not_stacked"), 1) is None
    assert "refused" in (restack.comment_text(restack.Outcome(5, "b", "push_failed"), 1) or "")


def test_core_75_cli_reports_each_pr(
    repos: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    _origin, work = repos
    spec_head = (work.parent / "spec_head").read_text()
    prs = work.parent / "prs.txt"
    prs.write_text("2 code\n\n")
    import os

    cwd = Path.cwd()
    os.chdir(work)
    try:
        original = restack.regenerate
        restack.regenerate = fake_regen  # type: ignore[assignment]
        try:
            code = restack.main(
                ["--merged-sha", spec_head, "--merged-number", "1", "--prs", str(prs)]
            )
        finally:
            restack.regenerate = original  # type: ignore[assignment]
    finally:
        os.chdir(cwd)
    assert code == 0
    assert '"status": "rebased"' in capsys.readouterr().out
