from __future__ import annotations

from pathlib import Path

from chartpub.security import read_env_file, redact, require_token


def test_reads_token_without_logging(tmp_path: Path) -> None:
    path = tmp_path / "credentials.env"
    path.write_text("GITHUB_TOKEN=test-secret-value\n", encoding="utf-8")
    values = read_env_file(path)
    assert require_token(values) == "test-secret-value"
    assert redact("failed with test-secret-value", values) == "failed with [REDACTED]"


def test_redacts_gh_token_alias() -> None:
    assert redact("token alias-secret", {"GH_TOKEN": "alias-secret"}) == "token [REDACTED]"
