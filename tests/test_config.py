from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from chartpub.config import load_contract
from chartpub.errors import ContractError
from chartpub.models import PublicationContract, semver_key
from tests.conftest import contract_mapping


def valid_contract() -> dict[str, Any]:
    return contract_mapping(repository="owner/repo", pages_url="https://owner.example/repo")


def write_contract(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def load(tmp_path: Path, value: object) -> PublicationContract:
    path = tmp_path / "contract.json"
    write_contract(path, value)
    return load_contract(path)


def test_loads_complete_contract(tmp_path: Path) -> None:
    contract = load(tmp_path, valid_contract())
    assert contract.replacement_version == "0.4.1"
    assert contract.asset_name("0.4.1") == "ledger-api-0.4.1.tgz"
    assert contract.asset_url("0.4.1") == "https://owner.example/repo/ledger-api-0.4.1.tgz"
    assert contract.tag_for("0.4.0") == "chart-v0.4.0"
    assert contract.tag_for("0.4.1") == "chart-v0.4.1"
    assert contract.owner == "owner"
    assert contract.repo_name == "repo"
    assert contract.chart_dir == Path("charts/ledger-api")


def test_tag_for_refuses_out_of_scope_versions(tmp_path: Path) -> None:
    contract = load(tmp_path, valid_contract())
    with pytest.raises(ContractError, match="outside the contract scope"):
        contract.tag_for("9.9.9")


def test_rejects_non_object(tmp_path: Path) -> None:
    with pytest.raises(ContractError, match="JSON object"):
        load(tmp_path, [])


def test_rejects_unreadable_or_invalid_json(tmp_path: Path) -> None:
    with pytest.raises(ContractError, match="cannot load"):
        load_contract(tmp_path / "absent.json")
    path = tmp_path / "contract.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ContractError, match="cannot load"):
        load_contract(path)


def test_rejects_unknown_key(tmp_path: Path) -> None:
    value = valid_contract()
    value["typo_branch"] = "main"
    with pytest.raises(ContractError, match="unknown"):
        load(tmp_path, value)


def test_rejects_missing_key(tmp_path: Path) -> None:
    value = valid_contract()
    del value["pages_url"]
    with pytest.raises(ContractError, match="missing"):
        load(tmp_path, value)


def test_rejects_boolean_schema_version(tmp_path: Path) -> None:
    value = valid_contract()
    value["schema_version"] = True
    with pytest.raises(ContractError, match="schema_version"):
        load(tmp_path, value)


def test_rejects_unsupported_schema_version(tmp_path: Path) -> None:
    value = valid_contract()
    value["schema_version"] = 2
    with pytest.raises(ContractError, match="unsupported"):
        load(tmp_path, value)


@pytest.mark.parametrize(
    ("field", "bad", "match"),
    [
        ("repository", "no-slash", "owner/name"),
        ("repository", 7, "non-empty string"),
        ("source_branch", "../evil", "valid Git ref"),
        ("bad_tag", "-leading", "valid Git ref"),
        ("pages_url", "http://insecure.example", "https URL"),
        ("chart", "nested/name", "bare chart name"),
        ("bad_version", "0.4", "semantic version"),
        ("replacement_version", "latest", "semantic version"),
        ("expected_bad_tag_target", "abc", "40-character"),
        ("expected_pages_tip", "Z" * 40, "40-character"),
        ("release_asset_name", "ledger-api.tgz", "{version}"),
        ("release_asset_name", "dir/{version}.tgz", "path separator"),
        ("source_branch", "gh-pages", "must differ"),
        ("replacement_version", "0.4.0", "must differ from bad_version"),
        ("replacement_tag", "chart-v0.4.0", "replacement_tag must differ"),
        ("pages_url", "  ", "non-empty string"),
    ],
)
def test_rejects_malformed_fields(tmp_path: Path, field: str, bad: object, match: str) -> None:
    value = valid_contract()
    value[field] = bad
    with pytest.raises(ContractError, match=match):
        load(tmp_path, value)


def test_semver_key_orders_prereleases_below_releases() -> None:
    assert semver_key("1.0.0") > semver_key("1.0.0-rc.1")
    assert semver_key("0.10.0") > semver_key("0.9.0")
    assert semver_key("nonsense") < semver_key("0.0.1")
