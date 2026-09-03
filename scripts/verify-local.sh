#!/usr/bin/env bash
# Everything CI runs, in the same order, against the working tree.
set -euo pipefail

ruff format --check .
ruff check .
mypy src
pytest --cov=chartpub --cov-report=term-missing --cov-fail-under=90
python -m build
helm lint --strict charts/ledger-api
helm template ledger-api charts/ledger-api -f tests/fixtures/values-minimal.yaml >/dev/null
helm template ledger-api charts/ledger-api -f tests/fixtures/values-ha.yaml >/dev/null
chartpub publish --dry-run --contract publication-contract.json >/dev/null
echo "local verification passed"
