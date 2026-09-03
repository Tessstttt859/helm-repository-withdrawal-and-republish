"""Loading and strict validation of the publication contract."""

from __future__ import annotations

import json
from pathlib import Path

from chartpub.errors import ContractError
from chartpub.models import PublicationContract


def load_contract(path: Path) -> PublicationContract:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot load publication contract: {exc}") from exc
    if not isinstance(raw, dict):
        raise ContractError("publication contract must be a JSON object")
    try:
        return PublicationContract.from_mapping(raw)
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ContractError):
            raise
        raise ContractError(f"malformed publication contract: {exc}") from exc
