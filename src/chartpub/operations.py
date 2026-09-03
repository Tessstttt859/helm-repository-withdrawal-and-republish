"""Lifecycle orchestration: plan, publish, withdraw, audit and repair."""

from __future__ import annotations

import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from chartpub import index as index_module
from chartpub.archive import (
    archive_metadata,
    inspect_archive,
    package_chart,
    sha256_bytes,
)
from chartpub.errors import (
    PublicationError,
    RemoteConflict,
    UsageError,
    ValidationError,
)
from chartpub.github import GitHubClient
from chartpub.models import Artifact, Plan, PlanStep, PublicationContract, RemoteSnapshot
from chartpub.pages import (
    Git,
    commit_snapshot,
    push_with_lease,
    remote_tip,
    snapshot_files,
    write_snapshot,
)
from chartpub.transaction import Journal, Publisher
from chartpub.validate import HelmRunner, ValidationReport, default_helm_runner, validate_candidate

QUARANTINE_NOTE = (
    "WITHDRAWN: this version failed installation validation and was removed from the "
    "public Helm index. This release is kept as a draft quarantine record for audit."
)
_ASSET_RE = re.compile(r"^(?P<chart>.+)-(?P<version>\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)*)\.tgz$")


@dataclass
class Settings:
    """Everything a lifecycle command needs, with no credential in sight."""

    contract: PublicationContract
    repo_root: Path
    state_dir: Path
    values_files: tuple[Path, ...] = ()
    dry_run: bool = True
    helm: HelmRunner = default_helm_runner
    allow_drift: bool = False


@dataclass
class PagesState:
    tip: str | None
    index: dict[str, Any]
    files: dict[str, bytes]


@dataclass
class Operations:
    """All lifecycle commands. Remote access is injected so tests stay offline."""

    settings: Settings
    client: GitHubClient | None = None
    git: Git | None = None
    remote_url: str = ""
    _workspaces: list[Path] = field(default_factory=list)

    # ------------------------------------------------------------------ util
    @property
    def contract(self) -> PublicationContract:
        return self.settings.contract

    def _require_client(self) -> GitHubClient:
        if self.client is None:
            raise UsageError("this command needs GitHub credentials (--credentials)")
        return self.client

    def _require_git(self) -> Git:
        if self.git is None:
            raise UsageError("this command needs a Git remote and credentials")
        return self.git

    def close(self) -> None:
        for path in self._workspaces:
            shutil.rmtree(path, ignore_errors=True)
        self._workspaces.clear()
        if self.git is not None:
            self.git.cleanup()

    # -------------------------------------------------------------- packaging
    def package(self, version: str, output_dir: Path | None = None) -> Artifact:
        chart_dir = self.settings.repo_root / self.contract.chart_dir
        target = output_dir or (self.settings.state_dir / "package")
        artifact = package_chart(chart_dir, target, version)
        expected = self.contract.asset_name(version)
        if artifact.path.name != expected:
            raise ValidationError(
                f"packaged {artifact.path.name} but the contract expects {expected}"
            )
        return artifact

    def validate(self, artifact: Artifact) -> ValidationReport:
        return validate_candidate(artifact, self.settings.values_files, helm=self.settings.helm)

    # ----------------------------------------------------------------- remote
    def releases_by_tag(self) -> dict[str, dict[str, Any]]:
        return {
            str(release.get("tag_name")): release
            for release in self._require_client().list_releases()
        }

    def published_versions(self, releases: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Versions the public index is supposed to advertise.

        A version counts as published when it has a non-draft release carrying
        the chart asset. Quarantining a release therefore removes it from the
        desired public state without deleting any history.
        """
        published: dict[str, dict[str, Any]] = {}
        for _tag, release in sorted(releases.items()):
            if release.get("draft"):
                continue
            for asset in release.get("assets", []):
                match = _ASSET_RE.match(str(asset.get("name", "")))
                if match and match.group("chart") == self.contract.chart:
                    published[match.group("version")] = {"release": release, "asset": asset}
        return published

    def snapshot(self) -> RemoteSnapshot:
        client = self._require_client()
        releases = self.releases_by_tag()
        bad = releases.get(self.contract.bad_tag)
        replacement = releases.get(self.contract.replacement_tag)
        pages = self.read_pages()
        return RemoteSnapshot(
            main_tip=client.get_ref(f"heads/{self.contract.source_branch}"),
            pages_tip=pages.tip,
            bad_tag_target=client.get_ref(f"tags/{self.contract.bad_tag}"),
            replacement_tag_target=client.get_ref(f"tags/{self.contract.replacement_tag}"),
            bad_release_state=_release_state(bad),
            bad_release_id=int(bad["id"]) if bad else None,
            replacement_release_state=_release_state(replacement),
            replacement_release_id=int(replacement["id"]) if replacement else None,
            index_versions=index_module.versions(pages.index, self.contract.chart),
            pages_files=tuple(sorted(pages.files)),
        )

    # ------------------------------------------------------------------ pages
    def _workspace(self) -> Path:
        path = Path(tempfile.mkdtemp(prefix="chartpub-pages-"))
        self._workspaces.append(path)
        return path

    def checkout_pages(self) -> tuple[Path, str | None]:
        git = self._require_git()
        branch = self.contract.pages_branch
        worktree = self._workspace()
        git.run("init", "--quiet", "--initial-branch", branch, str(worktree), cwd=worktree.parent)
        git.run("remote", "add", "origin", self.remote_url, cwd=worktree)
        tip = remote_tip(git, self.remote_url, branch, cwd=worktree)
        if tip is not None:
            git.run("fetch", "--quiet", "--depth", "1", "origin", tip, cwd=worktree)
            git.run("checkout", "--quiet", "-B", branch, tip, cwd=worktree)
        return worktree, tip

    def read_pages(self) -> PagesState:
        worktree, tip = self.checkout_pages()
        files: dict[str, bytes] = {}
        for path in sorted(worktree.rglob("*")):
            if ".git" in path.relative_to(worktree).parts or not path.is_file():
                continue
            files[path.relative_to(worktree).as_posix()] = path.read_bytes()
        index = index_module.load_index(worktree / "index.yaml")
        return PagesState(tip=tip, index=index, files=files)

    def desired_pages(
        self, *, extra_archives: dict[str, bytes] | None = None
    ) -> tuple[dict[str, Any], dict[str, bytes]]:
        """Rebuild the whole snapshot from the releases that are still public."""
        client = self._require_client()
        releases = self.releases_by_tag()
        published = self.published_versions(releases)
        current = self.read_pages()
        extras = dict(extra_archives or {})

        index: dict[str, Any] = index_module.empty_index()
        archives: dict[str, bytes] = {}
        for version in sorted(published):
            asset = published[version]["asset"]
            name = str(asset["name"])
            payload = extras.get(name)
            if payload is None:
                cached = current.files.get(name)
                # Trust the branch copy only while it still matches the index
                # digest; otherwise the immutable release asset is the truth.
                if cached is not None and sha256_bytes(cached) == _asset_digest(current, name):
                    payload = cached
                else:
                    payload = client.download_asset(int(asset["id"]))
            archives[name] = payload
            digest = sha256_bytes(payload)
            created = _existing_created(current.index, self.contract.chart, version)
            metadata = _metadata_from_bytes(payload, self.contract.chart)
            artifact = Artifact(Path(name), self.contract.chart, version, digest, len(payload))
            index_module.add_artifact(
                index,
                artifact,
                self.contract.pages_url,
                metadata=metadata,
                created=created or str(asset.get("created_at") or index_module.utc_now()),
                allow_replace=True,
            )
        return index_module.normalize(index), archives

    def write_pages_snapshot(
        self, index: dict[str, Any], archives: dict[str, bytes], *, expected_tip: str, message: str
    ) -> str | None:
        git = self._require_git()
        worktree, tip = self.checkout_pages()
        if tip != expected_tip:
            raise RemoteConflict(
                f"{self.contract.pages_branch} is at {tip or 'absent'} but {expected_tip} "
                "was expected; refusing to overwrite concurrent work"
            )
        files = snapshot_files(
            index_module.dump_index(index),
            archives,
            chart=self.contract.chart,
            url=self.contract.pages_url,
        )
        write_snapshot(worktree, files)
        commit = commit_snapshot(git, worktree, message)
        if commit is None:
            return None
        push_with_lease(git, worktree, self.remote_url, self.contract.pages_branch, expected_tip)
        return commit

    # ------------------------------------------------------------------ plans
    def plan(self, command: str) -> Plan:
        if command == "publish":
            return self.plan_publish()
        if command == "withdraw":
            return self.plan_withdraw()
        if command == "repair":
            return self.plan_repair()
        return self.plan_lifecycle()

    def _base_plan(self, command: str) -> Plan:
        contract = self.contract
        return Plan(
            command=command,
            repository=contract.repository,
            chart=contract.chart,
            bad_version=contract.bad_version,
            replacement_version=contract.replacement_version,
            dry_run=self.settings.dry_run,
        )

    def plan_lifecycle(self) -> Plan:
        """The offline plan: the full recovery, in the order it will run."""
        plan = self._base_plan("plan")
        contract = self.contract
        plan.preconditions = {
            "bad_tag": {
                "ref": f"refs/tags/{contract.bad_tag}",
                "expected": contract.expected_bad_tag_target,
            },
            "pages_branch": {
                "ref": f"refs/heads/{contract.pages_branch}",
                "expected": contract.expected_pages_tip,
            },
        }
        for step in self._withdraw_steps().values():
            plan.add(step)
        for step in self._publish_steps().values():
            plan.add(step)
        plan.notes.append(
            "Run `chartpub withdraw` then `chartpub publish`; both refuse to act if a "
            "precondition no longer matches."
        )
        return plan

    def _withdraw_steps(self) -> dict[str, PlanStep]:
        contract = self.contract
        steps = [
            PlanStep(
                "release",
                "quarantine",
                f"release {contract.bad_tag}",
                "convert the existing release to a draft, preserving its id and assets",
                destructive=True,
            ),
            PlanStep(
                "tag",
                "delete",
                f"refs/tags/{contract.bad_tag}",
                f"only if it still points at {contract.expected_bad_tag_target}",
                destructive=True,
            ),
            PlanStep(
                "pages",
                "remove",
                contract.asset_name(contract.bad_version),
                "drop the archive from the published snapshot",
                destructive=True,
            ),
            PlanStep(
                "pages",
                "reindex",
                "index.yaml",
                f"remove the {contract.chart} {contract.bad_version} entry",
                destructive=True,
            ),
        ]
        return {
            "quarantine-release": steps[0],
            "delete-tag": steps[1],
            "remove-archive": steps[2],
            "reindex": steps[3],
        }

    def _publish_steps(self) -> dict[str, PlanStep]:
        contract = self.contract
        version = contract.replacement_version
        steps = [
            PlanStep(
                "local", "package", contract.asset_name(version), "deterministic chart archive"
            ),
            PlanStep(
                "local",
                "validate",
                contract.asset_name(version),
                "helm lint, render every values fixture, verify archive, isolated install",
            ),
            PlanStep(
                "tag",
                "create",
                f"refs/tags/{contract.replacement_tag}",
                "at the verified source commit",
            ),
            PlanStep("release", "create", f"release {contract.replacement_tag}", "public release"),
            PlanStep(
                "release",
                "upload",
                contract.asset_name(version),
                "immutable asset, downloaded again and digest-checked",
            ),
            PlanStep("pages", "add", contract.asset_name(version), "only after the asset verifies"),
            PlanStep("pages", "reindex", "index.yaml", f"advertise {contract.chart} {version}"),
        ]
        return {
            "package": steps[0],
            "validate": steps[1],
            "create-tag": steps[2],
            "create-release": steps[3],
            "upload-asset": steps[4],
            "add-archive": steps[5],
            "reindex": steps[6],
        }

    def plan_withdraw(self) -> Plan:
        plan = self._base_plan("withdraw")
        snapshot = self.snapshot()
        plan.preconditions = snapshot.as_dict()
        contract = self.contract
        steps = self._withdraw_steps()
        expected_tag = contract.expected_bad_tag_target
        if snapshot.bad_release_id is not None and snapshot.bad_release_state != "draft":
            plan.add(steps["quarantine-release"])
        if snapshot.bad_tag_target is not None:
            if snapshot.bad_tag_target != expected_tag and not self.settings.allow_drift:
                plan.notes.append(
                    f"refs/tags/{contract.bad_tag} is at {snapshot.bad_tag_target}, "
                    f"not the contracted {expected_tag}"
                )
            plan.add(steps["delete-tag"])
        if contract.bad_version in snapshot.index_versions:
            plan.add(steps["remove-archive"])
            plan.add(steps["reindex"])
        return plan

    def plan_publish(self) -> Plan:
        plan = self._base_plan("publish")
        snapshot = self.snapshot()
        plan.preconditions = snapshot.as_dict()
        contract = self.contract
        version = contract.replacement_version
        steps = self._publish_steps()
        plan.add(steps["package"])
        plan.add(steps["validate"])
        if snapshot.replacement_tag_target is None:
            plan.add(steps["create-tag"])
        if snapshot.replacement_release_id is None:
            plan.add(steps["create-release"])
        plan.add(steps["upload-asset"])
        if version not in snapshot.index_versions:
            plan.add(steps["add-archive"])
            plan.add(steps["reindex"])
        return plan

    def plan_repair(self) -> Plan:
        plan = self._base_plan("repair")
        findings = self.audit()
        plan.preconditions = findings["remote"]
        for finding in findings["findings"]:
            if finding["repairable"]:
                plan.add(
                    PlanStep(
                        finding["scope"],
                        "reconcile",
                        finding["target"],
                        finding["detail"],
                        destructive=finding.get("destructive", False),
                    )
                )
        return plan

    # ------------------------------------------------------------------ audit
    def audit(self) -> dict[str, Any]:
        """Compare the live public state against what the contract implies."""
        contract = self.contract
        client = self._require_client()
        releases = self.releases_by_tag()
        published = self.published_versions(releases)
        pages = self.read_pages()
        advertised = set(index_module.versions(pages.index, contract.chart))
        findings: list[dict[str, Any]] = []

        def note(
            scope: str, target: str, detail: str, *, repairable: bool, destructive: bool = False
        ) -> None:
            findings.append(
                {
                    "scope": scope,
                    "target": target,
                    "detail": detail,
                    "repairable": repairable,
                    "destructive": destructive,
                }
            )

        bad_release = releases.get(contract.bad_tag)
        if bad_release is not None and not bad_release.get("draft"):
            note(
                "release",
                f"release {contract.bad_tag}",
                "the withdrawn version's release is still public",
                repairable=True,
                destructive=True,
            )
        if client.get_ref(f"tags/{contract.bad_tag}") is not None:
            note(
                "tag",
                f"refs/tags/{contract.bad_tag}",
                "the withdrawn version's public tag still exists",
                repairable=True,
                destructive=True,
            )
        for version in sorted(advertised - set(published)):
            note(
                "pages",
                f"{contract.chart} {version}",
                "advertised in index.yaml but not backed by a public release asset",
                repairable=True,
                destructive=True,
            )
        for version in sorted(set(published) - advertised):
            note(
                "pages",
                f"{contract.chart} {version}",
                "has a public release asset but is missing from index.yaml",
                repairable=True,
            )
        for entry in pages.index.get("entries", {}).get(contract.chart, []):
            name = str(entry.get("urls", [""])[0]).rsplit("/", 1)[-1]
            payload = pages.files.get(name)
            if payload is None:
                note(
                    "pages",
                    name,
                    "index.yaml references an archive that is not on the branch",
                    repairable=True,
                )
            elif sha256_bytes(payload) != entry.get("digest"):
                note(
                    "pages",
                    name,
                    "index.yaml digest does not match the published archive",
                    repairable=True,
                )
        referenced = set(index_module.referenced_files(pages.index)) | {"index.yaml", "README.md"}
        for name in sorted(set(pages.files) - referenced):
            note(
                "pages", name, "file on the branch is not referenced by index.yaml", repairable=True
            )

        remote = self.snapshot().as_dict()
        return {
            "command": "audit",
            "repository": contract.repository,
            "chart": contract.chart,
            "consistent": not findings,
            "published_versions": sorted(published),
            "advertised_versions": sorted(advertised),
            "findings": findings,
            "remote": remote,
        }

    # --------------------------------------------------------------- withdraw
    def withdraw(self) -> dict[str, Any]:
        contract = self.contract
        plan = self.plan_withdraw()
        result: dict[str, Any] = {
            "command": "withdraw",
            "repository": contract.repository,
            "chart": contract.chart,
            "version": contract.bad_version,
            "dry_run": self.settings.dry_run,
            "plan": plan.as_dict(),
            "withdrawn": {},
        }
        if self.settings.dry_run:
            return result
        if plan.is_noop:
            result["withdrawn"] = {"status": "already withdrawn"}
            return result

        client = self._require_client()
        snapshot = self.snapshot()
        expected_tag = (
            snapshot.bad_tag_target
            if self.settings.allow_drift
            else contract.expected_bad_tag_target
        )
        expected_pages = (
            snapshot.pages_tip if self.settings.allow_drift else contract.expected_pages_tip
        )
        # Check every precondition before the first mutation, so a drifted
        # remote stops the whole withdrawal instead of half of it.
        if snapshot.bad_tag_target is not None and snapshot.bad_tag_target != expected_tag:
            raise RemoteConflict(
                f"refs/tags/{contract.bad_tag} is at {snapshot.bad_tag_target} but "
                f"{expected_tag} was expected; nothing was changed"
            )
        if snapshot.pages_tip != expected_pages:
            raise RemoteConflict(
                f"{contract.pages_branch} is at {snapshot.pages_tip or 'absent'} but "
                f"{expected_pages} was expected; nothing was changed"
            )
        withdrawn: dict[str, Any] = {}

        # 1. Quarantine the release first; it stops being public immediately but
        #    the object, its id and its assets survive as recovery evidence.
        if snapshot.bad_release_id is not None and snapshot.bad_release_state != "draft":
            release = client.get_release(snapshot.bad_release_id)
            if release.get("tag_name") != contract.bad_tag:
                raise RemoteConflict(
                    f"release {snapshot.bad_release_id} is tagged {release.get('tag_name')!r}, "
                    f"not {contract.bad_tag!r}"
                )
            updated = client.quarantine_release(release, QUARANTINE_NOTE)
            withdrawn["release"] = {
                "id": int(updated["id"]),
                "tag": str(updated["tag_name"]),
                "draft": bool(updated["draft"]),
                "url": str(updated.get("html_url", "")),
            }

        # 2. Remove exactly the one public tag, guarded by its expected target.
        if snapshot.bad_tag_target is not None:
            client.delete_tag(contract.bad_tag, expected=expected_tag)
            withdrawn["tag"] = {
                "ref": f"refs/tags/{contract.bad_tag}",
                "was": snapshot.bad_tag_target,
            }

        # 3. Rebuild the published snapshot without that version.
        index, archives = self.desired_pages()
        commit = self.write_pages_snapshot(
            index,
            archives,
            expected_tip=str(expected_pages),
            message=f"withdraw: {contract.chart} {contract.bad_version}",
        )
        withdrawn["pages"] = {
            "branch": contract.pages_branch,
            "old_tip": snapshot.pages_tip,
            "new_tip": commit,
            "removed_file": contract.asset_name(contract.bad_version),
        }
        result["withdrawn"] = withdrawn
        return result

    # ---------------------------------------------------------------- publish
    def publish(self, *, target_commit: str | None = None) -> dict[str, Any]:
        contract = self.contract
        version = contract.replacement_version
        artifact = self.package(version)
        report = self.validate(artifact)
        result: dict[str, Any] = {
            "command": "publish",
            "repository": contract.repository,
            "chart": contract.chart,
            "version": version,
            "dry_run": self.settings.dry_run,
            "artifact": {
                "name": artifact.path.name,
                "sha256": artifact.sha256,
                "size": artifact.size,
            },
            "validation": report.as_dict(),
        }
        # A validation failure must never reach the public index.
        report.raise_for_status()
        if self.settings.dry_run:
            result["plan"] = self.plan_publish().as_dict() if self.client else None
            return result

        client = self._require_client()
        snapshot = self.snapshot()
        result["plan"] = self.plan_publish().as_dict()
        if (
            snapshot.replacement_tag_target not in (None, target_commit)
            and not self.settings.allow_drift
        ):
            raise RemoteConflict(
                f"refs/tags/{contract.replacement_tag} already exists at "
                f"{snapshot.replacement_tag_target}"
            )
        published: dict[str, Any] = {}
        state: dict[str, Any] = {}

        def write_release(candidate: Artifact) -> None:
            commit = target_commit or snapshot.main_tip
            if commit is None:
                raise PublicationError("cannot resolve the source commit to tag")
            if client.get_ref(f"tags/{contract.replacement_tag}") is None:
                client.create_ref(f"tags/{contract.replacement_tag}", commit)
            release = client.find_release(contract.replacement_tag)
            if release is None:
                release = client.create_release(
                    contract.replacement_tag,
                    target=commit,
                    name=f"{contract.chart} {candidate.version}",
                    body=(
                        f"Replacement for the withdrawn {contract.chart} "
                        f"{contract.bad_version}.\n\n"
                        f"`{candidate.path.name}` sha256: `{candidate.sha256}`"
                    ),
                )
            elif release.get("draft"):
                release = client.update_release(int(release["id"]), draft=False)
            release_id = int(release["id"])
            existing = {str(asset["name"]): asset for asset in client.list_assets(release_id)}
            asset = existing.get(candidate.path.name)
            if asset is not None and int(asset.get("size", -1)) != candidate.size:
                # A truncated upload from an earlier attempt: replace it rather
                # than adding a second asset with a mangled name.
                client.delete_asset(int(asset["id"]))
                asset = None
            if asset is None:
                asset = client.upload_asset(release_id, candidate.path.name, candidate.path)
            state["release_id"] = release_id
            state["asset_id"] = int(asset["id"])
            state["tag"] = contract.replacement_tag
            state["commit"] = commit
            published["release"] = {
                "id": release_id,
                "tag": contract.replacement_tag,
                "url": str(release.get("html_url", "")),
            }
            published["tag"] = {"ref": f"refs/tags/{contract.replacement_tag}", "sha": commit}

        def verify_release(candidate: Artifact) -> None:
            payload = client.download_asset(int(state["asset_id"]))
            digest = sha256_bytes(payload)
            if digest != candidate.sha256:
                raise ValidationError(
                    f"downloaded asset digest {digest} does not match {candidate.sha256}"
                )
            inspect_archive(candidate.path, expected_root=candidate.name)
            published["asset"] = {
                "id": int(state["asset_id"]),
                "name": candidate.path.name,
                "sha256": digest,
                "verified_bytes": len(payload),
            }

        def rollback_release(candidate: Artifact) -> None:
            asset_id = state.pop("asset_id", None)
            if asset_id is not None:
                client.delete_asset(int(asset_id))
                published.pop("asset", None)

        def write_pages(candidate: Artifact) -> None:
            expected_pages = (
                snapshot.pages_tip
                if self.settings.allow_drift
                else self._expected_pages_tip(snapshot)
            )
            index, archives = self.desired_pages(
                extra_archives={candidate.path.name: candidate.path.read_bytes()}
            )
            commit = self.write_pages_snapshot(
                index,
                archives,
                expected_tip=str(expected_pages),
                message=f"publish: {contract.chart} {candidate.version}",
            )
            published["pages"] = {
                "branch": contract.pages_branch,
                "old_tip": expected_pages,
                "new_tip": commit,
                "added_file": candidate.path.name,
            }

        publisher = Publisher(
            self.settings.state_dir,
            write_pages,
            write_release,
            verify_release=verify_release,
            rollback_release=rollback_release,
            context=state,
        )
        journal = publisher.publish(artifact)
        result["published"] = published
        result["journal"] = journal.data
        return result

    def _expected_pages_tip(self, snapshot: RemoteSnapshot) -> str:
        """After a withdrawal the branch has moved on; lease against what we saw."""
        return str(snapshot.pages_tip or self.contract.expected_pages_tip)

    # ----------------------------------------------------------------- repair
    def repair(self) -> dict[str, Any]:
        contract = self.contract
        before = self.audit()
        result: dict[str, Any] = {
            "command": "repair",
            "repository": contract.repository,
            "chart": contract.chart,
            "dry_run": self.settings.dry_run,
            "plan": self.plan_repair().as_dict(),
            "before": before["findings"],
            "actions": [],
        }
        if self.settings.dry_run or before["consistent"]:
            result["after"] = before["findings"]
            result["consistent"] = before["consistent"]
            return result

        client = self._require_client()
        actions: list[str] = []
        releases = self.releases_by_tag()
        bad_release = releases.get(contract.bad_tag)
        if bad_release is not None and not bad_release.get("draft"):
            client.quarantine_release(bad_release, QUARANTINE_NOTE)
            actions.append(f"quarantined release {contract.bad_tag}")
        bad_tag_target = client.get_ref(f"tags/{contract.bad_tag}")
        if bad_tag_target is not None:
            expected = (
                bad_tag_target if self.settings.allow_drift else contract.expected_bad_tag_target
            )
            client.delete_tag(contract.bad_tag, expected=expected)
            actions.append(f"deleted refs/tags/{contract.bad_tag}")

        pages = self.read_pages()
        index, archives = self.desired_pages()
        commit = self.write_pages_snapshot(
            index,
            archives,
            expected_tip=str(pages.tip),
            message=f"repair: reconcile {contract.chart} publication state",
        )
        if commit is not None:
            actions.append(f"rebuilt {contract.pages_branch} at {commit}")
        result["actions"] = actions
        after = self.audit()
        result["after"] = after["findings"]
        result["consistent"] = after["consistent"]
        return result

    # ------------------------------------------------------------------ state
    def journal(self) -> Journal:
        return Journal.load(self.settings.state_dir)


def _release_state(release: dict[str, Any] | None) -> str | None:
    if release is None:
        return None
    if release.get("draft"):
        return "draft"
    if release.get("prerelease"):
        return "prerelease"
    return "published"


def _existing_created(index: dict[str, Any], chart: str, version: str) -> str | None:
    for entry in index.get("entries", {}).get(chart, []):
        if entry.get("version") == version:
            created = entry.get("created")
            return str(created) if created else None
    return None


def _asset_digest(pages: PagesState, name: str) -> str | None:
    for entries in pages.index.get("entries", {}).values():
        for entry in entries:
            urls = entry.get("urls") or []
            if urls and str(urls[0]).rsplit("/", 1)[-1] == name:
                digest = entry.get("digest")
                return str(digest) if digest else None
    return None


def _metadata_from_bytes(payload: bytes, chart: str) -> dict[str, Any]:
    with tempfile.NamedTemporaryFile(suffix=".tgz", delete=False) as handle:
        handle.write(payload)
        path = Path(handle.name)
    try:
        inspect_archive(path, expected_root=chart)
        return archive_metadata(path, chart)
    finally:
        path.unlink(missing_ok=True)
