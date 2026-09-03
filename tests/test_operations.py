"""End-to-end lifecycle tests against a mocked GitHub and a local bare remote."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

from chartpub.archive import package_chart, sha256_bytes
from chartpub.errors import RemoteConflict, UsageError, ValidationError
from chartpub.index import dump_index, empty_index
from chartpub.models import Artifact, PublicationContract
from chartpub.operations import Operations, Settings
from chartpub.pages import snapshot_files
from tests.conftest import (
    BAD_TAG_SHA,
    MAIN_SHA,
    PAGES_URL,
    contract_mapping,
    make_operations,
    pages_tip,
    published_files,
    published_index,
    seed_pages,
)
from tests.fakes import FakeGitHub

BAD = "0.4.0"
NEW = "0.4.1"
OLD = "0.3.0"
MUTATIONS = ("POST", "PATCH", "DELETE", "PUT")


@dataclass
class Incident:
    ops: Operations
    fake: FakeGitHub
    remote: Path
    tmp_path: Path
    archives: dict[str, bytes]

    def index(self) -> dict[str, Any]:
        return published_index(self.remote, self.tmp_path)

    def files(self) -> set[str]:
        return published_files(self.remote, self.tmp_path)

    def versions(self) -> list[str]:
        entries = self.index().get("entries", {}).get("ledger-api", [])
        return [str(item["version"]) for item in entries]

    def mutating_calls(self) -> list[tuple[str, str]]:
        return [call for call in self.fake.calls if call[0] in MUTATIONS]


def _index_text(entries: dict[str, tuple[str, str]]) -> str:
    index = empty_index()
    index["entries"]["ledger-api"] = [
        {
            "apiVersion": "v2",
            "appVersion": "1.8.2",
            "created": created,
            "description": "Example ledger HTTP API used by the publication recovery exercise",
            "digest": digest,
            "name": "ledger-api",
            "type": "application",
            "urls": [f"{PAGES_URL}/ledger-api-{version}.tgz"],
            "version": version,
        }
        for version, (digest, created) in sorted(entries.items())
    ]
    return dump_index(index)


@pytest.fixture
def incident(
    work_root: Path,
    pages_remote: Path,
    fake_github: FakeGitHub,
    values_files: tuple[Path, ...],
    tmp_path: Path,
) -> Iterator[Incident]:
    """The exact staging state: 0.3.0 healthy, 0.4.0 published but broken."""
    build = tmp_path / "build"
    archives = {
        f"ledger-api-{version}.tgz": package_chart(
            work_root / "charts" / "ledger-api", build, version
        ).path.read_bytes()
        for version in (OLD, BAD)
    }
    fake_github.refs["heads/main"] = MAIN_SHA
    fake_github.refs[f"tags/chart-v{OLD}"] = "d" * 40
    fake_github.refs[f"tags/chart-v{BAD}"] = BAD_TAG_SHA
    fake_github.add_release(
        f"chart-v{OLD}", assets={f"ledger-api-{OLD}.tgz": archives[f"ledger-api-{OLD}.tgz"]}
    )
    fake_github.add_release(
        f"chart-v{BAD}", assets={f"ledger-api-{BAD}.tgz": archives[f"ledger-api-{BAD}.tgz"]}
    )
    tip = seed_pages(
        pages_remote,
        tmp_path,
        snapshot_files(
            _index_text(
                {
                    OLD: (sha256_bytes(archives[f"ledger-api-{OLD}.tgz"]), "2026-01-01T00:00:00Z"),
                    BAD: (sha256_bytes(archives[f"ledger-api-{BAD}.tgz"]), "2026-02-01T00:00:00Z"),
                }
            ),
            archives,
            chart="ledger-api",
            url=PAGES_URL,
        ),
        message="publish 0.4.0",
    )
    contract = PublicationContract.from_mapping(contract_mapping(expected_pages_tip=tip))
    ops = make_operations(
        work_root=work_root,
        pages_remote=pages_remote,
        fake=fake_github,
        values=values_files,
        contract=contract,
    )
    yield Incident(ops, fake_github, pages_remote, tmp_path, archives)
    ops.close()


# ----------------------------------------------------------------- diagnosis


def test_audit_reports_every_incident_symptom(incident: Incident) -> None:
    report = incident.ops.audit()
    assert report["consistent"] is False
    assert report["advertised_versions"] == [OLD, BAD]
    assert report["published_versions"] == [OLD, BAD]
    assert report["remote"]["bad_release_state"] == "published"
    assert report["remote"]["bad_tag_target"] == BAD_TAG_SHA
    scopes = {finding["scope"] for finding in report["findings"]}
    assert scopes == {"release", "tag"}


def test_plan_groups_changes_by_surface(incident: Incident) -> None:
    plan = incident.ops.plan_lifecycle().as_dict()
    changes = plan["changes"]
    assert [step["action"] for step in changes["local"]] == ["package", "validate"]
    assert {step["action"] for step in changes["github_tag"]} == {"delete", "create"}
    assert {step["action"] for step in changes["github_release"]} == {
        "quarantine",
        "create",
        "upload",
    }
    assert plan["force_update_required"] is True


def test_plan_for_a_single_command_uses_live_state(incident: Incident) -> None:
    plan = incident.ops.plan("withdraw").as_dict()
    assert plan["command"] == "withdraw"
    assert plan["preconditions"]["pages_tip"] == pages_tip(incident.remote)
    assert incident.mutating_calls() == []


# ------------------------------------------------------------------ withdraw


def test_withdraw_touches_only_the_bad_version(incident: Incident) -> None:
    before = incident.fake.release_by_tag(f"chart-v{BAD}")
    assert before is not None
    result = incident.ops.withdraw()

    quarantined = incident.fake.release_by_tag(f"chart-v{BAD}")
    assert quarantined is not None
    assert quarantined["id"] == before["id"]
    assert quarantined["tag_name"] == f"chart-v{BAD}"
    assert quarantined["draft"] is True
    assert [a["name"] for a in quarantined["assets"]] == [f"ledger-api-{BAD}.tgz"]
    assert "WITHDRAWN" in quarantined["body"]

    assert f"tags/chart-v{BAD}" not in incident.fake.refs
    assert incident.fake.refs[f"tags/chart-v{OLD}"] == "d" * 40
    healthy = incident.fake.release_by_tag(f"chart-v{OLD}")
    assert healthy is not None and healthy["draft"] is False

    assert incident.versions() == [OLD]
    assert incident.files() == {"index.yaml", "README.md", f"ledger-api-{OLD}.tgz"}
    assert result["withdrawn"]["pages"]["new_tip"] == pages_tip(incident.remote)
    assert result["withdrawn"]["release"]["draft"] is True


def test_withdrawn_index_keeps_the_healthy_entry_intact(incident: Incident) -> None:
    before = incident.index()["entries"]["ledger-api"]
    kept = next(item for item in before if item["version"] == OLD)
    incident.ops.withdraw()
    after = incident.index()["entries"]["ledger-api"]
    assert after == [kept]


def test_withdraw_dry_run_performs_no_remote_write(
    work_root: Path, pages_remote: Path, fake_github: FakeGitHub, incident: Incident
) -> None:
    incident.ops.settings.dry_run = True
    tip = pages_tip(incident.remote)
    incident.fake.calls.clear()
    result = incident.ops.withdraw()
    assert result["dry_run"] is True
    assert result["withdrawn"] == {}
    assert [step["action"] for step in result["plan"]["changes"]["github_release"]] == [
        "quarantine"
    ]
    assert incident.mutating_calls() == []
    assert pages_tip(incident.remote) == tip
    release = incident.fake.release_by_tag(f"chart-v{BAD}")
    assert release is not None and release["draft"] is False
    assert f"tags/chart-v{BAD}" in incident.fake.refs


def test_withdraw_stops_before_mutating_when_the_tag_moved(incident: Incident) -> None:
    incident.fake.refs[f"tags/chart-v{BAD}"] = "9" * 40
    with pytest.raises(RemoteConflict, match="nothing was changed"):
        incident.ops.withdraw()
    release = incident.fake.release_by_tag(f"chart-v{BAD}")
    assert release is not None and release["draft"] is False
    assert incident.versions() == [BAD, OLD]


def test_withdraw_stops_before_mutating_when_pages_moved(incident: Incident) -> None:
    seed_pages(incident.remote, incident.tmp_path, {"index.yaml": b"apiVersion: v1\n"}, "moved")
    with pytest.raises(RemoteConflict, match="nothing was changed"):
        incident.ops.withdraw()
    release = incident.fake.release_by_tag(f"chart-v{BAD}")
    assert release is not None and release["draft"] is False


def test_withdraw_accepts_observed_tips_with_allow_drift(incident: Incident) -> None:
    incident.fake.refs[f"tags/chart-v{BAD}"] = "9" * 40
    incident.ops.settings.allow_drift = True
    incident.ops.withdraw()
    assert f"tags/chart-v{BAD}" not in incident.fake.refs
    assert incident.versions() == [OLD]


def test_withdraw_is_idempotent(incident: Incident) -> None:
    incident.ops.withdraw()
    tip = pages_tip(incident.remote)
    again = incident.ops.withdraw()
    assert again["withdrawn"] == {"status": "already withdrawn"}
    assert pages_tip(incident.remote) == tip


def test_withdraw_refuses_a_release_that_was_retagged_concurrently(
    incident: Incident, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = incident.ops.client
    assert client is not None
    monkeypatch.setattr(
        client, "get_release", lambda _id: {"id": _id, "tag_name": "chart-v9.9.9", "body": ""}
    )
    with pytest.raises(RemoteConflict, match="is tagged"):
        incident.ops.withdraw()
    assert incident.versions() == [BAD, OLD]


# ------------------------------------------------------------------- publish


def _withdraw_then(incident: Incident) -> None:
    incident.ops.withdraw()
    incident.ops.settings.allow_drift = True


def test_publish_creates_tag_release_asset_and_index(incident: Incident) -> None:
    _withdraw_then(incident)
    result = incident.ops.publish(target_commit=MAIN_SHA)

    assert result["validation"]["ok"] is True
    assert incident.fake.refs[f"tags/chart-v{NEW}"] == MAIN_SHA
    release = incident.fake.release_by_tag(f"chart-v{NEW}")
    assert release is not None and release["draft"] is False
    asset = release["assets"][0]
    uploaded = incident.fake.assets[int(asset["id"])]
    assert sha256_bytes(uploaded) == result["artifact"]["sha256"]

    assert incident.versions() == [NEW, OLD]
    entry = next(
        item for item in incident.index()["entries"]["ledger-api"] if item["version"] == NEW
    )
    assert entry["digest"] == result["artifact"]["sha256"]
    assert entry["urls"] == [f"{PAGES_URL}/ledger-api-{NEW}.tgz"]
    assert entry["appVersion"] == "1.8.2"
    assert f"ledger-api-{NEW}.tgz" in incident.files()
    assert result["journal"]["phase"] == "complete"


def test_published_archive_matches_the_release_asset_byte_for_byte(incident: Incident) -> None:
    _withdraw_then(incident)
    incident.ops.publish(target_commit=MAIN_SHA)
    release = incident.fake.release_by_tag(f"chart-v{NEW}")
    assert release is not None
    uploaded = incident.fake.assets[int(release["assets"][0]["id"])]
    checkout = incident.tmp_path / "verify-bytes"
    shutil.rmtree(checkout, ignore_errors=True)
    from tests.conftest import git

    git("clone", "--branch", "gh-pages", str(incident.remote), str(checkout), cwd=incident.tmp_path)
    assert (checkout / f"ledger-api-{NEW}.tgz").read_bytes() == uploaded


def test_publish_dry_run_performs_no_remote_write(incident: Incident) -> None:
    incident.ops.settings.dry_run = True
    tip = pages_tip(incident.remote)
    incident.fake.calls.clear()
    result = incident.ops.publish()
    assert result["dry_run"] is True
    assert result["validation"]["ok"] is True
    assert "published" not in result
    assert incident.mutating_calls() == []
    assert pages_tip(incident.remote) == tip
    assert f"tags/chart-v{NEW}" not in incident.fake.refs


def test_validation_failure_leaves_the_public_index_untouched(incident: Incident) -> None:
    deployment = (
        incident.ops.settings.repo_root / "charts" / "ledger-api" / "templates" / "deployment.yaml"
    )
    head, _, tail = deployment.read_text(encoding="utf-8").partition("  template:")
    deployment.write_text(
        head + "  template:" + tail.replace("component: api", "component: worker", 1),
        encoding="utf-8",
    )
    tip = pages_tip(incident.remote)
    incident.fake.calls.clear()
    with pytest.raises(ValidationError, match="failed validation"):
        incident.ops.publish(target_commit=MAIN_SHA)
    assert incident.mutating_calls() == []
    assert pages_tip(incident.remote) == tip
    assert incident.versions() == [BAD, OLD]


def test_digest_mismatch_rolls_the_asset_back_and_leaves_pages_alone(incident: Incident) -> None:
    _withdraw_then(incident)
    incident.fake.served[f"ledger-api-{NEW}.tgz"] = b"corrupted-download"
    tip = pages_tip(incident.remote)
    with pytest.raises(ValidationError, match="does not match"):
        incident.ops.publish(target_commit=MAIN_SHA)
    release = incident.fake.release_by_tag(f"chart-v{NEW}")
    assert release is not None and release["assets"] == []
    assert pages_tip(incident.remote) == tip
    assert incident.versions() == [OLD]

    # The retry reuses the same release and does not duplicate the asset.
    incident.fake.served.clear()
    incident.ops.publish(target_commit=MAIN_SHA)
    release = incident.fake.release_by_tag(f"chart-v{NEW}")
    assert release is not None and len(release["assets"]) == 1
    assert incident.versions() == [NEW, OLD]


def test_a_partial_attempt_resumes_without_duplicating_anything(
    incident: Incident, monkeypatch: pytest.MonkeyPatch
) -> None:
    _withdraw_then(incident)
    original = Operations.write_pages_snapshot
    calls = {"n": 0}

    def flaky(self: Operations, *args: Any, **kwargs: Any) -> str | None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("pages push interrupted")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Operations, "write_pages_snapshot", flaky)
    with pytest.raises(RuntimeError, match="pages push interrupted"):
        incident.ops.publish(target_commit=MAIN_SHA)
    assert incident.ops.journal().phase == "release-verified"
    assert incident.versions() == [OLD]

    incident.ops.publish(target_commit=MAIN_SHA)
    release = incident.fake.release_by_tag(f"chart-v{NEW}")
    assert release is not None and len(release["assets"]) == 1
    assert incident.versions() == [NEW, OLD]
    assert incident.ops.journal().phase == "complete"


def test_republishing_the_same_input_is_a_noop(incident: Incident) -> None:
    _withdraw_then(incident)
    incident.ops.publish(target_commit=MAIN_SHA)
    index_before = incident.index()
    tip = pages_tip(incident.remote)
    incident.ops.publish(target_commit=MAIN_SHA)
    assert incident.index() == index_before
    assert pages_tip(incident.remote) == tip
    release = incident.fake.release_by_tag(f"chart-v{NEW}")
    assert release is not None and len(release["assets"]) == 1


def test_publish_replaces_a_truncated_asset_from_an_earlier_attempt(incident: Incident) -> None:
    _withdraw_then(incident)
    release = incident.fake.add_release(f"chart-v{NEW}")
    incident.fake._attach(release, f"ledger-api-{NEW}.tgz", b"truncated")
    incident.ops.publish(target_commit=MAIN_SHA)
    release = incident.fake.release_by_tag(f"chart-v{NEW}")
    assert release is not None and len(release["assets"]) == 1
    assert incident.fake.assets[int(release["assets"][0]["id"])] != b"truncated"


def test_publish_reopens_a_draft_replacement_release(incident: Incident) -> None:
    _withdraw_then(incident)
    incident.fake.add_release(f"chart-v{NEW}", draft=True)
    incident.ops.publish(target_commit=MAIN_SHA)
    release = incident.fake.release_by_tag(f"chart-v{NEW}")
    assert release is not None and release["draft"] is False


def test_publish_stops_when_the_replacement_tag_already_moved(incident: Incident) -> None:
    _withdraw_then(incident)
    incident.ops.settings.allow_drift = False
    incident.fake.refs[f"tags/chart-v{NEW}"] = "e" * 40
    with pytest.raises(RemoteConflict, match="already exists"):
        incident.ops.publish(target_commit=MAIN_SHA)


def test_publish_stops_when_pages_moves_underneath_it(
    incident: Incident, monkeypatch: pytest.MonkeyPatch
) -> None:
    _withdraw_then(incident)
    original = Operations.desired_pages

    def racing(self: Operations, **kwargs: Any) -> Any:
        result = original(self, **kwargs)
        seed_pages(incident.remote, incident.tmp_path, {"index.yaml": b"raced\n"}, "raced")
        return result

    monkeypatch.setattr(Operations, "desired_pages", racing)
    with pytest.raises(RemoteConflict, match="refusing to overwrite concurrent work"):
        incident.ops.publish(target_commit=MAIN_SHA)


def test_publish_needs_a_source_commit(incident: Incident) -> None:
    _withdraw_then(incident)
    incident.fake.refs.pop("heads/main")
    with pytest.raises(Exception, match="cannot resolve the source commit"):
        incident.ops.publish(target_commit=None)


def test_package_enforces_the_contract_asset_name(incident: Incident) -> None:
    contract = PublicationContract.from_mapping(
        contract_mapping(release_asset_name="other-{version}.tgz")
    )
    incident.ops.settings = Settings(
        contract=contract,
        repo_root=incident.ops.settings.repo_root,
        state_dir=incident.ops.settings.state_dir,
        helm=incident.ops.settings.helm,
        dry_run=False,
    )
    with pytest.raises(ValidationError, match="contract expects"):
        incident.ops.package(NEW)


# -------------------------------------------------------------------- repair


def test_repair_rebuilds_a_lost_pages_branch_from_release_assets(incident: Incident) -> None:
    incident.ops.withdraw()
    incident.ops.settings.allow_drift = True
    incident.ops.publish(target_commit=MAIN_SHA)
    expected_index = incident.index()

    seed_pages(incident.remote, incident.tmp_path, {"README.md": b"lost\n"}, "wipe")
    assert incident.versions() == []

    result = incident.ops.repair()
    assert result["consistent"] is True
    assert incident.versions() == [NEW, OLD]
    assert incident.files() == {
        "index.yaml",
        "README.md",
        f"ledger-api-{OLD}.tgz",
        f"ledger-api-{NEW}.tgz",
    }
    rebuilt = incident.index()
    assert {item["digest"] for item in rebuilt["entries"]["ledger-api"]} == {
        item["digest"] for item in expected_index["entries"]["ledger-api"]
    }


def test_repair_finishes_a_half_done_withdrawal(incident: Incident) -> None:
    release = incident.fake.release_by_tag(f"chart-v{BAD}")
    assert release is not None
    incident.fake.refs.pop(f"tags/chart-v{BAD}")  # tag already deleted by hand
    result = incident.ops.repair()
    assert result["consistent"] is True
    assert incident.versions() == [OLD]
    quarantined = incident.fake.release_by_tag(f"chart-v{BAD}")
    assert quarantined is not None and quarantined["draft"] is True
    assert quarantined["id"] == release["id"]


def test_repair_is_idempotent(incident: Incident) -> None:
    incident.ops.repair()
    tip = pages_tip(incident.remote)
    second = incident.ops.repair()
    assert second["consistent"] is True
    assert second["actions"] == []
    assert pages_tip(incident.remote) == tip


def test_repair_dry_run_changes_nothing(incident: Incident) -> None:
    incident.ops.settings.dry_run = True
    tip = pages_tip(incident.remote)
    incident.fake.calls.clear()
    result = incident.ops.repair()
    assert result["consistent"] is False
    assert result["actions"] == []
    assert incident.mutating_calls() == []
    assert pages_tip(incident.remote) == tip


def test_repair_removes_an_orphan_file_and_restores_a_missing_one(incident: Incident) -> None:
    files = {
        "index.yaml": _index_text(
            {
                OLD: (
                    sha256_bytes(incident.archives[f"ledger-api-{OLD}.tgz"]),
                    "2026-01-01T00:00:00Z",
                )
            }
        ).encode("utf-8"),
        "README.md": b"# Helm chart repository\n",
        "stray.tgz": b"orphan",
    }
    seed_pages(incident.remote, incident.tmp_path, files, "orphan")
    incident.ops.settings.allow_drift = True
    audit = incident.ops.audit()
    details = " ".join(finding["detail"] for finding in audit["findings"])
    assert "not referenced by index.yaml" in details
    assert "missing from index.yaml" in details
    incident.ops.repair()
    assert "stray.tgz" not in incident.files()
    assert f"ledger-api-{OLD}.tgz" in incident.files()


def test_audit_flags_a_digest_that_no_longer_matches(incident: Incident) -> None:
    files = {
        "index.yaml": _index_text({OLD: ("0" * 64, "2026-01-01T00:00:00Z")}).encode("utf-8"),
        "README.md": b"# Helm chart repository\n",
        f"ledger-api-{OLD}.tgz": incident.archives[f"ledger-api-{OLD}.tgz"],
    }
    seed_pages(incident.remote, incident.tmp_path, files, "digest")
    details = " ".join(f["detail"] for f in incident.ops.audit()["findings"])
    assert "digest does not match" in details


def test_audit_flags_an_index_entry_without_an_archive(incident: Incident) -> None:
    files = {
        "index.yaml": _index_text(
            {
                OLD: (
                    sha256_bytes(incident.archives[f"ledger-api-{OLD}.tgz"]),
                    "2026-01-01T00:00:00Z",
                )
            }
        ).encode("utf-8"),
        "README.md": b"# Helm chart repository\n",
    }
    seed_pages(incident.remote, incident.tmp_path, files, "missing-archive")
    details = " ".join(f["detail"] for f in incident.ops.audit()["findings"])
    assert "not on the branch" in details


def test_plan_repair_lists_only_repairable_findings(incident: Incident) -> None:
    plan = incident.ops.plan_repair().as_dict()
    assert plan["command"] == "repair"
    assert plan["force_update_required"] is True
    assert {step["action"] for step in plan["changes"]["github_release"]} == {"reconcile"}
    assert {step["action"] for step in plan["changes"]["github_tag"]} == {"reconcile"}


# ------------------------------------------------------------------ plumbing


def test_desired_pages_prefers_the_branch_copy_when_the_digest_matches(
    incident: Incident,
) -> None:
    incident.fake.calls.clear()
    index, archives = incident.ops.desired_pages()
    assert set(archives) == {f"ledger-api-{OLD}.tgz", f"ledger-api-{BAD}.tgz"}
    assert ("GET", f"/repos/owner/repo/releases/assets/{1002}") not in incident.fake.calls
    assert yaml.safe_load(dump_index(index))["entries"]["ledger-api"][0]["version"] == BAD


def test_read_pages_handles_an_absent_branch(
    work_root: Path, tmp_path: Path, fake_github: FakeGitHub
) -> None:
    from tests.conftest import git

    bare = tmp_path / "empty.git"
    git("init", "--bare", "--initial-branch", "gh-pages", str(bare), cwd=tmp_path)
    ops = make_operations(work_root=work_root, pages_remote=bare, fake=fake_github)
    try:
        state = ops.read_pages()
        assert state.tip is None
        assert state.files == {}
        assert state.index["entries"] == {}
    finally:
        ops.close()


def test_operations_without_credentials_refuse_remote_work(work_root: Path) -> None:
    settings = Settings(
        contract=PublicationContract.from_mapping(contract_mapping()),
        repo_root=work_root,
        state_dir=work_root / ".chartpub",
    )
    ops = Operations(settings)
    with pytest.raises(UsageError, match="GitHub credentials"):
        ops.snapshot()
    with pytest.raises(UsageError, match="Git remote"):
        ops.checkout_pages()
    ops.close()


def test_snapshot_reports_release_states(incident: Incident) -> None:
    incident.fake.add_release("chart-v9.9.9", draft=True)
    snapshot = incident.ops.snapshot()
    assert snapshot.main_tip == MAIN_SHA
    assert snapshot.bad_release_state == "published"
    assert snapshot.replacement_release_state is None
    assert set(snapshot.index_versions) == {OLD, BAD}
    assert f"ledger-api-{BAD}.tgz" in snapshot.pages_files


def test_prerelease_is_reported_but_not_published(incident: Incident) -> None:
    release = incident.fake.add_release("chart-v0.5.0", assets={"ledger-api-0.5.0.tgz": b"x"})
    release["prerelease"] = True
    published = incident.ops.published_versions(incident.ops.releases_by_tag())
    assert "0.5.0" in published  # a prerelease is still a public asset
    incident.fake.release_by_tag("chart-v0.5.0")["draft"] = True
    published = incident.ops.published_versions(incident.ops.releases_by_tag())
    assert "0.5.0" not in published


def test_unrelated_assets_are_ignored(incident: Incident) -> None:
    release = incident.fake.release_by_tag(f"chart-v{OLD}")
    assert release is not None
    incident.fake._attach(release, "checksums.txt", b"sums")
    incident.fake._attach(release, "other-chart-1.0.0.tgz", b"other")
    published = incident.ops.published_versions(incident.ops.releases_by_tag())
    assert sorted(published) == [OLD, BAD]


def test_artifact_round_trip(tmp_path: Path, chart_dir: Path) -> None:
    artifact = package_chart(chart_dir, tmp_path, NEW)
    assert isinstance(artifact, Artifact)
    assert artifact.size == artifact.path.stat().st_size
