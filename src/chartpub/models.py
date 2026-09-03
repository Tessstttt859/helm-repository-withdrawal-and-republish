from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PublicationContract:
    schema_version: int
    repository: str
    source_branch: str
    pages_branch: str
    pages_url: str
    chart: str
    bad_version: str
    replacement_version: str
    bad_tag: str
    replacement_tag: str
    expected_bad_tag_target: str
    expected_pages_tip: str
    release_asset_name: str

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> PublicationContract:
        # This intentionally mirrors the original permissive loader. The
        # recovery work needs to define and enforce the actual contract.
        return cls(**raw)

    def asset_name(self, version: str) -> str:
        return self.release_asset_name.format(version=version)

    @property
    def chart_dir(self) -> Path:
        return Path("charts") / self.chart


@dataclass(frozen=True)
class Artifact:
    path: Path
    name: str
    version: str
    sha256: str
    size: int


@dataclass(frozen=True)
class RemoteSnapshot:
    main_tip: str
    pages_tip: str
    bad_tag_target: str | None
    bad_release_state: str | None

