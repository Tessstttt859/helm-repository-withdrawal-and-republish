"""Deterministic construction and guarded update of the Pages snapshot branch."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from chartpub.errors import PublicationError, RemoteConflict
from chartpub.security import redact

#: Committed with fixed identity and timestamps so the same snapshot content
#: always produces the same commit.
COMMIT_ENV = {
    "GIT_AUTHOR_NAME": "chartpub",
    "GIT_AUTHOR_EMAIL": "chartpub@users.noreply.github.com",
    "GIT_COMMITTER_NAME": "chartpub",
    "GIT_COMMITTER_EMAIL": "chartpub@users.noreply.github.com",
}
ASKPASS = """#!/bin/sh
case "$1" in
*[Uu]sername*) printf '%s' "x-access-token" ;;
*) printf '%s' "$CHARTPUB_TOKEN" ;;
esac
"""
README = """# Helm chart repository

This branch is generated publication state. Add it as a Helm repository with:

```bash
helm repo add {chart} {url}
```
"""


@dataclass(frozen=True)
class GitResult:
    code: int
    out: str
    err: str


class Git:
    """Runs git without ever placing a credential in argv, a URL, or config."""

    def __init__(self, root: Path, token: str = "", secrets: Sequence[str] = ()) -> None:
        self.root = root
        self.token = token
        self.secrets = tuple(secrets) + ((token,) if token else ())
        self._askpass: Path | None = None

    def _environment(self) -> Mapping[str, str]:
        env = dict(os.environ)
        env.update(COMMIT_ENV)
        env["GIT_TERMINAL_PROMPT"] = "0"
        if self.token:
            if self._askpass is None:
                handle = Path(tempfile.mkdtemp(prefix="chartpub-askpass-")) / "askpass.sh"
                handle.write_text(ASKPASS, encoding="utf-8")
                handle.chmod(handle.stat().st_mode | stat.S_IXUSR)
                self._askpass = handle
            env["GIT_ASKPASS"] = str(self._askpass)
            env["CHARTPUB_TOKEN"] = self.token
        return env

    def run(self, *args: str, check: bool = True, cwd: Path | None = None) -> GitResult:
        completed = subprocess.run(  # noqa: S603 - fixed executable, list arguments
            ["git", *args],
            cwd=str(cwd or self.root),
            env=dict(self._environment()),
            capture_output=True,
            text=True,
            check=False,
        )
        result = GitResult(
            completed.returncode,
            redact(completed.stdout, self.secrets),
            redact(completed.stderr, self.secrets),
        )
        if check and result.code != 0:
            raise PublicationError(
                f"git {' '.join(args)} failed ({result.code}): "
                f"{(result.err or result.out).strip()[:400]}"
            )
        return result

    def cleanup(self) -> None:
        if self._askpass is not None:
            shutil.rmtree(self._askpass.parent, ignore_errors=True)
            self._askpass = None


def snapshot_files(
    index_yaml: str, archives: Mapping[str, bytes], *, chart: str, url: str
) -> dict[str, bytes]:
    """The complete, deterministic content of the Pages branch."""
    files: dict[str, bytes] = {
        "index.yaml": index_yaml.encode("utf-8"),
        "README.md": README.format(chart=chart, url=url.rstrip("/")).encode("utf-8"),
    }
    for name in sorted(archives):
        files[name] = archives[name]
    return files


def write_snapshot(worktree: Path, files: Mapping[str, bytes]) -> None:
    """Replace the worktree content with exactly `files` (plus .git)."""
    for existing in sorted(worktree.iterdir(), reverse=True):
        if existing.name == ".git":
            continue
        if existing.is_dir():
            shutil.rmtree(existing)
        else:
            existing.unlink()
    for name in sorted(files):
        target = worktree / name
        if ".." in Path(name).parts or Path(name).is_absolute():
            raise PublicationError(f"refusing to write snapshot path {name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(files[name])


def commit_snapshot(git: Git, worktree: Path, message: str) -> str | None:
    """Commit the staged snapshot; returns None when nothing changed."""
    git.run("add", "--all", ".", cwd=worktree)
    if git.run("diff", "--cached", "--quiet", check=False, cwd=worktree).code == 0:
        return None
    git.run("commit", "--no-verify", "-m", message, cwd=worktree)
    return git.run("rev-parse", "HEAD", cwd=worktree).out.strip()


def push_with_lease(git: Git, worktree: Path, remote: str, branch: str, expected: str) -> None:
    """Push only if the remote branch still points at `expected`."""
    actual = remote_tip(git, remote, branch, cwd=worktree)
    if actual != expected:
        raise RemoteConflict(
            f"{branch} is at {actual or 'absent'} but {expected} was expected; "
            "refusing to force-update over concurrent work"
        )
    git.run(
        "push",
        f"--force-with-lease=refs/heads/{branch}:{expected}",
        remote,
        f"HEAD:refs/heads/{branch}",
        cwd=worktree,
    )


def remote_tip(git: Git, remote: str, branch: str, *, cwd: Path | None = None) -> str | None:
    result = git.run(
        "ls-remote", "--exit-code", remote, f"refs/heads/{branch}", check=False, cwd=cwd
    )
    if result.code != 0:
        return None
    line = result.out.strip().splitlines()[0] if result.out.strip() else ""
    return line.split()[0] if line else None
