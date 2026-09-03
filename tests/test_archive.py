from __future__ import annotations

import os
from pathlib import Path

from chartpub.archive import package_chart, verify_archive


def test_package_digest_verifies(tmp_path: Path, chart_dir: Path) -> None:
    artifact = package_chart(chart_dir, tmp_path, "0.4.1")
    assert verify_archive(artifact.path, artifact.sha256)


def test_package_is_reproducible(tmp_path: Path, chart_dir: Path) -> None:
    first = package_chart(chart_dir, tmp_path / "first", "0.4.1")
    chart_yaml = chart_dir / "Chart.yaml"
    stat = chart_yaml.stat()
    os.utime(chart_yaml, (stat.st_atime, stat.st_mtime + 5))
    try:
        second = package_chart(chart_dir, tmp_path / "second", "0.4.1")
    finally:
        os.utime(chart_yaml, (stat.st_atime, stat.st_mtime))
    assert first.sha256 == second.sha256
