from __future__ import annotations

from pathlib import Path

import pytest

from chartpub.models import Artifact
from chartpub.transaction import Publisher


def test_release_is_verified_before_pages_change(tmp_path: Path) -> None:
    events: list[str] = []

    def fail_release(_artifact: Artifact) -> None:
        events.append("release")
        raise RuntimeError("upload rejected")

    publisher = Publisher(tmp_path / "state", lambda _artifact: events.append("pages"), fail_release)
    artifact = Artifact(tmp_path / "chart.tgz", "ledger-api", "0.4.1", "a" * 64, 1)
    with pytest.raises(RuntimeError, match="upload rejected"):
        publisher.publish(artifact)
    assert events == ["release"]


def test_completed_publication_records_digest(tmp_path: Path) -> None:
    publisher = Publisher(tmp_path / "state", lambda _artifact: None, lambda _artifact: None)
    artifact = Artifact(tmp_path / "chart.tgz", "ledger-api", "0.4.1", "b" * 64, 1)
    publisher.publish(artifact)
    assert '"phase": "complete"' in (tmp_path / "state" / "transaction.json").read_text()
    assert artifact.sha256 in (tmp_path / "state" / "transaction.json").read_text()

