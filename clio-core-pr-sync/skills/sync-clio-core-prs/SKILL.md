---
name: sync-clio-core-prs
description: Update the user's open iowarp/clio-core PRs with the latest dev and report conflicts.
---

# Sync clio-core PRs with dev

1. Run the sync tool (add `--dry-run` if the request says dry run):

   ```sh
   python3 /mnt/common/hyoklee/prup/bin/sync_prs.py --json
   ```

   It prints one JSON object: `develop_sha` (the head of `dev`) plus a `prs` list where
   each entry has `number`, `title`, `head`, `behind_by`, and `status`, one of:
   - `up_to_date` — nothing to do
   - `updated` — dev was merged into the PR branch
   - `would_update` — dry run; branch is behind
   - `conflict` — GitHub refused the merge because of conflicts; the entry also has
     `conflicting_files` and a `detail`
   - `skipped` — draft or opted out via the `prup:skip` label
   - `error` — see `detail`

2. For `error` entries, re-run once; if it still fails, report the `detail`.

3. Report: one line per PR (`#number title — status`), then for each conflict PR
   its URL, the `detail`, and every path in `conflicting_files`, so the user can
   resolve it.
