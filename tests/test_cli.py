from __future__ import annotations

import json
from pathlib import Path

import pytest

from chartpub.cli import build_parser, main
from chartpub.errors import (
    EXIT_CONFLICT,
    EXIT_OK,
    EXIT_USAGE,
    EXIT_VALIDATION,
    RemoteConflict,
    ValidationError,
)
from tests.conftest import contract_mapping, git
from tests.test_config import valid_contract, write_contract


def _contract(tmp_path: Path, **overrides: object) -> Path:
    path = tmp_path / "contract.json"
    write_contract(path, contract_mapping(**overrides))
    return path


def test_plan_is_machine_readable(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "contract.json"
    write_contract(path, valid_contract())
    assert main(["plan", "--contract", str(path)]) == EXIT_OK
    output = json.loads(capsys.readouterr().out)
    assert output["command"] == "plan"


def test_all_lifecycle_commands_exist() -> None:
    help_text = build_parser().format_help()
    for name in ("plan", "publish", "withdraw", "audit", "repair"):
        assert name in help_text


def test_plan_groups_changes_and_flags_force(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["plan", "--contract", str(_contract(tmp_path))]) == EXIT_OK
    output = json.loads(capsys.readouterr().out)
    assert set(output["changes"]) == {"local", "github_release", "github_tag", "pages"}
    assert output["force_update_required"] is True
    assert output["dry_run"] is True
    assert "chartpub withdraw" in " ".join(output["notes"])


def test_plan_is_offline_by_default(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # No --credentials and no network: planning still succeeds.
    assert main(["plan", "--contract", str(_contract(tmp_path)), "--repo-root", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["preconditions"]["pages_branch"]["expected"]


def test_publish_dry_run_needs_no_credentials(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], work_root: Path
) -> None:
    code = main(
        [
            "publish",
            "--dry-run",
            "--contract",
            str(_contract(tmp_path)),
            "--repo-root",
            str(work_root),
            "--state-dir",
            str(tmp_path / "state"),
        ]
    )
    output = json.loads(capsys.readouterr().out)
    assert code == EXIT_OK
    assert output["dry_run"] is True
    assert output["validation"]["ok"] is True
    assert output["artifact"]["name"] == "ledger-api-0.4.1.tgz"


def test_publish_dry_run_reports_validation_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], work_root: Path
) -> None:
    deployment = work_root / "charts" / "ledger-api" / "templates" / "deployment.yaml"
    head, _, tail = deployment.read_text(encoding="utf-8").partition("  template:")
    deployment.write_text(
        head + "  template:" + tail.replace("component: api", "component: worker", 1),
        encoding="utf-8",
    )
    code = main(
        [
            "publish",
            "--dry-run",
            "--contract",
            str(_contract(tmp_path)),
            "--repo-root",
            str(work_root),
            "--state-dir",
            str(tmp_path / "state"),
        ]
    )
    assert code == EXIT_VALIDATION
    assert "failed validation" in capsys.readouterr().err


def test_a_malformed_contract_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "contract.json"
    write_contract(path, {"schema_version": 1})
    assert main(["plan", "--contract", str(path)]) == EXIT_USAGE
    assert "missing contract field" in capsys.readouterr().err


def test_remote_commands_require_credentials(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["audit", "--contract", str(_contract(tmp_path))]) == EXIT_USAGE
    assert "GitHub credentials" in capsys.readouterr().err


def test_credentials_must_match_the_contract_repository(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env = tmp_path / "credentials.env"
    env.write_text(
        "GITHUB_TOKEN=some-secret-token\nGITHUB_REPOSITORY=someone/else\n", encoding="utf-8"
    )
    code = main(["audit", "--contract", str(_contract(tmp_path)), "--credentials", str(env)])
    assert code == EXIT_USAGE
    message = capsys.readouterr().err
    assert "someone/else" in message
    assert "some-secret-token" not in message


def test_origin_must_match_the_contract_repository(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "elsewhere"
    root.mkdir()
    git("init", "--initial-branch", "main", ".", cwd=root)
    git("remote", "add", "origin", "https://github.com/someone/else.git", cwd=root)
    env = tmp_path / "credentials.env"
    env.write_text("GITHUB_TOKEN=some-secret-token\n", encoding="utf-8")
    code = main(
        [
            "audit",
            "--contract",
            str(_contract(tmp_path)),
            "--credentials",
            str(env),
            "--repo-root",
            str(root),
        ]
    )
    assert code == EXIT_USAGE
    assert "refusing to mutate" in capsys.readouterr().err


def test_error_output_never_leaks_a_token(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    token = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"
    env = tmp_path / "credentials.env"
    env.write_text(f"GITHUB_TOKEN={token}\n", encoding="utf-8")
    monkeypatch.setattr(
        "chartpub.cli.resolve_target",
        lambda _root, _expected: (_ for _ in ()).throw(
            ValidationError(f"boom with {token} inside")
        ),
    )
    code = main(["audit", "--contract", str(_contract(tmp_path)), "--credentials", str(env)])
    assert code == EXIT_VALIDATION
    assert token not in capsys.readouterr().err


def test_exit_code_maps_a_remote_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "chartpub.cli.load_contract",
        lambda _path: (_ for _ in ()).throw(RemoteConflict("tip moved")),
    )
    assert main(["plan", "--contract", str(_contract(tmp_path))]) == EXIT_CONFLICT
    assert "tip moved" in capsys.readouterr().err


def test_values_fixtures_can_be_overridden(tmp_path: Path, work_root: Path) -> None:
    values = tmp_path / "extra.yaml"
    values.write_text("replicaCount: 4\n", encoding="utf-8")
    code = main(
        [
            "publish",
            "--dry-run",
            "--contract",
            str(_contract(tmp_path)),
            "--repo-root",
            str(work_root),
            "--state-dir",
            str(tmp_path / "state"),
            "--values",
            str(values),
        ]
    )
    assert code == EXIT_OK
