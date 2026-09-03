from __future__ import annotations

import json
from pathlib import Path

from chartpub.cli import build_parser, main

from .test_config import valid_contract, write_contract


def test_plan_is_machine_readable(tmp_path: Path, capsys: object) -> None:
    path = tmp_path / "contract.json"
    write_contract(path, valid_contract())
    assert main(["plan", "--contract", str(path)]) == 0
    output = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert output["command"] == "plan"


def test_all_lifecycle_commands_exist() -> None:
    parser = build_parser()
    help_text = parser.format_help()
    assert "withdraw" in help_text
    assert "repair" in help_text
