#!/usr/bin/env python3
"""Run the hdf5-pr-sync CLIO agent when HDFGroup/hdf5 develop gets a new commit.

Meant for cron (e.g. every 10 minutes). Each run:
  1. reads the develop head with ``git ls-remote`` (no API quota used);
  2. exits if it matches the SHA recorded by the last successful run;
  3. otherwise starts a CLIO session on the ``hdf5-pr-sync`` blueprint and asks it
     to sync the PRs, falling back to running ``sync_prs.py`` directly if the CLIO
     server cannot be used (``--runner direct`` skips CLIO entirely);
  4. records the SHA only when the sync succeeded, so failures are retried.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
REPO = os.environ.get("PRUP_REPO", "HDFGroup/hdf5")
BASE = os.environ.get("PRUP_BASE", "develop")
BLUEPRINT_ID = "hdf5-pr-sync"
BLUEPRINT_DIR = ROOT / BLUEPRINT_ID
STATE_DIR = Path(os.environ.get("PRUP_STATE_DIR", ROOT / ".state"))
CLIO_PORT = os.environ.get("CLIO_PORT", "17800")
CLIO_URL = f"http://127.0.0.1:{CLIO_PORT}"
CLIO_SERVE = os.environ.get(
    "PRUP_CLIO_SERVE",
    f"uv run --project {Path.home() / 'iowarp/clio-agent'} clio-agent serve --port {CLIO_PORT}",
)
# "<provider_id>/<model_id>": Devstral on ALCF Sophia, the model clio-coder uses.
CLIO_MODEL = os.environ.get(
    "PRUP_CLIO_MODEL", "argonne_sophia/mistralai/Devstral-2-123B-Instruct-2512"
)
CLIO_CODER_CREDENTIALS = Path.home() / ".config/clio-coder/credentials.yaml"
# Same public Globus client clio-coder and clio-agent use for the ALCF gateway.
ALCF_AUTH_CLIENT_ID = "58fdd3bc-e1c3-4ce5-80ea-8d6b87cfb944"
TURN_TIMEOUT_S = 30 * 60


def log(msg: str) -> None:
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


def alcf_token() -> str:
    """ALCF gateway access token: $CLIO_ARGONNE_TOKEN, else clio-coder's stored login.

    clio-coder's credentials file is only read; an expired access token is
    refreshed in memory with its (non-rotating) Globus refresh token.
    """
    if os.environ.get("CLIO_ARGONNE_TOKEN"):
        return os.environ["CLIO_ARGONNE_TOKEN"]
    if not CLIO_CODER_CREDENTIALS.exists():
        return ""
    entry, in_alcf = {}, False
    for line in CLIO_CODER_CREDENTIALS.read_text().splitlines():
        stripped = line.strip()
        if stripped == "alcf:":
            in_alcf = True
        elif in_alcf and ":" in stripped and line.startswith("    "):
            key, _, value = stripped.partition(":")
            entry[key] = value.strip().strip("'\"")
        elif in_alcf and stripped:
            break
    if entry.get("access") and int(entry.get("expires") or 0) > time.time() * 1000 + 600_000:
        return entry["access"]
    if not entry.get("refresh"):
        return ""
    req = urllib.request.Request(
        "https://auth.globus.org/v2/oauth2/token",
        data=urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "client_id": ALCF_AUTH_CLIENT_ID,
            "refresh_token": entry["refresh"],
        }).encode(),
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = json.loads(resp.read())
    for grant in [payload, *payload.get("other_tokens", [])]:
        if "action_all" in grant.get("scope", ""):
            return grant["access_token"]
    return payload.get("access_token", "")


def develop_head() -> str:
    out = subprocess.run(
        ["git", "ls-remote", f"https://github.com/{REPO}.git", f"refs/heads/{BASE}"],
        capture_output=True, text=True, check=True, timeout=120,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    ).stdout.split()
    if not out:
        raise RuntimeError(f"{BASE} not found on {REPO}")
    return out[0]


class Clio:
    def __init__(self) -> None:
        self.token = os.environ.get("CLIO_BEARER_TOKEN", "")
        self.server: subprocess.Popen | None = None

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(
            CLIO_URL + path, method=method, headers=headers,
            data=json.dumps(body).encode() if body is not None else None,
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}

    def healthy(self) -> bool:
        try:
            self.call("GET", "/v1/health")
            return True
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def ensure_server(self) -> None:
        if self.healthy():
            return
        log(f"starting CLIO server: {CLIO_SERVE}")
        env = dict(os.environ)
        if CLIO_MODEL.startswith("argonne_"):
            token = alcf_token()
            if not token:
                raise RuntimeError("no ALCF token (log in with clio-coder or set CLIO_ARGONNE_TOKEN)")
            env["CLIO_ARGONNE_TOKEN"] = token
        with open(STATE_DIR / "clio-server.log", "a") as server_log:
            self.server = subprocess.Popen(
                CLIO_SERVE, shell=True, env=env, stdout=server_log, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, start_new_session=True,
            )
        for _ in range(60):
            if self.healthy():
                return
            time.sleep(5)
        raise RuntimeError("CLIO server did not become healthy")

    def stop_server(self) -> None:
        """Stop a server this run started, so its short-lived ALCF token never goes stale."""
        if self.server is not None:
            os.killpg(self.server.pid, signal.SIGTERM)
            self.server.wait(timeout=60)
            self.server = None

    def run_turn(self, prompt: str) -> str:
        self.ensure_server()
        rows = self.call("GET", "/v1/agent-blueprints").get("agent_blueprints", [])
        installed = {b.get("id") for b in rows}
        if BLUEPRINT_ID not in installed:
            log(f"installing blueprint from {BLUEPRINT_DIR}")
            self.call("POST", "/v1/agent-blueprints/install",
                      {"path": str(BLUEPRINT_DIR), "scope": "global"})
        else:
            # Installs are pinned snapshots; re-snapshot so local edits take effect.
            self.call("POST", f"/v1/agent-blueprints/{BLUEPRINT_ID}/update", {"scope": "global"})
        provider_id, _, model_id = CLIO_MODEL.partition("/")
        session = self.call("POST", "/v1/sessions", {
            "title": f"hdf5 PR sync {datetime.now():%Y-%m-%d %H:%M}",
            "approval_mode": "bypass",
            "model": {"provider_id": provider_id, "model_id": model_id},
        })
        sid = session["id"]
        self.call("POST", f"/v1/sessions/{sid}/agent-blueprint", {"blueprint_id": BLUEPRINT_ID})
        self.call("POST", f"/v1/sessions/{sid}/messages", {"text": prompt, "delivery": "start"})
        log(f"CLIO session {sid} started")

        deadline = time.monotonic() + TURN_TIMEOUT_S
        time.sleep(5)
        while time.monotonic() < deadline:
            status = self.call("GET", f"/v1/sessions/{sid}")["status"]
            if status == "idle":
                break
            if status in {"error", "cancelled", "waiting_user", "waiting_permission"}:
                raise RuntimeError(f"CLIO session {sid} ended in status {status}")
            time.sleep(10)
        else:
            raise RuntimeError(f"CLIO session {sid} timed out")

        # The messages route returns newest-first.
        messages = self.call("GET", f"/v1/sessions/{sid}/messages")
        items = messages.get("messages", messages) if isinstance(messages, dict) else messages
        for msg in items:
            if msg.get("role") == "assistant":
                return "\n".join(
                    p.get("text", "") for p in msg.get("parts", []) if p.get("type") == "text"
                ) or msg.get("text", "")
        raise RuntimeError(f"CLIO session {sid} produced no assistant reply")


def run_direct(dry_run: bool) -> int:
    cmd = [sys.executable, str(HERE / "sync_prs.py")] + (["--dry-run"] if dry_run else [])
    return subprocess.run(cmd).returncode


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--runner", choices=["clio", "direct"], default="clio")
    ap.add_argument("--force", action="store_true", help="sync even if develop is unchanged")
    ap.add_argument("--dry-run", action="store_true", help="report only; record nothing")
    args = ap.parse_args()

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock = open(STATE_DIR / "watch.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("another run is in progress; exiting")
        return 0

    sha_file = STATE_DIR / f"{BASE}.sha"
    head = develop_head()
    last = sha_file.read_text().strip() if sha_file.exists() else ""
    if head == last and not args.force:
        return 0
    log(f"{REPO}@{BASE}: {last[:10] or '(none)'} -> {head[:10]}")

    ok = False
    if args.runner == "clio":
        prompt = (
            f"{BASE} on {REPO} is now at {head}. Sync all of my open PRs with it"
            f"{' as a dry run (pass --dry-run)' if args.dry_run else ''} and report the results."
        )
        clio = Clio()
        try:
            log("agent report:\n" + clio.run_turn(prompt))
            ok = True
        except (RuntimeError, urllib.error.URLError, OSError, KeyError, ValueError) as exc:
            log(f"CLIO run failed ({exc}); falling back to direct sync")
        finally:
            clio.stop_server()
    if not ok:
        ok = run_direct(args.dry_run) == 0

    if ok and not args.dry_run:
        sha_file.write_text(head + "\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
