"""The CLI entry point driven end to end against the mocked remote."""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from chartpub.cli import main
from chartpub.errors import EXIT_OK, EXIT_VALIDATION, PublicationError
from chartpub.github import GitHubClient, urllib_transport
from tests.conftest import MAIN_SHA, contract_mapping, git, seed_pages
from tests.fakes import FakeGitHub

TOKEN = "cli-secret-token-value"


@pytest.fixture
def cli_env(
    tmp_path: Path,
    work_root: Path,
    pages_remote: Path,
    fake_github: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[dict[str, Any]]:
    """Wire the CLI to the fake GitHub and the local bare gh-pages remote."""
    build = tmp_path / "build"
    from chartpub.archive import package_chart, sha256_bytes
    from chartpub.pages import snapshot_files
    from tests.test_operations import _index_text

    payload = package_chart(work_root / "charts" / "ledger-api", build, "0.4.0").path.read_bytes()
    fake_github.refs["heads/main"] = MAIN_SHA
    fake_github.refs["tags/chart-v0.4.0"] = "b" * 40
    fake_github.add_release("chart-v0.4.0", assets={"ledger-api-0.4.0.tgz": payload})
    tip = seed_pages(
        pages_remote,
        tmp_path,
        snapshot_files(
            _index_text({"0.4.0": (sha256_bytes(payload), "2026-02-01T00:00:00Z")}),
            {"ledger-api-0.4.0.tgz": payload},
            chart="ledger-api",
            url="https://owner.example/repo",
        ),
        message="publish 0.4.0",
    )

    contract = tmp_path / "contract.json"
    contract.write_text(json.dumps(contract_mapping(expected_pages_tip=tip)), encoding="utf-8")
    credentials = tmp_path / "credentials.env"
    credentials.write_text(
        f"GITHUB_TOKEN={TOKEN}\nGITHUB_REPOSITORY=owner/repo\n", encoding="utf-8"
    )

    def client(**kwargs: Any) -> GitHubClient:
        kwargs["transport"] = fake_github
        return GitHubClient(**kwargs)

    monkeypatch.setattr("chartpub.cli.GitHubClient", client)
    monkeypatch.setattr(
        "chartpub.cli.resolve_target", lambda _root, expected: (str(pages_remote), expected)
    )
    yield {
        "argv": [
            "--contract",
            str(contract),
            "--credentials",
            str(credentials),
            "--repo-root",
            str(work_root),
            "--state-dir",
            str(tmp_path / "state"),
        ],
        "fake": fake_github,
        "remote": pages_remote,
        "tmp_path": tmp_path,
    }


def _run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict[str, Any]]:
    code = main(argv)
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip() else {}


def test_audit_exits_non_zero_while_the_incident_stands(
    cli_env: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    code, payload = _run(["audit", *cli_env["argv"]], capsys)
    assert code == EXIT_VALIDATION
    assert payload["consistent"] is False


def test_withdraw_then_publish_then_audit_is_clean(
    cli_env: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    code, withdrawn = _run(["withdraw", *cli_env["argv"]], capsys)
    assert code == EXIT_OK
    assert withdrawn["withdrawn"]["release"]["draft"] is True
    assert "tags/chart-v0.4.0" not in cli_env["fake"].refs

    code, published = _run(
        [
            "publish",
            *cli_env["argv"],
            "--target-commit",
            MAIN_SHA,
            "--allow-drift",
        ],
        capsys,
    )
    assert code == EXIT_OK
    assert published["published"]["asset"]["sha256"] == published["artifact"]["sha256"]

    code, audited = _run(["audit", *cli_env["argv"], "--allow-drift"], capsys)
    assert code == EXIT_OK
    assert audited["consistent"] is True
    assert audited["advertised_versions"] == ["0.4.1"]


def test_repair_reports_actions(
    cli_env: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    code, repaired = _run(["repair", *cli_env["argv"]], capsys)
    assert code == EXIT_OK
    assert repaired["consistent"] is True
    assert any("quarantined" in action for action in repaired["actions"])


def test_repair_dry_run_is_read_only(
    cli_env: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    code, repaired = _run(["repair", "--dry-run", *cli_env["argv"]], capsys)
    assert code == EXIT_OK
    assert repaired["actions"] == []
    assert cli_env["fake"].release_by_tag("chart-v0.4.0")["draft"] is False


def test_plan_with_remote_state(
    cli_env: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    code, plan = _run(["plan", "--remote", "--for", "withdraw", *cli_env["argv"]], capsys)
    assert code == EXIT_OK
    assert plan["command"] == "withdraw"
    assert plan["preconditions"]["bad_release_state"] == "published"


def test_plan_remote_without_a_subcommand_falls_back_to_the_lifecycle(
    cli_env: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    code, plan = _run(["plan", "--remote", *cli_env["argv"]], capsys)
    assert code == EXIT_OK
    assert plan["command"] == "plan"


def test_publish_defaults_to_the_working_tree_head(
    cli_env: dict[str, Any], capsys: pytest.CaptureFixture[str], work_root: Path
) -> None:
    git("init", "--initial-branch", "main", ".", cwd=work_root)
    git("add", "--all", ".", cwd=work_root)
    git("commit", "-m", "chart", cwd=work_root)
    _run(["withdraw", *cli_env["argv"]], capsys)
    code, published = _run(["publish", *cli_env["argv"], "--allow-drift"], capsys)
    assert code == EXIT_OK
    assert published["published"]["tag"]["sha"] == git("rev-parse", "HEAD", cwd=work_root)


def test_urllib_transport_returns_status_headers_and_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponse:
        status = 201
        headers = {"Link": '<x>; rel="next"'}

        def read(self) -> bytes:
            return b'{"ok": true}'

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: FakeResponse())
    status, headers, body = urllib_transport("GET", "https://api.github.com/x", {}, None)
    assert (status, body) == (201, b'{"ok": true}')
    assert headers["Link"].endswith('rel="next"')


def test_urllib_transport_surfaces_http_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_http(*_a: object, **_k: object) -> None:
        raise urllib.error.HTTPError(
            "https://api.github.com/x",
            404,
            "Not Found",
            {"X": "y"},
            None,  # type: ignore[arg-type]
        )

    monkeypatch.setattr(urllib.request, "urlopen", raise_http)
    status, headers, body = urllib_transport("GET", "https://api.github.com/x", {}, None)
    assert status == 404
    assert headers["X"] == "y"
    client = GitHubClient(repository="owner/repo", token="t", transport=urllib_transport)
    with pytest.raises(PublicationError, match="failed \\(404\\)"):
        client.get_release(1)


def test_module_entry_point_runs() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "chartpub", "--help"], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0
    assert "withdraw" in completed.stdout
