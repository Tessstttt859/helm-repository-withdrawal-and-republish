"""Resolving and guarding the remote target named by the contract."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from chartpub.errors import UsageError

_URL_RE = re.compile(
    r"^(?:https://(?P<host>[^/@]+)/|git@(?P<sshhost>[^:]+):)(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$"
)


def origin_url(repo_root: Path) -> str:
    completed = subprocess.run(  # noqa: S603 - fixed executable, list arguments
        ["git", "-C", str(repo_root), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise UsageError("this working tree has no `origin` remote to publish from")
    return completed.stdout.strip()


def parse_repository(url: str) -> str:
    """owner/name from a credential-free origin URL."""
    if "@" in url and url.startswith("https://"):
        raise UsageError("origin URL embeds credentials; remove them before publishing")
    match = _URL_RE.match(url)
    if match is None:
        raise UsageError(f"cannot parse a GitHub owner/repo out of origin URL: {url}")
    return f"{match.group('owner')}/{match.group('repo')}"


def resolve_target(repo_root: Path, expected: str) -> tuple[str, str]:
    """Return (url, owner/repo), refusing to act on a different repository."""
    url = origin_url(repo_root)
    actual = parse_repository(url)
    if actual.casefold() != expected.casefold():
        raise UsageError(
            f"origin resolves to {actual!r} but the contract targets {expected!r}; "
            "refusing to mutate a repository the contract does not name"
        )
    return url, actual


def head_commit(repo_root: Path, ref: str = "HEAD") -> str:
    completed = subprocess.run(  # noqa: S603 - fixed executable, list arguments
        ["git", "-C", str(repo_root), "rev-parse", ref],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise UsageError(f"cannot resolve {ref} in {repo_root}")
    return completed.stdout.strip()
