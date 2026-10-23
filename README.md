# prup — keep my iowarp/clio-core PRs synced with dev

A CLIO Agent Blueprint (`clio-core-pr-sync/`) plus two small stdlib-only scripts.

| Path | Role |
|---|---|
| `clio-core-pr-sync/` | CLIO blueprint: `AGENT.md`, `experts/root.md` (react expert with `shell_bash`), `skills/sync-clio-core-prs/SKILL.md` |
| `bin/sync_prs.py` | Finds open PRs by `hyoklee` against `dev` and calls GitHub's "Update branch" API (merges dev into the PR head) when a PR is behind. `--dry-run`, `--json`, `--conflicts N` |
| `bin/watch_develop.py` | Cron entry point. Reads the dev head with `git ls-remote`; if it changed, starts a CLIO session on the blueprint and asks it to sync. Falls back to `sync_prs.py` directly if CLIO is unusable. Records the SHA in `.state/` only on success. |

## Behavior

- PRs that are already up to date are left alone; PRs with the `prup:skip` label and draft
  PRs are skipped (`--include-drafts` to change that).
- The update is a merge commit on your PR branch (same as the "Update branch" button), and
  uses `expected_head_sha` so it never clobbers a push that just landed.
- On a conflict, a local trial merge in `.work/clio-core` lists the conflicting files
  and the conflict is reported, never resolved.
- Optional, off by default: if `PRUP_CHANGELOG` names a changelog file (e.g.
  `release_docs/CHANGELOG.md` for `HDFGroup/hdf5`) and the only conflict is new `### `
  entries added at the same spot in it, both entries are kept (PR's first) and the merge
  is pushed without force, committed with your git identity (status `resolved`). It is
  verified first: the PR head must not have moved, and the result must differ from the
  base only by the PR's own changes. `--no-resolve` turns this off.
- Each update pushes to the PR, which re-triggers CI on that PR.

## Configuration (env)

| Variable | Default |
|---|---|
| `PRUP_AUTHOR` / `PRUP_REPO` / `PRUP_BASE` | `hyoklee` / `iowarp/clio-core` / `dev` |
| `PRUP_CHANGELOG` | unset (no auto-resolve); `release_docs/CHANGELOG.md` restores the HDF5 behavior |
| `GITHUB_TOKEN` | falls back to `git credential fill` for github.com |
| `PRUP_CLIO_MODEL` | `argonne_sophia/mistralai/Devstral-2-123B-Instruct-2512` (same as clio-coder) |
| `CLIO_ARGONNE_TOKEN` | else read from clio-coder's `~/.config/clio-coder/credentials.yaml` (read-only; refreshed in memory) |
| `PRUP_CLIO_SERVE` | `uv run --project ~/iowarp/clio-agent clio-agent serve --port $CLIO_PORT` |
| `CLIO_PORT` / `CLIO_BEARER_TOKEN` | `17800` / unset |

## Usage

```sh
python3 bin/sync_prs.py --dry-run              # what would be updated
python3 bin/watch_develop.py --force --dry-run # full CLIO path, no changes
python3 bin/watch_develop.py --runner direct   # sync without CLIO
```

Cron (hourly):

```cron
0 * * * * PATH=$HOME/mc3/bin:/usr/bin:/bin; cd /mnt/common/hyoklee/prup && /usr/bin/python3 bin/watch_develop.py >> .state/watch.log 2>&1
```

## ALCF auth

The watcher reuses clio-coder's ALCF login. If it is missing or its refresh token has
expired, log in again with clio-coder (or export `CLIO_ARGONNE_TOKEN`).
