from __future__ import annotations

from pathlib import Path

from chartpub.index import add_artifact, remove_version
from chartpub.models import Artifact


def artifact(version: str = "0.4.1") -> Artifact:
    return Artifact(Path(f"ledger-api-{version}.tgz"), "ledger-api", version, "a" * 64, 42)


def test_add_is_idempotent() -> None:
    index: dict[str, object] = {"apiVersion": "v1", "entries": {}}
    add_artifact(index, artifact(), "https://example.test/charts")
    add_artifact(index, artifact(), "https://example.test/charts")
    assert len(index["entries"]["ledger-api"]) == 1  # type: ignore[index]


def test_remove_preserves_other_versions() -> None:
    index: dict[str, object] = {"apiVersion": "v1", "entries": {}}
    add_artifact(index, artifact("0.4.0"), "https://example.test/charts")
    add_artifact(index, artifact("0.3.0"), "https://example.test/charts")
    remove_version(index, "ledger-api", "0.4.0")
    versions = index["entries"]["ledger-api"]  # type: ignore[index]
    assert [item["version"] for item in versions] == ["0.3.0"]

