from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from chartpub.errors import ValidationError
from chartpub.index import (
    add_artifact,
    dump_index,
    empty_index,
    entry_for,
    has_version,
    load_index,
    normalize,
    referenced_files,
    remove_version,
    versions,
    write_index,
)
from chartpub.models import Artifact

BASE = "https://example.test/charts"


def artifact(version: str = "0.4.1", digest: str = "a" * 64) -> Artifact:
    return Artifact(Path(f"ledger-api-{version}.tgz"), "ledger-api", version, digest, 42)


def fresh() -> dict[str, Any]:
    return empty_index()


def test_add_is_idempotent() -> None:
    index = fresh()
    add_artifact(index, artifact(), BASE, created="2026-01-01T00:00:00Z")
    first = dump_index(index)
    add_artifact(index, artifact(), BASE)
    assert len(index["entries"]["ledger-api"]) == 1
    assert dump_index(index) == first


def test_remove_preserves_other_versions() -> None:
    index = fresh()
    add_artifact(index, artifact("0.4.0"), BASE, created="2026-01-01T00:00:00Z")
    add_artifact(index, artifact("0.3.0"), BASE, created="2026-01-01T00:00:00Z")
    remove_version(index, "ledger-api", "0.4.0")
    assert [item["version"] for item in index["entries"]["ledger-api"]] == ["0.3.0"]


def test_remove_last_version_drops_the_chart() -> None:
    index = fresh()
    add_artifact(index, artifact("0.4.0"), BASE, created="2026-01-01T00:00:00Z")
    remove_version(index, "ledger-api", "0.4.0")
    assert index["entries"] == {}
    assert versions(index, "ledger-api") == ()


def test_versions_are_newest_first() -> None:
    index = fresh()
    for version in ("0.9.0", "0.10.0", "0.4.1", "0.4.1-rc.1", "1.0.0"):
        add_artifact(index, artifact(version), BASE, created="2026-01-01T00:00:00Z")
    assert versions(index, "ledger-api") == (
        "1.0.0",
        "0.10.0",
        "0.9.0",
        "0.4.1",
        "0.4.1-rc.1",
    )


def test_charts_are_sorted_and_output_is_stable() -> None:
    index = fresh()
    add_artifact(index, artifact("0.4.1"), BASE, created="2026-01-01T00:00:00Z")
    index["entries"]["another"] = [
        {
            "name": "another",
            "version": "1.0.0",
            "digest": "b" * 64,
            "created": "2026-01-01T00:00:00Z",
        }
    ]
    text = dump_index(index)
    assert list(yaml.safe_load(text)["entries"]) == ["another", "ledger-api"]
    assert dump_index(yaml.safe_load(text)) == text


def test_generated_is_derived_from_entries() -> None:
    index = fresh()
    add_artifact(index, artifact("0.4.0"), BASE, created="2026-01-01T00:00:00Z")
    add_artifact(index, artifact("0.4.1"), BASE, created="2026-02-02T00:00:00Z")
    assert index["generated"] == "2026-02-02T00:00:00Z"


def test_replacing_a_digest_requires_withdrawal() -> None:
    index = fresh()
    add_artifact(index, artifact(digest="a" * 64), BASE, created="2026-01-01T00:00:00Z")
    with pytest.raises(ValidationError, match="different digest"):
        add_artifact(index, artifact(digest="b" * 64), BASE)
    add_artifact(index, artifact(digest="b" * 64), BASE, allow_replace=True)
    assert index["entries"]["ledger-api"][0]["digest"] == "b" * 64


def test_entry_carries_chart_metadata() -> None:
    entry = entry_for(
        artifact(),
        BASE,
        metadata={"appVersion": "1.8.2", "description": "d", "icon": "", "keywords": []},
        created="2026-01-01T00:00:00Z",
    )
    assert entry["appVersion"] == "1.8.2"
    assert "icon" not in entry and "keywords" not in entry
    assert entry["urls"] == [f"{BASE}/ledger-api-0.4.1.tgz"]
    assert list(entry) == sorted(entry)


def test_entry_created_defaults_to_now() -> None:
    entry = entry_for(artifact(), BASE)
    assert entry["created"].endswith("Z")


def test_referenced_files_and_has_version() -> None:
    index = fresh()
    add_artifact(index, artifact("0.4.1"), BASE, created="2026-01-01T00:00:00Z")
    assert referenced_files(index) == ("ledger-api-0.4.1.tgz",)
    assert has_version(index, "ledger-api", "0.4.1")
    assert not has_version(index, "ledger-api", "0.4.0")


def test_load_index_round_trips(tmp_path: Path) -> None:
    index = fresh()
    add_artifact(index, artifact(), BASE, created="2026-01-01T00:00:00Z")
    path = tmp_path / "index.yaml"
    write_index(path, index)
    assert load_index(path) == normalize(index)


def test_load_index_defaults_when_absent_or_empty(tmp_path: Path) -> None:
    assert load_index(tmp_path / "missing.yaml")["entries"] == {}
    blank = tmp_path / "blank.yaml"
    blank.write_text("\n", encoding="utf-8")
    assert load_index(blank)["entries"] == {}


def test_load_index_rejects_bad_shapes(tmp_path: Path) -> None:
    path = tmp_path / "index.yaml"
    path.write_text("- a\n", encoding="utf-8")
    with pytest.raises(ValueError, match="mapping"):
        load_index(path)
    path.write_text("entries: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="entries must be a mapping"):
        load_index(path)
    path.write_text("entries:\n  ledger-api: 3\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a list"):
        load_index(path)
    path.write_text("entries: {\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="valid YAML"):
        load_index(path)
