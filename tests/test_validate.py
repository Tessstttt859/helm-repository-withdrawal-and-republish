from __future__ import annotations

import shutil
from collections.abc import Sequence
from pathlib import Path

import pytest

from chartpub.archive import package_chart
from chartpub.errors import ValidationError
from chartpub.validate import (
    ValidationReport,
    default_helm_runner,
    validate_candidate,
    validate_manifests,
)

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm 3 is required")

MISMATCHED = [
    {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "ledger"},
        "spec": {
            "selector": {"matchLabels": {"app": "ledger", "component": "api"}},
            "template": {"metadata": {"labels": {"app": "ledger", "component": "worker"}}},
        },
    }
]


def test_selector_mismatch_is_caught() -> None:
    problems = validate_manifests(MISMATCHED)
    assert problems and "selector does not match" in problems[0]


def test_matching_selector_passes() -> None:
    documents = [
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "ledger"},
            "spec": {
                "selector": {"matchLabels": {"app": "ledger"}},
                "template": {"metadata": {"labels": {"app": "ledger", "extra": "ok"}}},
            },
        }
    ]
    assert validate_manifests(documents) == []


def test_structural_problems_are_reported() -> None:
    problems = validate_manifests(
        [
            {"kind": "Service", "metadata": {"name": "a"}},
            {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "a"}},
            {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "a"}},
            {"apiVersion": "v1", "kind": "Service", "metadata": {}},
        ]
    )
    assert any("apiVersion or kind" in problem for problem in problems)
    assert any("duplicate resource" in problem for problem in problems)
    assert any("metadata.name" in problem for problem in problems)


def test_candidate_passes_every_gate(
    tmp_path: Path, chart_dir: Path, values_files: tuple[Path, ...]
) -> None:
    artifact = package_chart(chart_dir, tmp_path, "0.4.1")
    report = validate_candidate(artifact, values_files)
    assert report.ok, report.as_dict()
    names = [check.name for check in report.checks]
    assert "helm-lint" in names
    assert "helm-install[isolated]" in names
    assert all(f"helm-template[{values.name}]" in names for values in values_files)
    report.raise_for_status()


def test_a_broken_chart_fails_before_publication(
    tmp_path: Path, chart_dir: Path, values_files: tuple[Path, ...]
) -> None:
    staged = tmp_path / "ledger-api"
    shutil.copytree(chart_dir, staged)
    deployment = staged / "templates" / "deployment.yaml"
    deployment.write_text(
        deployment.read_text(encoding="utf-8").replace(
            "app.kubernetes.io/component: api\n  template",
            "app.kubernetes.io/component: api\n  template",
        ),
        encoding="utf-8",
    )
    text = deployment.read_text(encoding="utf-8")
    head, _, tail = text.partition("  template:")
    deployment.write_text(
        head + "  template:" + tail.replace("component: api", "component: worker", 1),
        encoding="utf-8",
    )
    artifact = package_chart(staged, tmp_path / "out", "0.4.2")
    report = validate_candidate(artifact, values_files)
    assert not report.ok
    assert any("selector does not match" in check.detail for check in report.failures)
    with pytest.raises(ValidationError, match="failed validation"):
        report.raise_for_status()


def test_digest_mismatch_stops_validation_immediately(tmp_path: Path, chart_dir: Path) -> None:
    artifact = package_chart(chart_dir, tmp_path, "0.4.1")
    tampered = artifact.__class__(artifact.path, artifact.name, artifact.version, "0" * 64, 1)
    report = validate_candidate(tampered, ())
    assert not report.ok
    assert [check.name for check in report.checks] == ["archive-contents"]


def test_version_mismatch_is_rejected(tmp_path: Path, chart_dir: Path) -> None:
    artifact = package_chart(chart_dir, tmp_path, "0.4.1")
    claimed = artifact.__class__(
        artifact.path, artifact.name, "9.9.9", artifact.sha256, artifact.size
    )
    report = validate_candidate(claimed, ())
    assert not report.ok
    assert "declares version" in report.failures[0].detail


def test_helm_failures_surface_in_the_report(tmp_path: Path, chart_dir: Path) -> None:
    artifact = package_chart(chart_dir, tmp_path, "0.4.1")

    def failing(args: Sequence[str]) -> tuple[int, str, str]:
        return 1, "", f"simulated failure for {args[0]}"

    report = validate_candidate(artifact, (tmp_path / "values.yaml",), helm=failing)
    assert not report.ok
    assert {check.name for check in report.failures} >= {"helm-lint", "helm-install[isolated]"}


def test_unparsable_render_is_reported(tmp_path: Path, chart_dir: Path) -> None:
    artifact = package_chart(chart_dir, tmp_path, "0.4.1")
    values = tmp_path / "values.yaml"
    values.write_text("replicaCount: 1\n", encoding="utf-8")

    def broken(args: Sequence[str]) -> tuple[int, str, str]:
        if args[0] == "template":
            return 0, "a: [1,\n", ""
        return 0, "", ""

    report = validate_candidate(artifact, (values,), helm=broken)
    assert any("unparsable" in check.detail for check in report.failures)


def test_report_helpers() -> None:
    report = ValidationReport()
    report.add("ok-check", True, "fine")
    assert report.ok and report.failures == []
    report.raise_for_status()


def test_default_runner_requires_helm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("chartpub.validate.shutil.which", lambda _name: None)
    with pytest.raises(ValidationError, match="helm 3 is required"):
        default_helm_runner(["version"])
