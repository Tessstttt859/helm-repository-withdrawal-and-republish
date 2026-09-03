"""Resumable publication transaction: release first, pages last."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from chartpub.errors import RollbackError
from chartpub.models import Artifact

PageWriter = Callable[[Artifact], None]
ReleaseWriter = Callable[[Artifact], None]
ReleaseVerifier = Callable[[Artifact], None]
Rollback = Callable[[Artifact], None]

#: Ordered phases. The pages snapshot, which is what makes a chart publicly
#: discoverable, is only touched after the immutable asset is verified.
PHASES = ("started", "release-uploaded", "release-verified", "pages-updated", "complete")
STATE_FILE = "transaction.json"


@dataclass
class Journal:
    """Non-secret resume state written next to the working directory."""

    path: Path
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, state_dir: Path) -> Journal:
        path = state_dir / STATE_FILE
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                data = {}
            if isinstance(data, dict):
                return cls(path, data)
        return cls(path, {})

    @property
    def phase(self) -> str:
        phase = str(self.data.get("phase", ""))
        return phase if phase in PHASES else ""

    def matches(self, artifact: Artifact) -> bool:
        return (
            self.data.get("artifact") == artifact.path.name
            and self.data.get("sha256") == artifact.sha256
        )

    def reached(self, phase: str, artifact: Artifact) -> bool:
        """True when a previous attempt already completed `phase` for this input."""
        if not self.phase or not self.matches(artifact):
            return False
        return PHASES.index(self.phase) >= PHASES.index(phase)

    def record(self, phase: str, artifact: Artifact, **extra: Any) -> None:
        if phase not in PHASES:
            raise ValueError(f"unknown transaction phase: {phase}")
        self.data.update(
            {
                "phase": phase,
                "artifact": artifact.path.name,
                "chart": artifact.name,
                "version": artifact.version,
                "sha256": artifact.sha256,
                "size": artifact.size,
                **extra,
            }
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


class Publisher:
    """Drives the publication order and records enough state to resume."""

    def __init__(
        self,
        state_dir: Path,
        write_pages: PageWriter,
        write_release: ReleaseWriter,
        verify_release: ReleaseVerifier | None = None,
        rollback_release: Rollback | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        self.state_dir = state_dir
        self.write_pages = write_pages
        self.write_release = write_release
        self.verify_release = verify_release
        self.rollback_release = rollback_release
        #: Non-secret ids (release, asset) a resumed attempt needs to continue.
        self.context = context if context is not None else {}

    def publish(self, artifact: Artifact) -> Journal:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        journal = Journal.load(self.state_dir)
        if not journal.matches(artifact):
            journal.data.clear()
        else:
            self.context.update(journal.data.get("context") or {})
        if journal.phase == "complete":
            return journal
        if not journal.phase:
            journal.record("started", artifact, context=self.context)

        # 1. Immutable release asset first: a failure here is invisible to users.
        if not journal.reached("release-uploaded", artifact):
            self.write_release(artifact)
            journal.record("release-uploaded", artifact, context=self.context)

        # 2. Prove the uploaded bytes round-trip before advertising them.
        if not journal.reached("release-verified", artifact):
            try:
                if self.verify_release is not None:
                    self.verify_release(artifact)
            except Exception as exc:
                self._rollback(artifact, exc)
                journal.record("started", artifact, context=self.context)
                raise
            journal.record("release-verified", artifact, context=self.context)

        # 3. Only now does the chart become publicly discoverable.
        if not journal.reached("pages-updated", artifact):
            self.write_pages(artifact)
            journal.record("pages-updated", artifact, context=self.context)

        journal.record("complete", artifact, context=self.context)
        return journal

    def _rollback(self, artifact: Artifact, cause: Exception) -> None:
        if self.rollback_release is None:
            return
        try:
            self.rollback_release(artifact)
        except Exception as exc:
            raise RollbackError(
                f"rollback failed after {cause}: {exc}; the release asset for "
                f"{artifact.name} {artifact.version} must be removed by hand"
            ) from exc
