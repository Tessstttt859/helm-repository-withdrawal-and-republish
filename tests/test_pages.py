from __future__ import annotations

from pathlib import Path

import pytest

from chartpub.errors import PublicationError, RemoteConflict
from chartpub.pages import (
    Git,
    commit_snapshot,
    push_with_lease,
    remote_tip,
    snapshot_files,
    write_snapshot,
)
from tests.conftest import git, pages_tip


def _clone(bare: Path, tmp_path: Path, name: str = "wt") -> tuple[Git, Path]:
    worktree = tmp_path / name
    git("clone", "--branch", "gh-pages", str(bare), str(worktree), cwd=tmp_path)
    return Git(worktree), worktree


def test_snapshot_files_are_complete_and_sorted() -> None:
    files = snapshot_files(
        "apiVersion: v1\n",
        {"b.tgz": b"b", "a.tgz": b"a"},
        chart="ledger-api",
        url="https://owner.example/repo/",
    )
    assert list(files) == ["index.yaml", "README.md", "a.tgz", "b.tgz"]
    assert b"helm repo add ledger-api https://owner.example/repo" in files["README.md"]


def test_write_snapshot_replaces_everything_but_git(tmp_path: Path, pages_remote: Path) -> None:
    _gitrunner, worktree = _clone(pages_remote, tmp_path)
    (worktree / "stale.tgz").write_bytes(b"old")
    (worktree / "nested").mkdir()
    (worktree / "nested" / "deep.txt").write_text("old", encoding="utf-8")
    write_snapshot(worktree, {"index.yaml": b"apiVersion: v1\n"})
    assert {p.name for p in worktree.iterdir()} == {".git", "index.yaml"}


def test_write_snapshot_refuses_escaping_paths(tmp_path: Path, pages_remote: Path) -> None:
    _gitrunner, worktree = _clone(pages_remote, tmp_path)
    with pytest.raises(PublicationError, match="refusing to write"):
        write_snapshot(worktree, {"../escape": b"x"})


def test_commit_is_a_noop_when_nothing_changed(tmp_path: Path, pages_remote: Path) -> None:
    runner, worktree = _clone(pages_remote, tmp_path)
    assert commit_snapshot(runner, worktree, "no change") is None
    write_snapshot(worktree, {"index.yaml": b"apiVersion: v1\n"})
    commit = commit_snapshot(runner, worktree, "change")
    assert commit is not None
    assert commit_snapshot(runner, worktree, "again") is None


def test_push_with_lease_rejects_a_moved_tip(tmp_path: Path, pages_remote: Path) -> None:
    runner, worktree = _clone(pages_remote, tmp_path)
    write_snapshot(worktree, {"index.yaml": b"apiVersion: v1\n"})
    commit_snapshot(runner, worktree, "change")
    with pytest.raises(RemoteConflict, match="refusing to force-update"):
        push_with_lease(runner, worktree, str(pages_remote), "gh-pages", "0" * 40)
    expected = pages_tip(pages_remote)
    assert expected is not None
    push_with_lease(runner, worktree, str(pages_remote), "gh-pages", expected)
    assert pages_tip(pages_remote) != expected


def test_remote_tip_is_none_for_an_unknown_branch(tmp_path: Path, pages_remote: Path) -> None:
    runner, worktree = _clone(pages_remote, tmp_path)
    assert remote_tip(runner, str(pages_remote), "does-not-exist", cwd=worktree) is None
    assert remote_tip(runner, str(pages_remote), "gh-pages", cwd=worktree) is not None


def test_git_failures_are_raised_with_redacted_output(tmp_path: Path) -> None:
    runner = Git(tmp_path, token="super-secret-token", secrets=("super-secret-token",))
    try:
        with pytest.raises(PublicationError, match="git ls-remote"):
            runner.run("ls-remote", "https://super-secret-token@invalid.invalid/x")
    finally:
        runner.cleanup()


def test_askpass_is_created_and_removed(tmp_path: Path, pages_remote: Path) -> None:
    runner = Git(tmp_path, token="super-secret-token")
    try:
        runner.run("ls-remote", str(pages_remote), check=False)
        askpass = runner._askpass
        assert askpass is not None and askpass.is_file()
    finally:
        runner.cleanup()
    assert not askpass.exists()
    assert runner._askpass is None
