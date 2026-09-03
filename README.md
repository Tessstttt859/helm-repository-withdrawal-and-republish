# Helm Repository Withdrawal and Republish

This repository holds the `ledger-api` Helm chart and `chartpub`, the operator
CLI that packages the chart, publishes it as a GitHub release plus a GitHub
Pages Helm repository, withdraws a bad version, and repairs a partial failure.

`publication-contract.json` is the only source of truth for which public objects
may be touched. Every destructive step is scoped to the versions, tags and
branch it names, and every remote mutation is guarded by a compare-and-swap
check against an expected tip.

## Contents

- [Publication state machine](#publication-state-machine)
- [Publication order](#publication-order)
- [Commands](#commands)
- [Dry-run examples](#dry-run-examples)
- [Recovery procedure](#recovery-procedure)
- [Exact destructive scope](#exact-destructive-scope)
- [Exit codes](#exit-codes)
- [Credential handling](#credential-handling)
- [Independent verification](#independent-verification)
- [Development](#development)
- [Known limitation](#known-limitation)

## Publication state machine

A chart version is in exactly one of these states, and the state is derived
from the remote, never from local files:

| State | GitHub release | Public tag | `index.yaml` entry | Archive on `gh-pages` |
| --- | --- | --- | --- | --- |
| `absent` | none | none | none | none |
| `staged` | published, asset uploaded | present | none | none |
| `published` | published, asset verified | present | present | present |
| `quarantined` | draft, assets retained | none | none | none |

Transitions:

```
absent ──publish(package+validate)──▶ staged ──publish(verify+pages)──▶ published
published ──withdraw──▶ quarantined
quarantined ──publish(new version)──▶ published (a new version; the old one stays quarantined)
any partial state ──repair──▶ published | quarantined
```

`repair` is the reconciler. It recomputes the intended public state from the
releases that are still public — a version is public exactly when it has a
non-draft release carrying its chart asset — and rewrites the Pages branch to
match. That is what makes withdrawal, publication and repair all idempotent.

## Publication order

The order exists so that a crash at any point leaves the public index either
untouched or correct, never advertising bytes nobody can install:

1. **Package** the chart deterministically (`chartpub` never shells out to
   `helm package`; see [reproducibility](#reproducibility)).
2. **Validate** the candidate: `helm lint --strict`, render every values
   fixture, render the defaults, verify archive members and digest, and install
   the packaged archive into an isolated test release. **A failure stops here**,
   before any remote call.
3. **Create the tag** at the verified source commit, then **create the release**
   and **upload the immutable asset**.
4. **Download the asset again** and compare its SHA-256 to the local digest. On
   mismatch the asset is deleted (rollback) and the run fails.
5. **Only now** rebuild the `gh-pages` snapshot and push it with
   `--force-with-lease` against the tip observed at the start.

Each completed step is recorded in `.chartpub/transaction.json` (non-secret:
phase, artifact name, digest, size, release id, asset id). Re-running `publish`
resumes from the recorded phase, so a partial attempt never uploads a second
asset or adds a second index entry.

## Commands

```
chartpub plan      [--remote] [--for publish|withdraw|repair]
chartpub publish   [--dry-run] [--target-commit SHA] [--allow-drift]
chartpub withdraw  [--dry-run] [--allow-drift]
chartpub audit     [--allow-drift]
chartpub repair    [--dry-run] [--allow-drift]
```

Shared flags: `--contract`, `--repo-root`, `--state-dir`, `--credentials`,
`--values` (repeatable).

Every command prints one JSON document on stdout. `plan` output separates
`changes.local`, `changes.github_release`, `changes.github_tag` and
`changes.pages`, and sets `force_update_required` when any step is destructive.

`--allow-drift` swaps the contracted expected tips for the tips actually
observed at the start of the run. It is required for the second half of a
recovery, because withdrawing moves `gh-pages` off the tip the contract pins.
It never disables compare-and-swap — the run still stops if the remote moves
after it was inspected.

## Dry-run examples

`--dry-run` performs no remote write of any kind. It still packages, validates
and reads remote state.

```bash
# Offline: the whole recovery, grouped by surface, with no network access.
chartpub plan --contract publication-contract.json
```

```bash
# Live read-only: what withdrawal would do against the current remote.
chartpub plan --remote --for withdraw \
  --contract publication-contract.json \
  --credentials ~/.config/agent-eval/github-helm-publish.env
```

```bash
# Validate the replacement candidate without publishing anything.
chartpub publish --dry-run --contract publication-contract.json
```

```bash
# Show the withdrawal plan and confirm nothing is written.
chartpub withdraw --dry-run \
  --contract publication-contract.json \
  --credentials ~/.config/agent-eval/github-helm-publish.env
```

## Recovery procedure

1. `chartpub audit --credentials <env>` — record the current state. Exit code 3
   means drift was found; the `findings` array says exactly what.
2. `chartpub withdraw --dry-run --credentials <env>` — review the plan.
3. `chartpub withdraw --credentials <env>` — quarantine the release, delete the
   public tag, rebuild `gh-pages` without that version.
4. `chartpub publish --allow-drift --credentials <env>` — package, validate,
   tag, release, upload, verify the download, then update `gh-pages`.
5. `chartpub audit --allow-drift --credentials <env>` — expect exit 0 and
   `"consistent": true`.

If any step is interrupted, re-run the same command. If the remote was changed
by something else in between, the command stops with exit code 4 and names the
ref and the two sha values; run `chartpub repair --dry-run` to see the
reconciliation plan, then `chartpub repair`.

## Exact destructive scope

`withdraw` touches **only** the objects named by the contract:

- the release whose `tag_name` equals `bad_tag` — it is **converted to a draft**
  and annotated. Its id, tag name, body history and assets are preserved, so it
  remains available as recovery evidence. It is never deleted.
- `refs/tags/<bad_tag>` — deleted, and only when it still points at
  `expected_bad_tag_target`.
- on `gh-pages`: the `<chart>-<bad_version>.tgz` file and its `index.yaml`
  entry.

Never touched by any command: other releases, other tags, other branches,
packages, repository visibility, collaborators, secrets, branch protections,
rulesets, the default branch, or any repository setting. `gh-pages` is only ever
updated with `--force-with-lease` against a tip confirmed immediately before the
push; there is no unguarded force push anywhere in the tool.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | success, or `audit` found no drift |
| 1 | unexpected internal error |
| 2 | usage or contract error, including a credential/origin target mismatch |
| 3 | validation failure, or `audit` found drift |
| 4 | remote compare-and-swap conflict — nothing was overwritten |
| 5 | a publication step failed |
| 6 | a rollback failed and an operator must clean up by hand |

## Credential handling

- Credentials are read at runtime from the `KEY=VALUE` file named by
  `--credentials`. They are never written to the repository, never placed in a
  URL, never stored in Git configuration, and never passed on a command line.
- Git authenticates through a temporary `GIT_ASKPASS` helper that reads the
  token from the child process environment; the helper is deleted when the
  command ends.
- `GITHUB_REPOSITORY` in the credential file, the `origin` URL and
  `publication-contract.json` must all agree. If they disagree, the command
  exits 2 before any remote call.
- All error text, subprocess output and API error bodies pass through a
  redactor that removes known secret values and anything token-shaped
  (`ghp_…`, `github_pat_…`) before it reaches stdout or stderr.

## Reproducibility

`package_chart` builds the archive itself so the digest depends only on the
chart content and the published version:

- members are emitted in sorted order (directories first, then files);
- `uid`/`gid` are 0, `uname`/`gname` empty, `mtime` 0, modes 0644/0755;
- the gzip header carries `mtime=0`;
- `Chart.yaml` is re-serialized with sorted keys and the published version;
- symlinks, non-regular files, absolute paths, `..` members and duplicate
  member names are rejected on both write and read.

`index.yaml` is equally deterministic: charts sorted by name, versions sorted
newest-first by semantic version, entry keys sorted, `created` preserved for an
unchanged version, and `generated` derived from the newest `created` rather than
from the clock. Re-running a completed publication rewrites a byte-identical
snapshot, so the commit is a no-op.

## Independent verification

These commands use nothing from this tool:

```bash
# The public index, straight from Pages.
curl -fsSL https://tessstttt859.github.io/helm-repository-withdrawal-and-republish/index.yaml
```

```bash
# A fresh Helm client discovers and renders the replacement.
helm repo add ledger-api-verify \
  https://tessstttt859.github.io/helm-repository-withdrawal-and-republish
helm repo update ledger-api-verify
helm search repo ledger-api-verify/ledger-api --versions
helm template check ledger-api-verify/ledger-api --version 0.4.1
```

```bash
# The advertised archive matches the advertised digest.
curl -fsSL https://tessstttt859.github.io/helm-repository-withdrawal-and-republish/ledger-api-0.4.1.tgz \
  | shasum -a 256
```

```bash
# The withdrawn tag is gone and its release is a draft.
gh api repos/Tessstttt859/helm-repository-withdrawal-and-republish/tags --jq '.[].name'
gh api repos/Tessstttt859/helm-repository-withdrawal-and-republish/releases \
  --jq '.[] | {tag_name, draft}'
```

## Development

Python 3.13 and Helm 3 are required.

```bash
python3.13 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
./scripts/verify-local.sh
```

CI runs the same checks on pull requests and on `main`: `ruff format --check`,
`ruff check`, strict `mypy`, `pytest` with a 90% coverage floor, `python -m
build`, `helm lint --strict`, every values fixture rendered, `chartpub publish
--dry-run`, and a separate job that installs the packaged archive against a real
API server in a throwaway `kind` cluster.

## Known limitation

`chartpub publish` runs the isolated-install check as
`helm install <release> <archive> --dry-run=client`. When no Kubernetes API
server is reachable — the usual case on a laptop — that command cannot run at
all, so the check falls back to rendering the same isolated release without the
API server and says so in its `detail` field. The structural rules that the API
server would enforce (workload selector must match its own pod template labels,
unique `kind/name`, `apiVersion` and `metadata.name` present) are applied
locally in every case; those rules are what the staging incident actually
violated. The full server-side install runs in CI, where the `smoke` job stands
up a `kind` cluster and uses `--dry-run=server`.
