"""Typed publication contract and the value objects used across the CLI."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from chartpub.errors import ContractError

REPOSITORY_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)*$")
REF_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")

_STRING_FIELDS = (
    "repository",
    "source_branch",
    "pages_branch",
    "pages_url",
    "chart",
    "bad_version",
    "replacement_version",
    "bad_tag",
    "replacement_tag",
    "expected_bad_tag_target",
    "expected_pages_tip",
    "release_asset_name",
)
_REQUIRED_KEYS = frozenset(("schema_version", *_STRING_FIELDS))
SUPPORTED_SCHEMA_VERSION = 1


def semver_key(version: str) -> tuple[tuple[int, ...], int, str]:
    """Order versions newest-first deterministically, tolerating odd input."""
    core, _, rest = version.partition("-")
    parts: list[int] = []
    for chunk in core.split("."):
        parts.append(int(chunk) if chunk.isdigit() else -1)
    # A release sorts above any pre-release of the same core version.
    return (tuple(parts), 0 if rest else 1, version)


@dataclass(frozen=True)
class PublicationContract:
    """The only source of truth for which public objects may be touched."""

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
        keys = frozenset(raw)
        unknown = sorted(keys - _REQUIRED_KEYS)
        if unknown:
            raise ContractError(f"unknown contract field(s): {', '.join(unknown)}")
        missing = sorted(_REQUIRED_KEYS - keys)
        if missing:
            raise ContractError(f"missing contract field(s): {', '.join(missing)}")

        schema_version = raw["schema_version"]
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise ContractError("schema_version must be an integer")
        if schema_version != SUPPORTED_SCHEMA_VERSION:
            raise ContractError(
                f"schema_version {schema_version} is unsupported "
                f"(expected {SUPPORTED_SCHEMA_VERSION})"
            )
        for name in _STRING_FIELDS:
            value = raw[name]
            if not isinstance(value, str) or not value.strip():
                raise ContractError(f"{name} must be a non-empty string")

        contract = cls(schema_version=schema_version, **{n: raw[n] for n in _STRING_FIELDS})
        contract.validate()
        return contract

    def validate(self) -> None:
        if not REPOSITORY_RE.match(self.repository):
            raise ContractError("repository must be in owner/name form")
        for name in ("source_branch", "pages_branch", "bad_tag", "replacement_tag"):
            value: str = getattr(self, name)
            if not REF_NAME_RE.match(value) or ".." in value:
                raise ContractError(f"{name} is not a valid Git ref name: {value!r}")
        if self.source_branch == self.pages_branch:
            raise ContractError("source_branch and pages_branch must differ")
        if not self.pages_url.startswith("https://"):
            raise ContractError("pages_url must be an https URL")
        if "/" in self.chart or not self.chart.strip():
            raise ContractError("chart must be a bare chart name")
        for name in ("bad_version", "replacement_version"):
            value = getattr(self, name)
            if not VERSION_RE.match(value):
                raise ContractError(f"{name} must be a semantic version: {value!r}")
        if self.bad_version == self.replacement_version:
            raise ContractError("replacement_version must differ from bad_version")
        if self.bad_tag == self.replacement_tag:
            raise ContractError("replacement_tag must differ from bad_tag")
        for name in ("expected_bad_tag_target", "expected_pages_tip"):
            value = getattr(self, name)
            if not SHA1_RE.match(value):
                raise ContractError(f"{name} must be a full 40-character commit sha")
        if "{version}" not in self.release_asset_name:
            raise ContractError("release_asset_name must contain a {version} placeholder")
        if "/" in self.release_asset_name or "\\" in self.release_asset_name:
            raise ContractError("release_asset_name must not contain a path separator")

    def asset_name(self, version: str) -> str:
        return self.release_asset_name.format(version=version)

    def tag_for(self, version: str) -> str:
        if version == self.bad_version:
            return self.bad_tag
        if version == self.replacement_version:
            return self.replacement_tag
        raise ContractError(f"version {version} is outside the contract scope")

    def asset_url(self, version: str) -> str:
        return f"{self.pages_url.rstrip('/')}/{self.asset_name(version)}"

    @property
    def owner(self) -> str:
        return self.repository.split("/", 1)[0]

    @property
    def repo_name(self) -> str:
        return self.repository.split("/", 1)[1]

    @property
    def chart_dir(self) -> Path:
        return Path("charts") / self.chart


@dataclass(frozen=True)
class Artifact:
    """A packaged chart archive and its verified identity."""

    path: Path
    name: str
    version: str
    sha256: str
    size: int


@dataclass(frozen=True)
class RemoteSnapshot:
    """Remote tips captured before any mutation, used for compare-and-swap."""

    main_tip: str | None = None
    pages_tip: str | None = None
    bad_tag_target: str | None = None
    replacement_tag_target: str | None = None
    bad_release_state: str | None = None
    bad_release_id: int | None = None
    replacement_release_state: str | None = None
    replacement_release_id: int | None = None
    index_versions: tuple[str, ...] = ()
    pages_files: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "main_tip": self.main_tip,
            "pages_tip": self.pages_tip,
            "bad_tag_target": self.bad_tag_target,
            "replacement_tag_target": self.replacement_tag_target,
            "bad_release_state": self.bad_release_state,
            "bad_release_id": self.bad_release_id,
            "replacement_release_state": self.replacement_release_state,
            "replacement_release_id": self.replacement_release_id,
            "index_versions": list(self.index_versions),
            "pages_files": list(self.pages_files),
        }


Scope = Literal["local", "release", "tag", "pages"]


@dataclass(frozen=True)
class PlanStep:
    """One reviewable unit of work, grouped by the surface it changes."""

    scope: Scope
    action: str
    target: str
    detail: str = ""
    destructive: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "action": self.action,
            "target": self.target,
            "detail": self.detail,
            "destructive": self.destructive,
        }


@dataclass
class Plan:
    """A deterministic, machine-readable description of intended changes."""

    command: str
    repository: str
    chart: str
    bad_version: str
    replacement_version: str
    dry_run: bool = True
    steps: list[PlanStep] = field(default_factory=list)
    preconditions: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def add(self, step: PlanStep) -> None:
        self.steps.append(step)

    def scope(self, scope: Scope) -> list[PlanStep]:
        return [step for step in self.steps if step.scope == scope]

    @property
    def force_update_required(self) -> bool:
        return any(step.destructive for step in self.steps)

    @property
    def is_noop(self) -> bool:
        return not self.steps

    def as_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "repository": self.repository,
            "chart": self.chart,
            "bad_version": self.bad_version,
            "replacement_version": self.replacement_version,
            "dry_run": self.dry_run,
            "force_update_required": self.force_update_required,
            "no_op": self.is_noop,
            "changes": {
                "local": [s.as_dict() for s in self.scope("local")],
                "github_release": [s.as_dict() for s in self.scope("release")],
                "github_tag": [s.as_dict() for s in self.scope("tag")],
                "pages": [s.as_dict() for s in self.scope("pages")],
            },
            "preconditions": self.preconditions,
            "notes": self.notes,
        }
