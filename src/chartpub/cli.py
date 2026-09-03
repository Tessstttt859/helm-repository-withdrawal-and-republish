from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from chartpub.config import load_contract
from chartpub.errors import ChartpubError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chartpub")
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "publish", "audit"):
        command = subcommands.add_parser(name)
        command.add_argument("--contract", type=Path, default=Path("publication-contract.json"))
        if name == "publish":
            command.add_argument("--dry-run", action="store_true")
    return parser


def _summary(contract_path: Path, command: str) -> dict[str, object]:
    contract = load_contract(contract_path)
    return {
        "command": command,
        "repository": contract.repository,
        "chart": contract.chart,
        "bad_version": contract.bad_version,
        "replacement_version": contract.replacement_version,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = _summary(args.contract, args.command)
        if args.command == "publish" and not args.dry_run:
            raise ChartpubError("live publication is not implemented")
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except ChartpubError as exc:
        print(f"chartpub: {exc}", file=sys.stderr)
        return 2

