from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from chartpub.archive import package_chart
from chartpub.config import load_contract
from chartpub.github import GitHubClient
from chartpub.models import PublicationContract
from chartpub.operations import Operations, Settings
from chartpub.pages import Git
from tests.fakes import FakeGitHub

REPOSITORY = "owner/repo"
PAGES_URL = "https://owner.example/repo"
MAIN_SHA = "a" * 40
BAD_TAG_SHA = "b" * 40


@pytest.fixture
def chart_dir() -> Path:
    return Path(__file__).parents[1] / "charts" / "ledger-api"


@pytest.fixture
def values_files() -> tuple[Path, ...]:
    root = Path(__file__).parent / "fixtures"
    return (root / "values-minimal.yaml", root / "values-ha.yaml")


def contract_mapping(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "repository": REPOSITORY,
        "source_branch": "main",
        "pages_branch": "gh-pages",
        "pages_url": PAGES_URL,
        "chart": "ledger-api",
        "bad_version": "0.4.0",
        "replacement_version": "0.4.1",
        "bad_tag": "chart-v0.4.0",
        "replacement_tag": "chart-v0.4.1",
        "expected_bad_tag_target": BAD_TAG_SHA,
        "expected_pages_tip": "c" * 40,
        "release_asset_name": "ledger-api-{version}.tgz",
    }
    value.update(overrides)
    return value


def git(*args: str, cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "HOME": str(cwd),
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
        },
    )
    return completed.stdout.strip()


@pytest.fixture
def pages_remote(tmp_path: Path) -> Path:
    """A bare repository standing in for origin, with an empty gh-pages branch."""
    bare = tmp_path / "origin.git"
    git("init", "--bare", "--initial-branch", "gh-pages", str(bare), cwd=tmp_path)
    seed = tmp_path / "seed"
    seed.mkdir()
    git("init", "--initial-branch", "gh-pages", ".", cwd=seed)
    (seed / "README.md").write_text("seed\n", encoding="utf-8")
    git("add", "--all", ".", cwd=seed)
    git("commit", "-m", "seed", cwd=seed)
    git("remote", "add", "origin", str(bare), cwd=seed)
    git("push", "origin", "HEAD:refs/heads/gh-pages", cwd=seed)
    shutil.rmtree(seed)
    return bare


def pages_tip(bare: Path, branch: str = "gh-pages") -> str | None:
    out = git("ls-remote", str(bare), f"refs/heads/{branch}", cwd=bare)
    return out.split()[0] if out else None


@pytest.fixture
def work_root(tmp_path: Path, chart_dir: Path) -> Path:
    """A throwaway working tree containing the chart under test."""
    root = tmp_path / "work"
    (root / "charts").mkdir(parents=True)
    shutil.copytree(chart_dir, root / "charts" / "ledger-api")
    return root


def make_operations(
    *,
    work_root: Path,
    pages_remote: Path,
    fake: FakeGitHub,
    values: Sequence[Path] = (),
    dry_run: bool = False,
    allow_drift: bool = False,
    contract: PublicationContract | None = None,
    token: str = "test-token-value",
) -> Operations:
    resolved = contract or PublicationContract.from_mapping(contract_mapping())
    settings = Settings(
        contract=resolved,
        repo_root=work_root,
        state_dir=work_root / ".chartpub",
        values_files=tuple(values),
        dry_run=dry_run,
        helm=_helm_or_skip(),
        allow_drift=allow_drift,
    )
    operations = Operations(settings)
    operations.client = GitHubClient(
        repository=resolved.repository, token=token, transport=fake, secrets=(token,)
    )
    operations.git = Git(work_root, token="", secrets=(token,))
    operations.remote_url = str(pages_remote)
    return operations


def _helm_or_skip() -> object:
    if shutil.which("helm") is None:  # pragma: no cover - depends on the host
        pytest.skip("helm 3 is required for validation tests")
    from chartpub.validate import default_helm_runner

    return default_helm_runner


@pytest.fixture
def fake_github() -> FakeGitHub:
    return FakeGitHub(repository=REPOSITORY, page_size=1)


@pytest.fixture
def operations(
    work_root: Path, pages_remote: Path, fake_github: FakeGitHub, values_files: tuple[Path, ...]
) -> Iterator[Operations]:
    fake_github.refs["heads/main"] = MAIN_SHA
    ops = make_operations(
        work_root=work_root, pages_remote=pages_remote, fake=fake_github, values=values_files
    )
    yield ops
    ops.close()


def published_index(bare: Path, tmp_path: Path) -> dict[str, object]:
    """Read index.yaml back out of the bare remote, the way a client would."""
    import yaml

    checkout = tmp_path / f"verify-{bare.name}"
    if checkout.exists():
        shutil.rmtree(checkout)
    git("clone", "--branch", "gh-pages", str(bare), str(checkout), cwd=tmp_path)
    index = checkout / "index.yaml"
    if not index.is_file():
        return {}
    value = yaml.safe_load(index.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def published_files(bare: Path, tmp_path: Path) -> set[str]:
    checkout = tmp_path / f"files-{bare.name}"
    if checkout.exists():
        shutil.rmtree(checkout)
    git("clone", "--branch", "gh-pages", str(bare), str(checkout), cwd=tmp_path)
    return {
        path.relative_to(checkout).as_posix()
        for path in checkout.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(checkout).parts
    }


def build_archive(chart_dir: Path, out: Path, version: str) -> bytes:
    return package_chart(chart_dir, out, version).path.read_bytes()


def write_contract(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def load(path: Path) -> PublicationContract:
    return load_contract(path)


def seed_pages(bare: Path, tmp_path: Path, files: dict[str, bytes], message: str = "seed") -> str:
    """Force the bare gh-pages branch to hold exactly `files`; returns the tip."""
    checkout = tmp_path / f"seed-{abs(hash(message)) % 10_000}"
    if checkout.exists():
        shutil.rmtree(checkout)
    git("clone", "--branch", "gh-pages", str(bare), str(checkout), cwd=tmp_path)
    for existing in checkout.iterdir():
        if existing.name == ".git":
            continue
        if existing.is_dir():
            shutil.rmtree(existing)
        else:
            existing.unlink()
    for name, payload in sorted(files.items()):
        target = checkout / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    git("add", "--all", ".", cwd=checkout)
    git("commit", "--allow-empty", "-m", message, cwd=checkout)
    git("push", "origin", "HEAD:refs/heads/gh-pages", cwd=checkout)
    tip = git("rev-parse", "HEAD", cwd=checkout)
    shutil.rmtree(checkout)
    return tip
