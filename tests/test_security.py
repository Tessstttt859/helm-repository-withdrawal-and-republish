from __future__ import annotations

from pathlib import Path

import pytest

from chartpub.errors import ChartpubError, UsageError
from chartpub.security import (
    read_env_file,
    redact,
    require_repository,
    require_token,
    secret_values,
)

REAL_SHAPE = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"


def test_reads_token_without_logging(tmp_path: Path) -> None:
    path = tmp_path / "credentials.env"
    path.write_text("GITHUB_TOKEN=test-secret-value\n", encoding="utf-8")
    values = read_env_file(path)
    assert require_token(values) == "test-secret-value"
    assert redact("failed with test-secret-value", values) == "failed with [REDACTED]"


def test_redacts_gh_token_alias() -> None:
    assert redact("token alias-secret", {"GH_TOKEN": "alias-secret"}) == "token [REDACTED]"


def test_parses_quotes_exports_and_comments(tmp_path: Path) -> None:
    path = tmp_path / "credentials.env"
    path.write_text(
        "# a comment\n\n"
        'export GITHUB_TOKEN="quoted-secret-value"\n'
        "GITHUB_REPOSITORY='owner/repo'\n",
        encoding="utf-8",
    )
    values = read_env_file(path)
    assert values["GITHUB_TOKEN"] == "quoted-secret-value"
    assert values["GITHUB_REPOSITORY"] == "owner/repo"


def test_rejects_a_malformed_entry(tmp_path: Path) -> None:
    path = tmp_path / "credentials.env"
    path.write_text("GITHUB_TOKEN\n", encoding="utf-8")
    with pytest.raises(ChartpubError, match="invalid environment entry"):
        read_env_file(path)


def test_missing_file_is_reported_without_content(tmp_path: Path) -> None:
    with pytest.raises(ChartpubError, match="cannot read credential file"):
        read_env_file(tmp_path / "absent.env")


def test_missing_token_is_reported() -> None:
    with pytest.raises(ChartpubError, match="token is missing"):
        require_token({"GITHUB_REPOSITORY": "owner/repo"})


def test_repository_mismatch_refuses_to_act() -> None:
    with pytest.raises(UsageError, match="targets"):
        require_repository({"GITHUB_REPOSITORY": "other/repo"}, "owner/repo")
    assert require_repository({"GITHUB_REPOSITORY": "OWNER/REPO"}, "owner/repo") == "owner/repo"
    assert require_repository({}, "owner/repo") == "owner/repo"


def test_secret_values_are_longest_first_and_skip_short_noise() -> None:
    values = {
        "GITHUB_TOKEN": "a-long-secret-value",
        "API_SECRET": "another-secret-value-x",
        "SHORT_TOKEN": "abc",
        "PAGES_URL": "https://example.test",
    }
    assert secret_values(values) == ("another-secret-value-x", "a-long-secret-value")


def test_redaction_covers_token_shapes_without_a_known_value() -> None:
    message = f"remote rejected: Bearer {REAL_SHAPE} denied"
    assert REAL_SHAPE not in redact(message)
    assert "[REDACTED]" in redact(message)
    assert "github_pat_" not in redact("token github_pat_" + "a" * 30)


def test_redaction_accepts_a_plain_sequence() -> None:
    assert redact("value abcdefghij here", ["abcdefghij", ""]) == "value [REDACTED] here"


def test_redaction_is_a_noop_without_secrets() -> None:
    assert redact("nothing to hide") == "nothing to hide"
