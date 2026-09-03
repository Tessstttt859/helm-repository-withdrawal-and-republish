from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from chartpub.models import Artifact


def load_index(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"apiVersion": "v1", "entries": {}}
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("index must be a mapping")
    return value


def add_artifact(index: dict[str, Any], artifact: Artifact, base_url: str) -> dict[str, Any]:
    entries = index.setdefault("entries", {})
    versions = entries.setdefault(artifact.name, [])
    versions.append(
        {
            "apiVersion": "v2",
            "name": artifact.name,
            "version": artifact.version,
            "digest": artifact.sha256,
            "urls": [f"{base_url.rstrip('/')}/{artifact.path.name}"],
            "created": datetime.now(UTC).isoformat(),
        }
    )
    versions.sort(key=lambda item: item["version"], reverse=True)
    index["generated"] = datetime.now(UTC).isoformat()
    return index


def remove_version(index: dict[str, Any], chart: str, version: str) -> dict[str, Any]:
    entries = index.get("entries", {})
    entries[chart] = [item for item in entries.get(chart, []) if item.get("version") != version]
    return index


def write_index(path: Path, index: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(index, sort_keys=False), encoding="utf-8")

