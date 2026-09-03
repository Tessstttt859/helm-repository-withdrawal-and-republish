# Helm Repository Withdrawal and Republish

This repository contains the `ledger-api` Helm chart and `chartpub`, the small
operator CLI used to package the chart and maintain its GitHub Pages index.

The repository is intentionally pinned to the state captured immediately after
a failed staging publication. `publication-contract.json` identifies the
affected version, the intended replacement, and the public refs that operators
are allowed to touch.

## Development

Python 3.13 and Helm 3 are required.

```bash
python3.13 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
ruff check .
mypy src
pytest
helm lint charts/ledger-api
helm template ledger-api charts/ledger-api -f tests/fixtures/values-minimal.yaml
```

The CLI currently exposes `plan`, `publish`, and `audit`:

```bash
chartpub plan --contract publication-contract.json
chartpub publish --contract publication-contract.json --dry-run
chartpub audit --contract publication-contract.json
```

Live GitHub operations read credentials from the operator-provided environment
file named by the deployment procedure. Credentials must never be stored in
this repository or placed in a remote URL.

