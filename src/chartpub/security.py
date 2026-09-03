"""Credential handling: read at runtime, never persist, always redact."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from pathlib import Path

from chartpub.errors import ChartpubError, UsageError

SENSITIVE_NAMES = frozenset({"GITHUB_TOKEN", "GH_TOKEN", "GITHUB_PAT", "TOKEN", "PASSWORD"})
REDACTED = "[REDACTED]"
_TOKEN_SHAPES = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,})\b")
_ENTRY_RE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_MIN_REDACTABLE = 8


def read_env_file(path: Path) -> dict[str, str]:
    """Parse a KEY=VALUE credential file. The values never leave this process."""
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ChartpubError(f"cannot read credential file: {path}") from exc
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _ENTRY_RE.match(stripped)
        if match is None:
            key = stripped.partition("=")[0].strip()
            raise ChartpubError(f"invalid environment entry for {key}")
        key, value = match.group(1), match.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def require_token(values: Mapping[str, str]) -> str:
    token = values.get("GITHUB_TOKEN") or values.get("GH_TOKEN") or ""
    if not token:
        raise ChartpubError("GitHub token is missing")
    return token


def require_repository(values: Mapping[str, str], expected: str) -> str:
    """Refuse to act when the credential file targets a different repository."""
    configured = values.get("GITHUB_REPOSITORY", "").strip()
    if configured and configured.casefold() != expected.casefold():
        raise UsageError(
            f"credential file targets {configured!r} but the contract targets {expected!r}"
        )
    return expected


def secret_values(values: Mapping[str, str]) -> tuple[str, ...]:
    """Every value worth scrubbing from output, longest first."""
    found = {
        value
        for name, value in values.items()
        if value
        and len(value) >= _MIN_REDACTABLE
        and (name.upper() in SENSITIVE_NAMES or "TOKEN" in name.upper() or "SECRET" in name.upper())
    }
    return tuple(sorted(found, key=len, reverse=True))


def redact(message: str, secrets: Mapping[str, str] | Iterable[str] | None = None) -> str:
    """Remove known secrets and anything token-shaped from operator output."""
    result = message
    candidates: tuple[str, ...]
    if secrets is None:
        candidates = ()
    elif isinstance(secrets, Mapping):
        candidates = secret_values(secrets)
    else:
        candidates = tuple(sorted((s for s in secrets if s), key=len, reverse=True))
    for value in candidates:
        if value:
            result = result.replace(value, REDACTED)
    return _TOKEN_SHAPES.sub(REDACTED, result)
