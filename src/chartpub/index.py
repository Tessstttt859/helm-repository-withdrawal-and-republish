"""Deterministic Helm repository index generation."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from chartpub.errors import ValidationError
from chartpub.models import Artifact, semver_key

API_VERSION = "v1"
EPOCH = "1970-01-01T00:00:00Z"
#: Entry keys copied from Chart.yaml, matching what `helm repo index` emits.
_METADATA_KEYS = (
    "apiVersion",
    "appVersion",
    "description",
    "home",
    "icon",
    "keywords",
    "kubeVersion",
    "maintainers",
    "sources",
    "type",
)


class _IndentedDumper(yaml.SafeDumper):
    """Indent block sequences so the published index stays diff-friendly."""

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        super().increase_indent(flow, False)


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def empty_index() -> dict[str, Any]:
    return {"apiVersion": API_VERSION, "entries": {}, "generated": EPOCH}


def load_index(path: Path) -> dict[str, Any]:
    if not path.exists():
        return empty_index()
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return empty_index()
    try:
        value = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValidationError(f"index is not valid YAML: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("index must be a mapping")
    value.setdefault("apiVersion", API_VERSION)
    entries = value.setdefault("entries", {})
    if not isinstance(entries, dict):
        raise ValueError("index entries must be a mapping")
    for chart, versions in entries.items():
        if not isinstance(versions, list):
            raise ValueError(f"index entries for {chart} must be a list")
    return value


def entry_for(
    artifact: Artifact,
    base_url: str,
    *,
    metadata: dict[str, Any] | None = None,
    created: str | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "apiVersion": "v2",
        "name": artifact.name,
        "version": artifact.version,
        "digest": artifact.sha256,
        "urls": [f"{base_url.rstrip('/')}/{artifact.path.name}"],
        "created": created or utc_now(),
    }
    for key in _METADATA_KEYS:
        value = (metadata or {}).get(key)
        if value not in (None, "", [], {}):
            entry[key] = value
    return {key: entry[key] for key in sorted(entry)}


def add_artifact(
    index: dict[str, Any],
    artifact: Artifact,
    base_url: str,
    *,
    metadata: dict[str, Any] | None = None,
    created: str | None = None,
    allow_replace: bool = False,
) -> dict[str, Any]:
    """Add or refresh one chart version. Re-adding identical input is a no-op."""
    entries = index.setdefault("entries", {})
    versions: list[dict[str, Any]] = entries.setdefault(artifact.name, [])
    existing = next((item for item in versions if item.get("version") == artifact.version), None)
    if existing is not None:
        if existing.get("digest") == artifact.sha256:
            created = created or str(existing.get("created") or utc_now())
        elif not allow_replace:
            raise ValidationError(
                f"{artifact.name} {artifact.version} is already published with a "
                "different digest; withdraw it before republishing"
            )
        versions.remove(existing)
    versions.append(entry_for(artifact, base_url, metadata=metadata, created=created))
    return normalize(index)


def remove_version(index: dict[str, Any], chart: str, version: str) -> dict[str, Any]:
    """Remove exactly one chart version, preserving every other entry."""
    entries = index.setdefault("entries", {})
    remaining = [item for item in entries.get(chart, []) if item.get("version") != version]
    if remaining:
        entries[chart] = remaining
    else:
        entries.pop(chart, None)
    return normalize(index)


def has_version(index: dict[str, Any], chart: str, version: str) -> bool:
    return any(item.get("version") == version for item in index.get("entries", {}).get(chart, []))


def versions(index: dict[str, Any], chart: str) -> tuple[str, ...]:
    return tuple(str(item.get("version")) for item in index.get("entries", {}).get(chart, []))


def referenced_files(index: dict[str, Any]) -> tuple[str, ...]:
    names: set[str] = set()
    for entries in index.get("entries", {}).values():
        for item in entries:
            for url in item.get("urls", []):
                names.add(str(url).rsplit("/", 1)[-1])
    return tuple(sorted(names))


def normalize(index: dict[str, Any]) -> dict[str, Any]:
    """Impose the canonical shape: sorted charts, newest-first versions."""
    entries: dict[str, list[dict[str, Any]]] = index.get("entries", {}) or {}
    normalized: dict[str, list[dict[str, Any]]] = {}
    created_values: list[str] = []
    for chart in sorted(entries):
        items = sorted(
            ({key: item[key] for key in sorted(item)} for item in entries[chart]),
            key=lambda item: semver_key(str(item.get("version", ""))),
            reverse=True,
        )
        normalized[chart] = items
        created_values.extend(str(item["created"]) for item in items if item.get("created"))
    index["apiVersion"] = index.get("apiVersion") or API_VERSION
    index["entries"] = normalized
    # `generated` is a pure function of the entries, so an unchanged repository
    # regenerates a byte-identical index.
    index["generated"] = (
        max(created_values) if created_values else str(index.get("generated") or EPOCH)
    )
    return {key: index[key] for key in sorted(index)}


def dump_index(index: dict[str, Any]) -> str:
    return yaml.dump(
        normalize(dict(index)),
        Dumper=_IndentedDumper,
        sort_keys=True,
        default_flow_style=False,
        width=4096,
        allow_unicode=True,
    )


def write_index(path: Path, index: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_index(index), encoding="utf-8")
