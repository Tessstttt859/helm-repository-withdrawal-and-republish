"""Pre-publication validation: nothing becomes discoverable until this passes."""

from __future__ import annotations

import shutil
import subprocess
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml

from chartpub.archive import archive_metadata, inspect_archive, require_digest
from chartpub.errors import ValidationError
from chartpub.models import Artifact

TEST_NAMESPACE = "chartpub-verify"


class HelmRunner(Protocol):
    def __call__(self, args: Sequence[str]) -> tuple[int, str, str]: ...


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass
class ValidationReport:
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)

    @property
    def failures(self) -> list[Check]:
        return [check for check in self.checks if not check.ok]

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append(Check(name, ok, detail))

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "checks": [check.as_dict() for check in self.checks]}

    def raise_for_status(self) -> None:
        if self.ok:
            return
        summary = "; ".join(f"{c.name}: {c.detail}" for c in self.failures)
        raise ValidationError(f"candidate failed validation ({summary})")


def default_helm_runner(args: Sequence[str]) -> tuple[int, str, str]:
    executable = shutil.which("helm")
    if executable is None:
        raise ValidationError("helm 3 is required on PATH to validate a candidate")
    completed = subprocess.run(  # noqa: S603 - fixed executable, list arguments
        [executable, *args], capture_output=True, text=True, check=False
    )
    return completed.returncode, completed.stdout, completed.stderr


def _tail(text: str, limit: int = 400) -> str:
    collapsed = " ".join(text.split())
    return collapsed[-limit:]


WORKLOAD_KINDS = frozenset({"Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job"})


def validate_manifests(documents: Sequence[dict[str, Any]]) -> list[str]:
    """Structural rules a server would reject at install time.

    The staging incident shipped a Deployment whose selector did not match its
    own pod template labels; the API server rejects that, so it is checked here
    rather than after the chart is public.
    """
    problems: list[str] = []
    seen: set[tuple[str, str]] = set()
    for document in documents:
        kind = str(document.get("kind", ""))
        metadata = document.get("metadata") or {}
        name = str(metadata.get("name", ""))
        if not kind or not document.get("apiVersion"):
            problems.append("a rendered document is missing apiVersion or kind")
            continue
        if not name:
            problems.append(f"{kind} is missing metadata.name")
            continue
        key = (kind, name)
        if key in seen:
            problems.append(f"duplicate resource {kind}/{name}")
        seen.add(key)
        if kind not in WORKLOAD_KINDS:
            continue
        spec = document.get("spec") or {}
        selector = (spec.get("selector") or {}).get("matchLabels") or {}
        labels = ((spec.get("template") or {}).get("metadata") or {}).get("labels") or {}
        mismatched = sorted(key for key, value in selector.items() if labels.get(key) != value)
        if mismatched:
            problems.append(
                f"{kind}/{name} selector does not match its pod template labels: "
                + ", ".join(f"{k}={selector[k]!r} vs {labels.get(k)!r}" for k in mismatched)
            )
    return problems


def _load_documents(rendered: str) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    for document in yaml.safe_load_all(rendered):
        if isinstance(document, dict) and document:
            documents.append(document)
    return documents


def validate_candidate(
    artifact: Artifact,
    values_files: Sequence[Path],
    *,
    helm: HelmRunner = default_helm_runner,
    release_name: str | None = None,
) -> ValidationReport:
    """Lint, render, inspect and trial-install a candidate archive."""
    report = ValidationReport()
    archive = artifact.path

    try:
        require_digest(archive, artifact.sha256)
        members = inspect_archive(archive, expected_root=artifact.name)
        names = {member.name for member in members}
        required = {f"{artifact.name}/Chart.yaml", f"{artifact.name}/values.yaml"}
        missing = sorted(required - names)
        if missing:
            raise ValidationError(f"archive is missing {', '.join(missing)}")
        metadata = archive_metadata(archive, artifact.name)
        if str(metadata.get("version")) != artifact.version:
            raise ValidationError(
                f"archived Chart.yaml declares version {metadata.get('version')!r}, "
                f"expected {artifact.version!r}"
            )
        report.add("archive-contents", True, f"{len(members)} safe members")
        report.add("archive-digest", True, artifact.sha256)
    except ValidationError as exc:
        report.add("archive-contents", False, str(exc))
        return report

    code, out, err = helm(["lint", "--strict", str(archive)])
    report.add("helm-lint", code == 0, _tail(err or out))

    for values in values_files:
        code, out, err = helm(["template", artifact.name, str(archive), "--values", str(values)])
        _record_render(report, f"helm-template[{values.name}]", code, out, err)

    code, out, err = helm(["template", artifact.name, str(archive)])
    _record_render(report, "helm-template[defaults]", code, out, err)

    name = release_name or f"chartpub-verify-{uuid.uuid4().hex[:8]}"
    code, out, err = helm(
        ["install", name, str(archive), "--dry-run=client", "--namespace", TEST_NAMESPACE]
    )
    note = ""
    if code != 0 and _cluster_unreachable(err or out):
        # No cluster is reachable from here. Fall back to a cluster-free render
        # of the same isolated release; CI runs the real install against kind.
        note = " (no cluster reachable: rendered without the API server)"
        code, out, err = helm(
            ["template", name, str(archive), "--namespace", TEST_NAMESPACE, "--is-upgrade=false"]
        )
    _record_render(
        report,
        "helm-install[isolated]",
        code,
        out.partition("MANIFEST:")[2] or out,
        err,
        success=f"isolated release {name} installs cleanly{note}",
    )
    return report


def _record_render(
    report: ValidationReport,
    name: str,
    code: int,
    out: str,
    err: str,
    *,
    success: str | None = None,
) -> None:
    """Turn one helm invocation into a single pass/fail check."""
    if code != 0:
        report.add(name, False, _tail(err or out))
        return
    try:
        documents = _load_documents(out)
    except yaml.YAMLError as exc:
        report.add(name, False, f"unparsable output: {exc}")
        return
    problems = validate_manifests(documents)
    report.add(
        name,
        not problems,
        "; ".join(problems) if problems else (success or f"{len(documents)} documents"),
    )


_UNREACHABLE = ("cluster unreachable", "could not get server version", "connection refused")


def _cluster_unreachable(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in _UNREACHABLE)
