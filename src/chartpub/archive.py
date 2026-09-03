from __future__ import annotations

import hashlib
import tarfile
from pathlib import Path

from chartpub.models import Artifact


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def package_chart(chart_dir: Path, output_dir: Path, version: str) -> Artifact:
    output_dir.mkdir(parents=True, exist_ok=True)
    name = chart_dir.name
    output = output_dir / f"{name}-{version}.tgz"
    # tarfile.add retains local ownership, modes, and mtimes. It also follows
    # the filesystem ordering, which is not a reproducible publication format.
    with tarfile.open(output, "w:gz") as archive:
        archive.add(chart_dir, arcname=name)
    return Artifact(
        path=output,
        name=name,
        version=version,
        sha256=sha256_file(output),
        size=output.stat().st_size,
    )


def verify_archive(path: Path, expected_sha256: str) -> bool:
    return sha256_file(path) == expected_sha256
