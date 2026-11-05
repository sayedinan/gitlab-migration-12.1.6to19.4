#!/usr/bin/env python3
"""
block.py - FINAL migration step: mirror "blocked" users from legacy to new.

Run this LAST, after Part A, Part B (issues/MRs), wikis, validation and the
link fix. Blocking earlier can break steps that act on behalf of a user
(e.g. snippets owned by a blocked user fail).

What it does:
  1. Reads all users on the LEGACY instance.
  2. For each user whose state is "blocked", finds the same username on the NEW
     instance and blocks it there (POST /users/:id/block).
  3. Never unblocks anyone. Users blocked on new but active on legacy are only
     reported, so you can decide by hand.

Blocking only stops login. Commits, issues, MRs, comments, wikis and project
memberships of a blocked user are kept.

Uses the same config.yaml as migrate_gitlab.py (legacy/new url + token,
GITLAB_LEGACY_TOKEN / GITLAB_NEW_TOKEN env overrides, user_skiplist, dry_run).

Usage:
    python3 block.py --dry-run     # show what would be blocked
    python3 block.py               # honours options.dry_run in config.yaml
"""

import argparse
import os
import time

import requests
import yaml

LOG_FILE = "migration.log"


def log(msg, level="INFO"):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [{level}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


class GitLabAPI:
    def __init__(self, base_url, token):
        self.base_url = base_url.rstrip("/") + "/api/v4"
        self.session = requests.Session()
        self.session.headers.update({"PRIVATE-TOKEN": token})

    def req(self, method, path, **kwargs):
        return self.session.request(method, f"{self.base_url}{path}", **kwargs)

    def get_all(self, path, params=None):
        params = dict(params or {})
        params["per_page"] = 100
        page, results = 1, []
        while True:
            params["page"] = page
            resp = self.req("GET", path, params=params)
            if resp.status_code != 200:
                log(f"API error {resp.status_code} on GET {path}: {resp.text[:200]}", "ERROR")
                break
            batch = resp.json()
            if not batch:
                break
            results.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return results


def load_config(path):
    with open(path) as f:
        cfg = yaml.safe_load(f)
    cfg["legacy"]["token"] = os.environ.get("GITLAB_LEGACY_TOKEN", cfg["legacy"]["token"])
    cfg["new"]["token"] = os.environ.get("GITLAB_NEW_TOKEN", cfg["new"]["token"])
    return cfg


def is_blocked(user):
    # 12.x reports "blocked"; newer versions also have "ldap_blocked" etc.
    return str(user.get("state", "")).startswith(("blocked", "ldap_blocked"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--dry-run", action="store_true",
                        help="override config.yaml dry_run to true")
    args = parser.parse_args()

    cfg = load_config(args.config)
    dry_run = args.dry_run or cfg["options"].get("dry_run", True)
    if dry_run:
        log("Running in DRY-RUN mode - no changes will be made.")

    skiplist = set(cfg["options"].get("user_skiplist", []) or cfg.get("user_skiplist", []))
    legacy = GitLabAPI(cfg["legacy"]["url"], cfg["legacy"]["token"])
    new = GitLabAPI(cfg["new"]["url"], cfg["new"]["token"])

    log("=== FINAL STEP: Block users that are blocked on legacy ===")

    # Safety: never block the account that owns the new-instance token.
    me = new.req("GET", "/user")
    my_username = me.json().get("username") if me.status_code == 200 else None

    counts = {"blocked": 0, "already": 0, "missing": 0, "skipped": 0, "failed": 0}
    legacy_users = legacy.get_all("/users")
    log(f"Legacy users: {len(legacy_users)}")

    for lu in legacy_users:
        username = lu["username"]
        if not is_blocked(lu):
            continue
        if username in skiplist or username == my_username:
            log(f"Skipping {username} (skiplist / token owner)")
            counts["skipped"] += 1
            continue

        found = new.get_all("/users", params={"username": username})
        found = [u for u in found if u["username"] == username]
        if not found:
            log(f"User {username} is blocked on legacy but missing on new instance "
                f"- run migrate_gitlab.py users step first", "WARN")
            counts["missing"] += 1
            continue

        nu = found[0]
        if is_blocked(nu):
            log(f"User {username} already blocked on new instance")
            counts["already"] += 1
            continue

        if dry_run:
            log(f"[DRY RUN] Would block user {username} to match source")
            counts["blocked"] += 1
            continue

        r = new.req("POST", f"/users/{nu['id']}/block")
        if r.status_code in (200, 201):
            log(f"Blocked user {username} to match source")
            counts["blocked"] += 1
        elif r.status_code == 403:
            log(f"User {username} could not be blocked (already blocked or LDAP-managed): "
                f"{r.text[:150]}", "WARN")
            counts["already"] += 1
        else:
            log(f"Failed to block user {username}: {r.status_code} {r.text[:200]}", "ERROR")
            counts["failed"] += 1

    # Informational only: blocked on new but active on legacy.
    legacy_active = {u["username"] for u in legacy_users if not is_blocked(u)}
    for nu in new.get_all("/users", params={"blocked": "true"}):
        if nu["username"] in legacy_active:
            log(f"NOTE: {nu['username']} is blocked on new but ACTIVE on legacy "
                f"(not changed by this script)", "WARN")

    log(f"Summary: {'would block' if dry_run else 'blocked'}={counts['blocked']}, "
        f"already blocked={counts['already']}, missing on new={counts['missing']}, "
        f"skipped={counts['skipped']}, failed={counts['failed']}")
    log("Done.")


if __name__ == "__main__":
    main()
