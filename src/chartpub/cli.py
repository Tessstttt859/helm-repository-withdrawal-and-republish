"""chartpub command line: plan, publish, withdraw, audit, repair."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from chartpub.config import load_contract
from chartpub.errors import EXIT_INTERNAL, EXIT_VALIDATION, ChartpubError
from chartpub.github import GitHubClient
from chartpub.models import PublicationContract
from chartpub.operations import Operations, Settings
from chartpub.pages import Git
from chartpub.remote import head_commit, resolve_target
from chartpub.security import (
    read_env_file,
    redact,
    require_repository,
    require_token,
    secret_values,
)

COMMANDS = ("plan", "publish", "withdraw", "audit", "repair")
DEFAULT_VALUES = (
    Path("tests/fixtures/values-minimal.yaml"),
    Path("tests/fixtures/values-ha.yaml"),
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chartpub",
        description=(
            "Recoverable publication lifecycle for a GitHub Pages Helm repository. "
            "Every command is deterministic and refuses to act when a remote "
            "precondition no longer matches."
        ),
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    helptext = {
        "plan": "show the deterministic change plan without touching anything",
        "publish": "validate and publish the replacement version",
        "withdraw": "quarantine and unpublish exactly the bad version",
        "audit": "compare the live public state against the contract",
        "repair": "reconcile a partial or drifted publication state",
    }
    for name in COMMANDS:
        command = subcommands.add_parser(name, help=helptext[name], description=helptext[name])
        command.add_argument("--contract", type=Path, default=Path("publication-contract.json"))
        command.add_argument(
            "--repo-root", type=Path, default=Path("."), help="working tree holding the chart"
        )
        command.add_argument(
            "--state-dir",
            type=Path,
            default=Path(".chartpub"),
            help="non-secret resume state and packaging output",
        )
        command.add_argument(
            "--credentials",
            type=Path,
            default=None,
            help="KEY=VALUE file holding GITHUB_TOKEN; read at runtime, never stored",
        )
        command.add_argument(
            "--values",
            type=Path,
            action="append",
            default=None,
            help="values fixture to render during validation (repeatable)",
        )
        command.add_argument(
            "--allow-drift",
            action="store_true",
            help="use the observed remote tips instead of the contracted ones",
        )
        if name in ("publish", "withdraw", "repair"):
            command.add_argument(
                "--dry-run", action="store_true", help="perform no remote write at all"
            )
        if name == "publish":
            command.add_argument(
                "--target-commit",
                default=None,
                help="source commit to tag (default: the remote source branch tip)",
            )
        if name == "plan":
            command.add_argument(
                "--remote",
                action="store_true",
                help="read live remote state instead of planning offline",
            )
            command.add_argument(
                "--for",
                dest="for_command",
                choices=("publish", "withdraw", "repair"),
                default=None,
                help="plan a single command instead of the whole recovery",
            )
    return parser


def _values_files(args: argparse.Namespace, repo_root: Path) -> tuple[Path, ...]:
    chosen = args.values if args.values else [repo_root / value for value in DEFAULT_VALUES]
    return tuple(path for path in chosen if path.is_file())


def _attach_remote(
    operations: Operations, contract: PublicationContract, credentials: Path | None
) -> tuple[str, ...]:
    """Wire live GitHub and Git access, refusing a mismatched target."""
    if credentials is None:
        return ()
    values = read_env_file(credentials)
    token = require_token(values)
    require_repository(values, contract.repository)
    url, repository = resolve_target(operations.settings.repo_root, contract.repository)
    secrets = secret_values(values)
    operations.client = GitHubClient(repository=repository, token=token, secrets=secrets)
    operations.git = Git(operations.settings.repo_root, token=token, secrets=secrets)
    operations.remote_url = url
    return secrets


def _run(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    contract = load_contract(args.contract)
    repo_root = args.repo_root.resolve()
    settings = Settings(
        contract=contract,
        repo_root=repo_root,
        state_dir=(repo_root / args.state_dir).resolve(),
        values_files=_values_files(args, repo_root),
        dry_run=bool(getattr(args, "dry_run", True)),
        allow_drift=bool(args.allow_drift),
    )
    operations = Operations(settings)
    needs_remote = args.command in ("publish", "withdraw", "audit", "repair") or getattr(
        args, "remote", False
    )
    try:
        if needs_remote:
            _attach_remote(operations, contract, args.credentials)
        if args.command == "plan":
            plan = (
                operations.plan(args.for_command)
                if args.for_command and operations.client
                else operations.plan_lifecycle()
            )
            return 0, plan.as_dict()
        if args.command == "audit":
            payload = operations.audit()
            return (0 if payload["consistent"] else EXIT_VALIDATION), payload
        if args.command == "withdraw":
            return 0, operations.withdraw()
        if args.command == "repair":
            return 0, operations.repair()
        target = args.target_commit
        if target is None and not settings.dry_run:
            target = head_commit(repo_root)
        return 0, operations.publish(target_commit=target)
    finally:
        operations.close()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        code, payload = _run(args)
    except ChartpubError as exc:
        print(f"chartpub: {redact(str(exc))}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:  # pragma: no cover - operator interrupt
        print("chartpub: interrupted", file=sys.stderr)
        return EXIT_INTERNAL
    print(json.dumps(payload, indent=2, sort_keys=True))
    return code
