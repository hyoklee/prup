---
name: sync-hdf5-prs
description: Update the user's open HDFGroup/hdf5 PRs with the latest develop and report conflicts.
---

# Sync HDF5 PRs with develop

1. Run the sync tool (add `--dry-run` if the request says dry run):

   ```sh
   python3 /mnt/common/hyoklee/prup/bin/sync_prs.py --json
   ```

   It prints one JSON object: `develop_sha` plus a `prs` list where each entry has
   `number`, `title`, `head`, `behind_by`, and `status`, one of:
   - `up_to_date` — nothing to do
   - `updated` — develop was merged into the PR branch
   - `would_update` — dry run; branch is behind
   - `resolved` — the only conflict was new entries added at the same spot in
     `release_docs/CHANGELOG.md`; develop was merged keeping both entries and pushed
   - `conflict` — GitHub refused the merge because of conflicts; the entry also has
     `conflicting_files` and a `detail` saying why it was not auto-resolved
   - `skipped` — draft or opted out via the `prup:skip` label
   - `error` — see `detail`

2. For `error` entries, re-run once; if it still fails, report the `detail`.

3. Report: one line per PR (`#number title — status`), then for each conflict PR
   its URL, the `detail`, and every path in `conflicting_files`, so the user can
   resolve it. Mention `resolved` PRs so the user knows a merge was pushed for them.
