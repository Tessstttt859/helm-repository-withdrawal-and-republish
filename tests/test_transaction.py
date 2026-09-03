from __future__ import annotations

import json
from pathlib import Path

import pytest

from chartpub.errors import RollbackError
from chartpub.models import Artifact
from chartpub.transaction import PHASES, Journal, Publisher


def artifact(tmp_path: Path, digest: str = "a" * 64) -> Artifact:
    return Artifact(tmp_path / "ledger-api-0.4.1.tgz", "ledger-api", "0.4.1", digest, 1)


def test_release_is_verified_before_pages_change(tmp_path: Path) -> None:
    events: list[str] = []

    def fail_release(_artifact: Artifact) -> None:
        events.append("release")
        raise RuntimeError("upload rejected")

    publisher = Publisher(
        tmp_path / "state", lambda _artifact: events.append("pages"), fail_release
    )
    with pytest.raises(RuntimeError, match="upload rejected"):
        publisher.publish(artifact(tmp_path))
    assert events == ["release"]


def test_completed_publication_records_digest(tmp_path: Path) -> None:
    publisher = Publisher(tmp_path / "state", lambda _a: None, lambda _a: None)
    item = artifact(tmp_path, "b" * 64)
    publisher.publish(item)
    text = (tmp_path / "state" / "transaction.json").read_text(encoding="utf-8")
    assert '"phase": "complete"' in text
    assert item.sha256 in text


def test_full_order_is_release_verify_pages(tmp_path: Path) -> None:
    events: list[str] = []
    publisher = Publisher(
        tmp_path / "state",
        lambda _a: events.append("pages"),
        lambda _a: events.append("release"),
        verify_release=lambda _a: events.append("verify"),
    )
    publisher.publish(artifact(tmp_path))
    assert events == ["release", "verify", "pages"]


def test_a_partial_attempt_resumes_without_duplicating_work(tmp_path: Path) -> None:
    events: list[str] = []

    def flaky_pages(_a: Artifact) -> None:
        events.append("pages")
        if events.count("pages") == 1:
            raise RuntimeError("pages push failed")

    publisher = Publisher(
        tmp_path / "state",
        flaky_pages,
        lambda _a: events.append("release"),
        verify_release=lambda _a: events.append("verify"),
    )
    item = artifact(tmp_path)
    with pytest.raises(RuntimeError, match="pages push failed"):
        publisher.publish(item)
    assert Journal.load(tmp_path / "state").phase == "release-verified"
    publisher.publish(item)
    # The asset is uploaded and verified exactly once across both attempts.
    assert events == ["release", "verify", "pages", "pages"]
    assert Journal.load(tmp_path / "state").phase == "complete"


def test_a_completed_publication_is_an_idempotent_noop(tmp_path: Path) -> None:
    events: list[str] = []
    publisher = Publisher(
        tmp_path / "state", lambda _a: events.append("pages"), lambda _a: events.append("release")
    )
    item = artifact(tmp_path)
    publisher.publish(item)
    publisher.publish(item)
    assert events == ["release", "pages"]


def test_a_different_artifact_starts_a_fresh_transaction(tmp_path: Path) -> None:
    events: list[str] = []
    publisher = Publisher(
        tmp_path / "state", lambda _a: events.append("pages"), lambda _a: events.append("release")
    )
    publisher.publish(artifact(tmp_path, "a" * 64))
    publisher.publish(artifact(tmp_path, "c" * 64))
    assert events == ["release", "pages", "release", "pages"]


def test_verification_failure_rolls_the_asset_back(tmp_path: Path) -> None:
    events: list[str] = []

    def verify(_a: Artifact) -> None:
        raise RuntimeError("digest mismatch")

    publisher = Publisher(
        tmp_path / "state",
        lambda _a: events.append("pages"),
        lambda _a: events.append("release"),
        verify_release=verify,
        rollback_release=lambda _a: events.append("rollback"),
    )
    with pytest.raises(RuntimeError, match="digest mismatch"):
        publisher.publish(artifact(tmp_path))
    assert events == ["release", "rollback"]
    # The rollback undid the upload, so a retry starts from the beginning
    # rather than trying to verify an asset that no longer exists.
    assert Journal.load(tmp_path / "state").phase == "started"


def test_a_failed_rollback_is_reported_for_an_operator(tmp_path: Path) -> None:
    def explode(_a: Artifact) -> None:
        raise RuntimeError("asset delete refused")

    publisher = Publisher(
        tmp_path / "state",
        lambda _a: None,
        lambda _a: None,
        verify_release=explode,
        rollback_release=explode,
    )
    with pytest.raises(RollbackError, match="must be removed by hand"):
        publisher.publish(artifact(tmp_path))


def test_journal_ignores_corrupt_state(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "transaction.json").write_text("{not json", encoding="utf-8")
    assert Journal.load(state).phase == ""
    (state / "transaction.json").write_text(json.dumps(["a"]), encoding="utf-8")
    assert Journal.load(state).phase == ""
    (state / "transaction.json").write_text(json.dumps({"phase": "nonsense"}), encoding="utf-8")
    assert Journal.load(state).phase == ""


def test_journal_rejects_an_unknown_phase(tmp_path: Path) -> None:
    journal = Journal.load(tmp_path)
    with pytest.raises(ValueError, match="unknown transaction phase"):
        journal.record("halfway", artifact(tmp_path))


def test_journal_clear_removes_the_file(tmp_path: Path) -> None:
    journal = Journal.load(tmp_path)
    journal.record("started", artifact(tmp_path))
    assert journal.path.is_file()
    journal.clear()
    assert not journal.path.exists()
    journal.clear()


def test_phase_order_is_explicit() -> None:
    assert PHASES.index("release-verified") < PHASES.index("pages-updated")
