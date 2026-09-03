from __future__ import annotations

from pathlib import Path

import pytest

from chartpub.errors import UsageError
from chartpub.remote import head_commit, origin_url, parse_repository, resolve_target
from tests.conftest import git


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git("init", "--initial-branch", "main", ".", cwd=root)
    (root / "file.txt").write_text("x", encoding="utf-8")
    git("add", "--all", ".", cwd=root)
    git("commit", "-m", "initial", cwd=root)
    git("remote", "add", "origin", "https://github.com/owner/repo.git", cwd=root)
    return root


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/owner/repo.git",
        "https://github.com/owner/repo",
        "git@github.com:owner/repo.git",
    ],
)
def test_parses_every_supported_origin_form(url: str) -> None:
    assert parse_repository(url) == "owner/repo"


def test_rejects_a_credential_bearing_origin() -> None:
    with pytest.raises(UsageError, match="embeds credentials"):
        parse_repository("https://token@github.com/owner/repo.git")


def test_rejects_an_unparsable_origin() -> None:
    with pytest.raises(UsageError, match="cannot parse"):
        parse_repository("ftp://example.invalid/thing")


def test_resolve_target_matches_the_contract(repo: Path) -> None:
    url, repository = resolve_target(repo, "Owner/Repo")
    assert repository == "owner/repo"
    assert url == origin_url(repo)


def test_resolve_target_refuses_a_different_repository(repo: Path) -> None:
    with pytest.raises(UsageError, match="refusing to mutate"):
        resolve_target(repo, "someone/else")


def test_missing_origin_is_reported(tmp_path: Path) -> None:
    root = tmp_path / "bare"
    root.mkdir()
    git("init", "--initial-branch", "main", ".", cwd=root)
    with pytest.raises(UsageError, match="no `origin` remote"):
        origin_url(root)


def test_head_commit_resolves_and_reports_failure(repo: Path) -> None:
    assert len(head_commit(repo)) == 40
    with pytest.raises(UsageError, match="cannot resolve"):
        head_commit(repo, "refs/heads/nope")
