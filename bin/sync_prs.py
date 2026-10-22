#!/usr/bin/env python3
"""Bring the user's open HDFGroup/hdf5 PRs up to date with develop.

Uses GitHub's "Update branch" API (merges the base branch into the PR head),
so no local clone is needed for the normal path. ``--conflicts N`` does a local
trial merge in a scratch clone to list conflicting files; it never pushes.

Token: $GITHUB_TOKEN / $GH_TOKEN, else ``git credential fill`` for github.com.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = os.environ.get("PRUP_REPO", "HDFGroup/hdf5")
BASE = os.environ.get("PRUP_BASE", "develop")
AUTHOR = os.environ.get("PRUP_AUTHOR", "hyoklee")
SKIP_LABEL = "prup:skip"
CHANGELOG = "release_docs/CHANGELOG.md"
API = "https://api.github.com"
WORK_DIR = Path(os.environ.get("PRUP_WORK_DIR", Path(__file__).resolve().parent.parent / ".work"))


def github_token() -> str:
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(var):
            return os.environ[var]
    out = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    for line in out.stdout.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1]
    sys.exit("error: no GitHub token (set GITHUB_TOKEN or store a github.com git credential)")


class GitHub:
    def __init__(self, token: str) -> None:
        self.token = token

    def request(self, method: str, path: str, body: dict | None = None) -> tuple[int, object]:
        req = urllib.request.Request(
            path if path.startswith("http") else API + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "prup-hdf5-pr-sync",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
                return resp.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw)
            except ValueError:
                return exc.code, {"message": raw.decode(errors="replace")}

    def get(self, path: str) -> object:
        status, data = self.request("GET", path)
        if status != 200:
            raise RuntimeError(f"GET {path}: {status} {data.get('message', '')}")
        return data


def my_prs(gh: GitHub) -> list[dict]:
    prs, page = [], 1
    while True:
        batch = gh.get(f"/repos/{REPO}/pulls?state=open&base={BASE}&per_page=100&page={page}")
        prs += [p for p in batch if p["user"]["login"].lower() == AUTHOR.lower()]
        if len(batch) < 100:
            return sorted(prs, key=lambda p: p["number"])
        page += 1


def sync_one(gh: GitHub, pr: dict, dry_run: bool, include_drafts: bool) -> dict:
    head_repo = pr["head"]["repo"]
    row = {
        "number": pr["number"],
        "title": pr["title"],
        "url": pr["html_url"],
        "head": f"{head_repo['full_name'] if head_repo else '?'}:{pr['head']['ref']}",
        "behind_by": None,
    }
    if any(lbl["name"] == SKIP_LABEL for lbl in pr["labels"]):
        return {**row, "status": "skipped", "detail": f"label {SKIP_LABEL}"}
    if pr["draft"] and not include_drafts:
        return {**row, "status": "skipped", "detail": "draft"}
    if head_repo is None:
        return {**row, "status": "error", "detail": "head repository was deleted"}

    owner = head_repo["owner"]["login"]
    cmp = gh.get(f"/repos/{REPO}/compare/{BASE}...{owner}:{pr['head']['sha']}")
    row["behind_by"] = cmp["behind_by"]
    if cmp["behind_by"] == 0:
        return {**row, "status": "up_to_date"}
    if dry_run:
        return {**row, "status": "would_update"}

    status, data = gh.request(
        "PUT",
        f"/repos/{REPO}/pulls/{pr['number']}/update-branch",
        {"expected_head_sha": pr["head"]["sha"]},
    )
    message = data.get("message", "") if isinstance(data, dict) else ""
    if status == 202:
        return {**row, "status": "updated"}
    if status == 422 and "conflict" in message.lower():
        return {**row, "status": "conflict", "detail": message}
    return {**row, "status": "error", "detail": f"HTTP {status}: {message}"}


def git(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check)


def scratch_clone() -> Path:
    clone = WORK_DIR / "hdf5"
    if not (clone / ".git").exists():
        clone.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", "--filter=blob:none", "--no-checkout",
             f"https://github.com/{REPO}.git", str(clone)],
            check=True, capture_output=True,
        )
    return clone


def list_conflicts(number: int) -> list[str]:
    clone = scratch_clone()
    git("fetch", "--quiet", "origin", f"+refs/heads/{BASE}:refs/remotes/origin/{BASE}",
        f"+refs/pull/{number}/head:refs/prup/{number}", cwd=clone)
    git("checkout", "--quiet", "--detach", "--force", f"refs/prup/{number}", cwd=clone)
    try:
        merge = git("-c", "user.name=prup", "-c", "user.email=prup@localhost",
                    "merge", "--no-commit", "--no-ff", f"origin/{BASE}", cwd=clone, check=False)
        if merge.returncode == 0:
            return []
        files = git("diff", "--name-only", "--diff-filter=U", cwd=clone).stdout.split()
        return files or [merge.stdout.strip() or merge.stderr.strip()]
    finally:
        git("merge", "--abort", cwd=clone, check=False)


def union_added_entries(text: str) -> str | None:
    """Keep both sides of every diff3 conflict hunk, ours first.

    Returns None unless every hunk is two sets of newly added ``### `` entries
    at the same spot (empty merge-base section).
    """
    out: list[str] = []
    ours: list[str] = []
    base: list[str] = []
    theirs: list[str] = []
    state = None
    for line in text.splitlines(keepends=True):
        if line.startswith("<<<<<<< ") and state is None:
            state, ours, base, theirs = "ours", [], [], []
        elif line.startswith("||||||| ") and state == "ours":
            state = "base"
        elif line.startswith("=======") and state == "base":
            state = "theirs"
        elif line.startswith(">>>>>>> ") and state == "theirs":
            sides = [[ln for ln in side if ln.strip()] for side in (ours, theirs)]
            if any(ln.strip() for ln in base) or not all(
                side and side[0].startswith("### ") for side in sides
            ):
                return None
            while ours and not ours[-1].strip():
                ours.pop()
            out += ours + ["\n"] + theirs
            state = None
        elif state == "ours":
            ours.append(line)
        elif state == "base":
            base.append(line)
        elif state == "theirs":
            theirs.append(line)
        else:
            out.append(line)
    return "".join(out) if state is None else None


def removed_lines(clone: Path, *diff_args: str) -> set[str]:
    diff = git("diff", *diff_args, "--", CHANGELOG, cwd=clone).stdout
    return {ln for ln in diff.splitlines() if ln.startswith("-") and not ln.startswith("---")}


def resolve_changelog(pr: dict, push: bool = True) -> tuple[str, str]:
    """Merge develop into the PR branch, keeping both sides of CHANGELOG-only conflicts.

    Returns (status, detail): ``resolved`` after a non-force push, ``would_resolve``
    when ``push`` is False, otherwise ``conflict`` with the reason it was left alone.
    """
    clone = scratch_clone()
    number, ref, sha = pr["number"], pr["head"]["ref"], pr["head"]["sha"]
    head_url = pr["head"]["repo"]["clone_url"]
    head_ref = f"refs/prup/head-{number}"
    git("fetch", "--quiet", "origin", f"+refs/heads/{BASE}:refs/remotes/origin/{BASE}", cwd=clone)
    git("fetch", "--quiet", head_url, f"+refs/heads/{ref}:{head_ref}", cwd=clone)
    if git("rev-parse", head_ref, cwd=clone).stdout.strip() != sha:
        return "conflict", "PR head moved during sync; not auto-resolving"
    git("checkout", "--quiet", "--force", "-B", f"prup-resolve-{number}", head_ref, cwd=clone)
    try:
        merge = git("-c", "merge.conflictStyle=diff3", "merge", "--no-ff", f"origin/{BASE}",
                    "-m", f"Merge branch '{BASE}' into {ref}", cwd=clone, check=False)
        if merge.returncode == 0:
            return "conflict", "merged cleanly locally; left for the next run"
        files = git("diff", "--name-only", "--diff-filter=U", cwd=clone).stdout.split()
        if files != [CHANGELOG]:
            return "conflict", "conflicts outside the CHANGELOG"
        path = clone / CHANGELOG
        resolved = union_added_entries(path.read_text())
        if resolved is None:
            return "conflict", "CHANGELOG conflict is not two sets of added entries"
        path.write_text(resolved)
        git("add", CHANGELOG, cwd=clone)
        git("commit", "--quiet", "--no-edit", cwd=clone)

        # The merge must carry exactly the PR's own changes on top of develop.
        pr_files = git("diff", "--name-only", f"origin/{BASE}...{head_ref}", cwd=clone).stdout
        merged_files = git("diff", "--name-only", f"origin/{BASE}", "HEAD", cwd=clone).stdout
        if pr_files != merged_files or not removed_lines(clone, f"origin/{BASE}", "HEAD") <= (
            removed_lines(clone, f"origin/{BASE}...{head_ref}")
        ):
            return "conflict", "auto-resolution failed verification; not pushed"
        new_sha = git("rev-parse", "HEAD", cwd=clone).stdout.strip()
        if not push:
            return "would_resolve", f"kept both CHANGELOG entries ({new_sha[:10]})"
        pushed = git("push", "--quiet", head_url, f"HEAD:refs/heads/{ref}", cwd=clone, check=False)
        if pushed.returncode != 0:
            return "error", f"push failed: {pushed.stderr.strip().splitlines()[-1:]}"
        return "resolved", f"merged {BASE}, kept both CHANGELOG entries ({new_sha[:10]})"
    finally:
        git("merge", "--abort", cwd=clone, check=False)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="report only; do not update")
    ap.add_argument("--include-drafts", action="store_true", help="also update draft PRs")
    ap.add_argument("--json", action="store_true", help="print machine-readable JSON")
    ap.add_argument("--conflicts", type=int, metavar="PR", help="list conflicting files for PR")
    ap.add_argument("--no-resolve", action="store_true",
                    help=f"do not auto-resolve conflicts confined to {CHANGELOG}")
    args = ap.parse_args()

    if args.conflicts:
        files = list_conflicts(args.conflicts)
        print(json.dumps({"number": args.conflicts, "conflicting_files": files}, indent=2))
        return 0

    gh = GitHub(github_token())
    develop_sha = gh.get(f"/repos/{REPO}/branches/{BASE}")["commit"]["sha"]
    rows = []
    for pr in my_prs(gh):
        try:
            row = sync_one(gh, pr, args.dry_run, args.include_drafts)
            if row["status"] == "conflict":
                try:
                    row["conflicting_files"] = list_conflicts(pr["number"])
                    if row["conflicting_files"] == [CHANGELOG] and not args.no_resolve:
                        row["status"], row["detail"] = resolve_changelog(pr)
                except subprocess.CalledProcessError as exc:
                    row["conflicting_files"] = [f"(could not list: {exc.stderr or exc})"]
            rows.append(row)
        except RuntimeError as exc:
            rows.append({"number": pr["number"], "title": pr["title"], "status": "error",
                         "detail": str(exc)})

    if args.json:
        print(json.dumps({"repo": REPO, "base": BASE, "develop_sha": develop_sha,
                          "dry_run": args.dry_run, "prs": rows}, indent=2))
    else:
        print(f"{REPO}@{BASE} {develop_sha[:10]}{' (dry run)' if args.dry_run else ''}")
        for r in rows:
            extra = f" ({r['detail']})" if r.get("detail") else ""
            behind = f" behind={r['behind_by']}" if r.get("behind_by") else ""
            print(f"  #{r['number']} {r['status']}{behind}{extra} - {r['title']}")
            for path in r.get("conflicting_files", []):
                print(f"      conflict: {path}")
    return 1 if any(r["status"] == "error" for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
