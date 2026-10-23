---
id: clio-core-pr-sync
version: 0.2.0
title: clio-core PR Sync Agent
description: Keeps the user's open iowarp/clio-core pull requests in sync with the dev branch.
root_expert: root
defaults:
  prompt_profile: heavy
requires:
  tools:
    - shell_bash
blueprint:
  format: agent-blueprint-v1
---

# clio-core PR Sync Agent

A single-expert CLIO agent that brings every open pull request authored by the
configured GitHub user on `iowarp/clio-core` up to date with `dev`.

The mechanical work lives in `bin/sync_prs.py` (GitHub's "Update branch"
API — merges `dev` into the PR head). The agent runs it, reads the JSON
result, and for PRs that cannot be updated automatically (merge conflicts,
missing permissions) investigates and reports which files conflict. It never
force-pushes and never pushes hand-resolved conflicts.

Turns are triggered by `bin/watch_develop.py`, which polls the `dev` head
and starts a CLIO session on this blueprint only when the SHA changes.
