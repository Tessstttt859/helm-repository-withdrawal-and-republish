from __future__ import annotations

import json
from pathlib import Path

import pytest

from chartpub.config import load_contract
from chartpub.errors import ContractError


def valid_contract() -> dict[str, object]:
    return {
        "schema_version": 1,
        "repository": "owner/repo",
        "source_branch": "main",
        "pages_branch": "gh-pages",
        "pages_url": "https://owner.example/repo",
        "chart": "ledger-api",
        "bad_version": "0.4.0",
        "replacement_version": "0.4.1",
        "bad_tag": "chart-v0.4.0",
        "replacement_tag": "chart-v0.4.1",
        "expected_bad_tag_target": "a" * 40,
        "expected_pages_tip": "b" * 40,
        "release_asset_name": "ledger-api-{version}.tgz",
    }


def write_contract(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def test_loads_complete_contract(tmp_path: Path) -> None:
    path = tmp_path / "contract.json"
    write_contract(path, valid_contract())
    assert load_contract(path).replacement_version == "0.4.1"


def test_rejects_non_object(tmp_path: Path) -> None:
    path = tmp_path / "contract.json"
    write_contract(path, [])
    with pytest.raises(ContractError, match="JSON object"):
        load_contract(path)


def test_rejects_unknown_key(tmp_path: Path) -> None:
    path = tmp_path / "contract.json"
    value = valid_contract()
    value["typo_branch"] = "main"
    write_contract(path, value)
    with pytest.raises(ContractError, match="unknown"):
        load_contract(path)


def test_rejects_boolean_schema_version(tmp_path: Path) -> None:
    path = tmp_path / "contract.json"
    value = valid_contract()
    value["schema_version"] = True
    write_contract(path, value)
    with pytest.raises(ContractError, match="schema_version"):
        load_contract(path)

