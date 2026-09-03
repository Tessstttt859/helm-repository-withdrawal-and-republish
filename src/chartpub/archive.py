"""Reproducible chart packaging and safe archive inspection."""

from __future__ import annotations

import gzip
import hashlib
import io
import tarfile
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from chartpub.errors import ValidationError
from chartpub.models import Artifact

#: Every archive member is stamped with this timestamp so identical chart
#: sources always produce byte-identical archives.
FIXED_MTIME = 0
FILE_MODE = 0o644
DIR_MODE = 0o755
DEFAULT_EXCLUDES = (".git", ".gitignore", ".DS_Store", "*.tgz", "__pycache__", ".helmignore")
MAX_MEMBERS = 4096
MAX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class ArchiveMember:
    name: str
    size: int
    mode: int
    is_dir: bool


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_helmignore(chart_dir: Path) -> tuple[str, ...]:
    path = chart_dir / ".helmignore"
    if not path.is_file():
        return ()
    patterns: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            patterns.append(stripped.rstrip("/"))
    return tuple(patterns)


def _is_excluded(relative: PurePosixPath, patterns: tuple[str, ...]) -> bool:
    for pattern in (*DEFAULT_EXCLUDES, *patterns):
        if fnmatch(relative.name, pattern) or fnmatch(str(relative), pattern):
            return True
        if any(fnmatch(part, pattern) for part in relative.parts[:-1]):
            return True
    return False


def collect_files(chart_dir: Path) -> list[Path]:
    """Chart files to package, in a stable, filesystem-independent order."""
    if not chart_dir.is_dir():
        raise ValidationError(f"chart directory does not exist: {chart_dir}")
    patterns = _read_helmignore(chart_dir)
    selected: list[Path] = []
    for path in sorted(chart_dir.rglob("*"), key=lambda item: str(item.relative_to(chart_dir))):
        relative = PurePosixPath(path.relative_to(chart_dir).as_posix())
        if _is_excluded(relative, patterns):
            continue
        if path.is_symlink():
            raise ValidationError(f"chart contains a symlink, which is unsafe: {relative}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValidationError(f"chart contains a non-regular file: {relative}")
        selected.append(path)
    if not selected:
        raise ValidationError(f"chart directory is empty: {chart_dir}")
    return selected


def read_chart_metadata(chart_dir: Path) -> dict[str, Any]:
    path = chart_dir / "Chart.yaml"
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValidationError(f"cannot read {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ValidationError(f"Chart.yaml is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValidationError("Chart.yaml must be a mapping")
    for key in ("apiVersion", "name", "version"):
        if not isinstance(raw.get(key), str) or not raw[key].strip():
            raise ValidationError(f"Chart.yaml is missing a usable {key}")
    return raw


def normalized_metadata(chart_dir: Path, version: str) -> dict[str, Any]:
    """Chart metadata with the published version pinned, key order normalized."""
    metadata = read_chart_metadata(chart_dir)
    metadata["version"] = version
    return {key: metadata[key] for key in sorted(metadata)}


def _tarinfo(name: str, size: int, *, is_dir: bool = False) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = 0 if is_dir else size
    info.mtime = FIXED_MTIME
    info.mode = DIR_MODE if is_dir else FILE_MODE
    info.type = tarfile.DIRTYPE if is_dir else tarfile.REGTYPE
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    return info


def package_chart(chart_dir: Path, output_dir: Path, version: str) -> Artifact:
    """Package a chart directory into a deterministic Helm chart archive.

    Member order, ownership, modes, timestamps and the gzip header are all
    normalized, so the digest depends only on the chart's content and version.
    """
    metadata = normalized_metadata(chart_dir, version)
    name = str(metadata["name"])
    if name != chart_dir.name:
        raise ValidationError(
            f"Chart.yaml name {name!r} does not match directory {chart_dir.name!r}"
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{name}-{version}.tgz"

    chart_yaml = yaml.safe_dump(metadata, sort_keys=True, default_flow_style=False).encode("utf-8")
    payload: dict[str, bytes] = {f"{name}/Chart.yaml": chart_yaml}
    for path in collect_files(chart_dir):
        relative = path.relative_to(chart_dir).as_posix()
        if relative == "Chart.yaml":
            continue
        payload[f"{name}/{relative}"] = path.read_bytes()

    directories = sorted(
        {str(PurePosixPath(member).parent) for member in payload} | {name},
    )

    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for directory in directories:
            archive.addfile(_tarinfo(f"{directory}/", 0, is_dir=True))
        for member in sorted(payload):
            data = payload[member]
            archive.addfile(_tarinfo(member, len(data)), io.BytesIO(data))

    compressed = io.BytesIO()
    with gzip.GzipFile(fileobj=compressed, mode="wb", compresslevel=9, mtime=FIXED_MTIME) as gz:
        gz.write(raw.getvalue())
    output.write_bytes(compressed.getvalue())

    return Artifact(
        path=output,
        name=name,
        version=version,
        sha256=sha256_file(output),
        size=output.stat().st_size,
    )


def inspect_archive(path: Path, *, expected_root: str | None = None) -> list[ArchiveMember]:
    """List archive members, rejecting anything unsafe or duplicated."""
    members: list[ArchiveMember] = []
    seen: set[str] = set()
    total = 0
    try:
        with tarfile.open(path, "r:gz") as archive:
            for info in archive:
                if len(members) >= MAX_MEMBERS:
                    raise ValidationError(f"archive has more than {MAX_MEMBERS} members")
                name = info.name
                pure = PurePosixPath(name)
                if pure.is_absolute() or name.startswith("/") or name.startswith("\\"):
                    raise ValidationError(f"archive member has an absolute path: {name}")
                if ".." in pure.parts:
                    raise ValidationError(f"archive member escapes the chart root: {name}")
                if info.issym() or info.islnk():
                    raise ValidationError(f"archive member is a link, which is unsafe: {name}")
                if not (info.isfile() or info.isdir()):
                    raise ValidationError(f"archive member is not a regular file: {name}")
                key = name.rstrip("/")
                if key in seen:
                    raise ValidationError(f"archive contains a duplicate member: {key}")
                seen.add(key)
                if expected_root is not None and pure.parts[:1] != (expected_root,):
                    raise ValidationError(f"archive member is outside {expected_root}/: {name}")
                total += info.size
                if total > MAX_UNCOMPRESSED_BYTES:
                    raise ValidationError("archive expands beyond the supported size limit")
                members.append(
                    ArchiveMember(name=key, size=info.size, mode=info.mode, is_dir=info.isdir())
                )
    except tarfile.TarError as exc:
        raise ValidationError(f"cannot read chart archive {path.name}: {exc}") from exc
    if not members:
        raise ValidationError(f"chart archive is empty: {path.name}")
    return members


def read_archive_member(path: Path, member: str) -> bytes:
    with tarfile.open(path, "r:gz") as archive:
        try:
            extracted = archive.extractfile(member)
        except KeyError as exc:
            raise ValidationError(f"archive is missing {member}") from exc
        if extracted is None:
            raise ValidationError(f"archive is missing {member}")
        with extracted:
            return extracted.read()


def archive_metadata(path: Path, chart: str) -> dict[str, Any]:
    raw = yaml.safe_load(read_archive_member(path, f"{chart}/Chart.yaml").decode("utf-8"))
    if not isinstance(raw, dict):
        raise ValidationError("archived Chart.yaml must be a mapping")
    return raw


def verify_archive(path: Path, expected_sha256: str) -> bool:
    return sha256_file(path) == expected_sha256


def require_digest(path: Path, expected_sha256: str) -> None:
    actual = sha256_file(path)
    if actual != expected_sha256:
        raise ValidationError(
            f"digest mismatch for {path.name}: expected {expected_sha256}, got {actual}"
        )
