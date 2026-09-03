from __future__ import annotations

import gzip
import io
import os
import tarfile
from pathlib import Path

import pytest

from chartpub.archive import (
    archive_metadata,
    collect_files,
    inspect_archive,
    normalized_metadata,
    package_chart,
    require_digest,
    sha256_bytes,
    verify_archive,
)
from chartpub.errors import ValidationError


def _tar(members: list[tuple[tarfile.TarInfo, bytes]], path: Path) -> Path:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as archive:
        for info, payload in members:
            archive.addfile(info, io.BytesIO(payload))
    path.write_bytes(gzip.compress(raw.getvalue(), mtime=0))
    return path


def _member(name: str, payload: bytes = b"x", kind: bytes = tarfile.REGTYPE) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    info.type = kind
    return info


def test_package_digest_verifies(tmp_path: Path, chart_dir: Path) -> None:
    artifact = package_chart(chart_dir, tmp_path, "0.4.1")
    assert verify_archive(artifact.path, artifact.sha256)
    require_digest(artifact.path, artifact.sha256)


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


def test_package_normalizes_ownership_and_times(tmp_path: Path, chart_dir: Path) -> None:
    artifact = package_chart(chart_dir, tmp_path, "0.4.1")
    with tarfile.open(artifact.path, "r:gz") as archive:
        infos = list(archive)
    directories = [info.name for info in infos if info.isdir()]
    files = [info.name for info in infos if info.isfile()]
    assert directories == sorted(directories)
    assert files == sorted(files)
    assert all(info.uid == 0 and info.gid == 0 and info.mtime == 0 for info in infos)
    assert all(info.uname == "" and info.gname == "" for info in infos)


def test_package_pins_the_published_version(tmp_path: Path, chart_dir: Path) -> None:
    artifact = package_chart(chart_dir, tmp_path, "9.9.9")
    assert archive_metadata(artifact.path, "ledger-api")["version"] == "9.9.9"
    assert artifact.path.name == "ledger-api-9.9.9.tgz"


def test_package_rejects_name_mismatch(tmp_path: Path, chart_dir: Path) -> None:
    renamed = tmp_path / "other-name"
    renamed.mkdir()
    for item in chart_dir.rglob("*"):
        target = renamed / item.relative_to(chart_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        if item.is_file():
            target.write_bytes(item.read_bytes())
    with pytest.raises(ValidationError, match="does not match directory"):
        package_chart(renamed, tmp_path / "out", "0.4.1")


def test_collect_files_rejects_symlinks(tmp_path: Path, chart_dir: Path) -> None:
    staged = tmp_path / "ledger-api"
    staged.mkdir()
    (staged / "Chart.yaml").write_bytes((chart_dir / "Chart.yaml").read_bytes())
    (staged / "escape.yaml").symlink_to("/etc/hosts")
    with pytest.raises(ValidationError, match="symlink"):
        collect_files(staged)


def test_collect_files_honours_helmignore(tmp_path: Path, chart_dir: Path) -> None:
    staged = tmp_path / "ledger-api"
    staged.mkdir()
    (staged / "Chart.yaml").write_bytes((chart_dir / "Chart.yaml").read_bytes())
    (staged / "notes.txt").write_text("scratch", encoding="utf-8")
    (staged / ".helmignore").write_text("notes.txt\n# comment\n", encoding="utf-8")
    names = {path.name for path in collect_files(staged)}
    assert names == {"Chart.yaml"}


def test_collect_files_rejects_empty_directory(tmp_path: Path) -> None:
    empty = tmp_path / "ledger-api"
    empty.mkdir()
    with pytest.raises(ValidationError, match="empty"):
        collect_files(empty)


def test_collect_files_rejects_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="does not exist"):
        collect_files(tmp_path / "absent")


def test_metadata_requires_usable_fields(tmp_path: Path) -> None:
    staged = tmp_path / "ledger-api"
    staged.mkdir()
    (staged / "Chart.yaml").write_text("name: ledger-api\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="apiVersion"):
        normalized_metadata(staged, "0.4.1")


def test_metadata_rejects_non_mapping(tmp_path: Path) -> None:
    staged = tmp_path / "ledger-api"
    staged.mkdir()
    (staged / "Chart.yaml").write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="mapping"):
        normalized_metadata(staged, "0.4.1")


@pytest.mark.parametrize(
    ("name", "match"),
    [
        ("/etc/passwd", "absolute path"),
        ("ledger-api/../../escape.yaml", "escapes"),
    ],
)
def test_inspect_rejects_unsafe_paths(tmp_path: Path, name: str, match: str) -> None:
    path = _tar([(_member(name), b"x")], tmp_path / "bad.tgz")
    with pytest.raises(ValidationError, match=match):
        inspect_archive(path)


def test_inspect_rejects_links(tmp_path: Path) -> None:
    info = _member("ledger-api/link", b"", tarfile.SYMTYPE)
    info.linkname = "/etc/hosts"
    path = _tar([(info, b"")], tmp_path / "link.tgz")
    with pytest.raises(ValidationError, match="link"):
        inspect_archive(path)


def test_inspect_rejects_non_regular_members(tmp_path: Path) -> None:
    path = _tar([(_member("ledger-api/dev", b"", tarfile.CHRTYPE), b"")], tmp_path / "dev.tgz")
    with pytest.raises(ValidationError, match="regular file"):
        inspect_archive(path)


def test_inspect_rejects_duplicates(tmp_path: Path) -> None:
    path = _tar(
        [(_member("ledger-api/Chart.yaml"), b"x"), (_member("ledger-api/Chart.yaml"), b"y")],
        tmp_path / "dup.tgz",
    )
    with pytest.raises(ValidationError, match="duplicate"):
        inspect_archive(path)


def test_inspect_rejects_foreign_root(tmp_path: Path, chart_dir: Path) -> None:
    artifact = package_chart(chart_dir, tmp_path, "0.4.1")
    with pytest.raises(ValidationError, match="outside"):
        inspect_archive(artifact.path, expected_root="other")


def test_inspect_rejects_unreadable_archive(tmp_path: Path) -> None:
    path = tmp_path / "junk.tgz"
    path.write_bytes(b"not a tarball at all")
    with pytest.raises(ValidationError, match="cannot read"):
        inspect_archive(path)


def test_inspect_rejects_empty_archive(tmp_path: Path) -> None:
    path = _tar([], tmp_path / "empty.tgz")
    with pytest.raises(ValidationError, match="empty"):
        inspect_archive(path)


def test_require_digest_reports_mismatch(tmp_path: Path, chart_dir: Path) -> None:
    artifact = package_chart(chart_dir, tmp_path, "0.4.1")
    with pytest.raises(ValidationError, match="digest mismatch"):
        require_digest(artifact.path, "0" * 64)


def test_archive_metadata_requires_chart_yaml(tmp_path: Path) -> None:
    path = _tar([(_member("ledger-api/values.yaml"), b"a: 1")], tmp_path / "no-chart.tgz")
    with pytest.raises(ValidationError, match="missing"):
        archive_metadata(path, "ledger-api")


def test_sha256_bytes_matches_file(tmp_path: Path, chart_dir: Path) -> None:
    artifact = package_chart(chart_dir, tmp_path, "0.4.1")
    assert sha256_bytes(artifact.path.read_bytes()) == artifact.sha256
