#!/usr/bin/env python3
"""
GitLab Migration Script — Part A (API + git layer)

Migrates, in order: users -> groups (with hierarchy) -> group membership
-> project shells -> repo history (git mirror push) -> direct project members
-> webhooks -> protected branch rules.

Run individual steps with --steps, or all of them in order (default).
Always run with --dry-run first to see what it *would* do.

Usage:
    python3 migrate_gitlab.py --dry-run
    python3 migrate_gitlab.py --steps users,groups
    python3 migrate_gitlab.py                      # runs everything, for real

Requires: pip install requests pyyaml --break-system-packages
"""

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
import yaml

STATE_FILE = "migration_state.json"
LOG_FILE = "migration.log"


def log(msg, level="INFO"):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [{level}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


class State:
    """Tracks what's already been migrated so the script is safe to re-run."""

    def __init__(self, path=STATE_FILE):
        self.path = path
        self.data = {"users": {}, "groups": {}, "projects": {}, "mirrors": {}}
        if os.path.exists(path):
            with open(path) as f:
                self.data = json.load(f)

    def save(self):
        with open(self.path, "w") as f:
            json.dump(self.data, f, indent=2)

    def mark(self, category, key, value=True):
        self.data.setdefault(category, {})[key] = value
        self.save()

    def is_done(self, category, key):
        return self.data.get(category, {}).get(key) is True


class GitLabAPI:
    def __init__(self, base_url, token):
        self.base_url = base_url.rstrip("/") + "/api/v4"
        self.session = requests.Session()
        self.session.headers.update({"PRIVATE-TOKEN": token})

    def _req(self, method, path, **kwargs):
        url = f"{self.base_url}{path}"
        resp = self.session.request(method, url, **kwargs)
        if resp.status_code >= 400:
            log(f"API error {resp.status_code} on {method} {path}: {resp.text[:300]}", "ERROR")
        return resp

    def get_all(self, path, params=None):
        """Handles GitLab's pagination automatically."""
        params = dict(params or {})
        params["per_page"] = 100
        page = 1
        results = []
        while True:
            params["page"] = page
            resp = self._req("GET", path, params=params)
            if resp.status_code != 200:
                break
            batch = resp.json()
            if not batch:
                break
            results.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return results

    def get(self, path, params=None):
        resp = self._req("GET", path, params=params)
        return resp.json() if resp.status_code == 200 else None

    def post(self, path, data=None):
        return self._req("POST", path, data=data)

    def put(self, path, data=None):
        return self._req("PUT", path, data=data)


def load_config(path="config.yaml"):
    with open(path) as f:
        cfg = yaml.safe_load(f)
    # env vars override file values for tokens (safer for real migration)
    cfg["legacy"]["token"] = os.environ.get("GITLAB_LEGACY_TOKEN", cfg["legacy"]["token"])
    cfg["new"]["token"] = os.environ.get("GITLAB_NEW_TOKEN", cfg["new"]["token"])
    return cfg


# ---------------------------------------------------------------------------
# STEP 1: USERS
# ---------------------------------------------------------------------------

def migrate_users(legacy: GitLabAPI, new: GitLabAPI, cfg, state: State, dry_run):
    log("=== STEP 1: Users ===")
    skiplist = set(cfg["options"].get("user_skiplist", []) or cfg.get("user_skiplist", []))
    # NOTE: GET /users only returns the `is_admin` field when called with an
    # admin token (which config.yaml requires anyway) - this is not exposed
    # to non-admin tokens.
    users = legacy.get_all("/users")
    for u in users:
        username = u["username"]
        if username in skiplist:
            log(f"Skipping user {username} (skiplist)")
            continue
        if state.is_done("users", username):
            log(f"User {username} already migrated, skipping")
            continue

        existing = new.get_all("/users", params={"username": username})
        if existing:
            log(f"User {username} already exists on new instance, marking done")
            state.mark("users", username)
            continue

        if dry_run:
            log(f"[DRY RUN] Would create user: {username} ({u.get('email', 'no-email')})")
            continue

        is_admin = bool(u.get("is_admin"))
        resp = new.post("/users", data={
            "email": u.get("email") or f"{username}@migrated.local",
            "username": username,
            "name": u.get("name", username),
            "password": cfg["options"]["temp_password"],
            "skip_confirmation": True,
            "reset_password": cfg["options"].get("force_password_reset", True),
            "admin": is_admin,
        })
        if resp.status_code == 201:
            log(f"Created user {username}" + (" (ADMIN)" if is_admin else ""))
            state.mark("users", username)
        else:
            log(f"Failed to create user {username}: {resp.text[:200]}", "ERROR")


# ---------------------------------------------------------------------------
# STEP 2: GROUPS (parents before children)
# ---------------------------------------------------------------------------

def migrate_groups(legacy: GitLabAPI, new: GitLabAPI, cfg, state: State, dry_run):
    log("=== STEP 2: Groups ===")
    groups = legacy.get_all("/groups", params={"all_available": True})
    # sort so parents (shorter full_path) are created before children
    groups.sort(key=lambda g: g["full_path"].count("/"))

    for g in groups:
        full_path = g["full_path"]
        if state.is_done("groups", full_path):
            log(f"Group {full_path} already migrated, skipping")
            continue

        existing = new.get(f"/groups/{requests.utils.quote(full_path, safe='')}")
        if existing:
            log(f"Group {full_path} already exists, marking done")
            state.mark("groups", full_path)
            continue

        parent_id = None
        if "/" in full_path:
            parent_path = full_path.rsplit("/", 1)[0]
            parent = new.get(f"/groups/{requests.utils.quote(parent_path, safe='')}")
            if not parent:
                log(f"Parent group {parent_path} not found for {full_path} — skipping for now, re-run after parent exists", "WARN")
                continue
            parent_id = parent["id"]

        if dry_run:
            log(f"[DRY RUN] Would create group: {full_path} (parent_id={parent_id})")
            continue

        resp = new.post("/groups", data={
            "name": g["name"],
            "path": g["path"],
            "description": g.get("description", ""),
            "parent_id": parent_id,
            "visibility": g.get("visibility", "private"),
        })
        if resp.status_code == 201:
            log(f"Created group {full_path}")
            state.mark("groups", full_path)
        else:
            log(f"Failed to create group {full_path}: {resp.text[:200]}", "ERROR")


# ---------------------------------------------------------------------------
# STEP 3: GROUP MEMBERSHIP
# ---------------------------------------------------------------------------

def migrate_membership(legacy: GitLabAPI, new: GitLabAPI, cfg, state: State, dry_run):
    log("=== STEP 3: Group Membership ===")
    groups = legacy.get_all("/groups", params={"all_available": True})
    for g in groups:
        full_path = g["full_path"]
        key = f"members:{full_path}"
        if state.is_done("groups", key):
            log(f"Membership for {full_path} already migrated, skipping")
            continue

        new_group = new.get(f"/groups/{requests.utils.quote(full_path, safe='')}")
        if not new_group:
            log(f"Group {full_path} not found on new instance yet — run groups step first", "WARN")
            continue

        members = legacy.get_all(f"/groups/{g['id']}/members")
        for m in members:
            username = m["username"]
            new_user = new.get_all("/users", params={"username": username})
            if not new_user:
                log(f"User {username} not found on new instance, skipping membership add", "WARN")
                continue
            new_user_id = new_user[0]["id"]

            if dry_run:
                log(f"[DRY RUN] Would add {username} to {full_path} at access_level={m['access_level']}")
                continue

            resp = new.post(f"/groups/{new_group['id']}/members", data={
                "user_id": new_user_id,
                "access_level": m["access_level"],
            })
            if resp.status_code in (201, 409):  # 409 = already a member
                log(f"Added/confirmed {username} in {full_path}")
            else:
                log(f"Failed to add {username} to {full_path}: {resp.text[:200]}", "ERROR")

        if not dry_run:
            state.mark("groups", key)


# ---------------------------------------------------------------------------
# STEP 4: PROJECT SHELLS
# ---------------------------------------------------------------------------

def migrate_projects(legacy: GitLabAPI, new: GitLabAPI, cfg, state: State, dry_run):
    log("=== STEP 4: Project shells ===")
    allowlist = set(cfg.get("project_allowlist") or [])
    projects = legacy.get_all("/projects", params={"membership": False})

    for p in projects:
        path_with_namespace = p["path_with_namespace"]
        if allowlist and path_with_namespace not in allowlist:
            continue
        if state.is_done("projects", path_with_namespace):
            log(f"Project {path_with_namespace} already migrated, skipping")
            continue

        namespace_path = path_with_namespace.rsplit("/", 1)[0]
        # try as a group first (handles nested paths like company/engineering/frontend)
        namespace = new.get(f"/groups/{requests.utils.quote(namespace_path, safe='')}")
        if not namespace:
            # fall back to personal user namespace
            candidates = new.get_all("/namespaces", params={"search": namespace_path.split("/")[-1]})
            namespace = next((n for n in candidates if n["full_path"] == namespace_path), None)
        if not namespace:
            log(f"Namespace {namespace_path} not found on new instance — run groups/users step first", "WARN")
            continue

        existing = new.get(f"/projects/{requests.utils.quote(path_with_namespace, safe='')}")
        if existing:
            log(f"Project {path_with_namespace} already exists, marking done")
            state.mark("projects", path_with_namespace)
            continue

        if dry_run:
            log(f"[DRY RUN] Would create project: {path_with_namespace}")
            continue

        resp = new.post("/projects", data={
            "name": p["name"],
            "path": p["path"],
            "namespace_id": namespace["id"],
            "description": p.get("description", ""),
            "visibility": p.get("visibility", "private"),
        })
        if resp.status_code == 201:
            log(f"Created project {path_with_namespace}")
            state.mark("projects", path_with_namespace)
        else:
            log(f"Failed to create project {path_with_namespace}: {resp.text[:200]}", "ERROR")


# ---------------------------------------------------------------------------
# STEP 5: REPO MIRROR PUSH
# ---------------------------------------------------------------------------

GIT_SUBPROCESS_TIMEOUT = 1800  # 30 min per git operation - large real repos may need to raise this


def _unprotect_all_branches(new_url, new_token, path_with_namespace):
    """Remove branch protection so mirror's force-push isn't rejected.
    Returns the list of protection records that were removed, so the caller
    can restore them after the push (see _reprotect_branches)."""
    api = GitLabAPI(new_url, new_token)
    enc_path = requests.utils.quote(path_with_namespace, safe="")
    protected = api.get_all(f"/projects/{enc_path}/protected_branches")
    removed = []
    for pb in protected:
        api._req("DELETE", f"/projects/{enc_path}/protected_branches/{requests.utils.quote(pb['name'], safe='')}")
        log(f"  Unprotected branch {pb['name']} on {path_with_namespace} (temporarily, for mirror push)")
        removed.append(pb)
    return removed


def _reprotect_branches(new_url, new_token, path_with_namespace, removed):
    """Restore branch protection removed by _unprotect_all_branches, preserving
    the original push/merge access levels."""
    if not removed:
        return
    api = GitLabAPI(new_url, new_token)
    enc_path = requests.utils.quote(path_with_namespace, safe="")
    for pb in removed:
        data = {
            "name": pb["name"],
            "push_access_level": pb.get("push_access_levels", [{}])[0].get("access_level", 40),
            "merge_access_level": pb.get("merge_access_levels", [{}])[0].get("access_level", 40),
        }
        allow_force_push = pb.get("allow_force_push")
        if allow_force_push is not None:
            data["allow_force_push"] = allow_force_push
        resp = api.post(f"/projects/{enc_path}/protected_branches", data=data)
        if resp.status_code == 201:
            log(f"  Re-protected branch {pb['name']} on {path_with_namespace}")
        else:
            log(f"  Failed to re-protect branch {pb['name']} on {path_with_namespace}: {resp.text[:200]}"
                " — re-apply protection manually", "ERROR")


def _mirror_one(path_with_namespace, legacy_url, legacy_token, new_url, new_token, work_dir, dry_run):
    if dry_run:
        log(f"[DRY RUN] Would mirror-push: {path_with_namespace}")
        return True

    clone_dir = Path(work_dir) / (path_with_namespace.replace("/", "__") + ".git")
    clone_dir.parent.mkdir(parents=True, exist_ok=True)

    legacy_auth_url = legacy_url.replace("://", f"://oauth2:{legacy_token}@")
    new_auth_url = new_url.replace("://", f"://oauth2:{new_token}@")

    removed_protections = []
    try:
        # protected branches (commonly the default branch, auto-protected on project creation)
        # reject the force-push that --mirror performs, so clear them first.
        removed_protections = _unprotect_all_branches(new_url, new_token, path_with_namespace)

        env = {**os.environ, "GIT_LFS_SKIP_SMUDGE": "1"}  # don't download LFS content during the plain clone
        subprocess.run(
            ["git", "clone", "--mirror", f"{legacy_auth_url}/{path_with_namespace}.git", str(clone_dir)],
            check=True, capture_output=True, text=True, env=env, timeout=GIT_SUBPROCESS_TIMEOUT,
        )

        # if this repo uses LFS, fetch+push the real objects BEFORE the mirror push —
        # GitLab's pre-receive hook rejects a push containing LFS pointers unless the
        # actual LFS objects are already present on the server.
        lfs_check = subprocess.run(
            ["git", "-C", str(clone_dir), "lfs", "ls-files"],
            capture_output=True, text=True, timeout=120,
        )
        has_lfs = bool(lfs_check.stdout.strip())
        if has_lfs:
            log(f"  {path_with_namespace} uses Git LFS — fetching LFS objects from legacy")
            subprocess.run(
                ["git", "-C", str(clone_dir), "lfs", "fetch", "--all", legacy_auth_url + f"/{path_with_namespace}.git"],
                check=True, capture_output=True, text=True, timeout=GIT_SUBPROCESS_TIMEOUT,
            )
            log(f"  Pushing LFS objects for {path_with_namespace} to new instance")
            subprocess.run(
                ["git", "-C", str(clone_dir), "lfs", "push", "--all", f"{new_auth_url}/{path_with_namespace}.git"],
                check=True, capture_output=True, text=True, timeout=GIT_SUBPROCESS_TIMEOUT,
            )

        # --mirror pushes ALL refs, including GitLab's internal server-managed refs
        # (refs/merge-requests/N/head, /merge) which GitLab always rejects as pushes —
        # they're generated server-side, not real branches. Push only real branches/tags/notes.
        subprocess.run(
            ["git", "-C", str(clone_dir), "push", "--force",
             f"{new_auth_url}/{path_with_namespace}.git",
             "refs/heads/*:refs/heads/*", "refs/tags/*:refs/tags/*"],
            check=True, capture_output=True, text=True, timeout=GIT_SUBPROCESS_TIMEOUT,
        )

        log(f"Mirrored {path_with_namespace}")
        return True
    except subprocess.TimeoutExpired as e:
        log(f"Mirror TIMED OUT for {path_with_namespace} after {e.timeout}s on `{' '.join(e.cmd)}`"
            " — branches were left unprotected during this attempt; will retry re-protection now"
            " and this repo can be safely re-run.", "ERROR")
        return False
    except subprocess.CalledProcessError as e:
        log(f"Mirror failed for {path_with_namespace}: {e.stderr[:600]}", "ERROR")
        return False
    finally:
        # Always try to restore protection, whether the mirror succeeded, failed, or timed out -
        # a project should never be left permanently unprotected because of this script.
        try:
            _reprotect_branches(new_url, new_token, path_with_namespace, removed_protections)
        except Exception as e:
            log(f"  Could not re-protect branches on {path_with_namespace}: {e}"
                " — re-apply protection manually", "ERROR")
        subprocess.run(["rm", "-rf", str(clone_dir)])


def migrate_mirror(legacy: GitLabAPI, new: GitLabAPI, cfg, state: State, dry_run):
    log("=== STEP 5: Repo mirror push ===")
    allowlist = set(cfg.get("project_allowlist") or [])
    projects = legacy.get_all("/projects", params={"membership": False})
    work_dir = cfg["options"]["mirror_work_dir"]
    parallel = cfg["options"]["mirror_parallel_jobs"]

    to_mirror = []
    for p in projects:
        path_with_namespace = p["path_with_namespace"]
        if allowlist and path_with_namespace not in allowlist:
            continue
        if state.is_done("mirrors", path_with_namespace):
            log(f"Repo {path_with_namespace} already mirrored, skipping")
            continue
        to_mirror.append(path_with_namespace)

    with ThreadPoolExecutor(max_workers=parallel) as pool:
        futures = {
            pool.submit(
                _mirror_one, path, cfg["legacy"]["url"], cfg["legacy"]["token"],
                cfg["new"]["url"], cfg["new"]["token"], work_dir, dry_run,
            ): path
            for path in to_mirror
        }
        for future in as_completed(futures):
            path = futures[future]
            success = future.result()
            if success and not dry_run:
                state.mark("mirrors", path)


# ---------------------------------------------------------------------------
# STEP 6: PROJECT MEMBERS (direct members only; group members are step 3)
# ---------------------------------------------------------------------------

def _scoped_projects(legacy: GitLabAPI, cfg):
    allowlist = set(cfg.get("project_allowlist") or [])
    projects = legacy.get_all("/projects", params={"membership": False})
    return [p for p in projects if not allowlist or p["path_with_namespace"] in allowlist]


def _new_project(new: GitLabAPI, path_with_namespace):
    return new.get(f"/projects/{requests.utils.quote(path_with_namespace, safe='')}")


def migrate_project_members(legacy: GitLabAPI, new: GitLabAPI, cfg, state: State, dry_run):
    log("=== STEP 6: Project members (direct) ===")
    for p in _scoped_projects(legacy, cfg):
        path = p["path_with_namespace"]
        if state.is_done("project_members", path):
            log(f"Project members for {path} already migrated, skipping")
            continue
        np_ = _new_project(new, path)
        if not np_:
            log(f"Project {path} not found on new instance - run projects step first", "WARN")
            continue
        failures = 0
        # /members (not /members/all) = direct members only; inherited ones come from groups
        for m in legacy.get_all(f"/projects/{p['id']}/members"):
            username = m["username"]
            nu = new.get_all("/users", params={"username": username})
            if not nu:
                log(f"User {username} not found on new instance, skipping membership add on {path}", "WARN")
                continue
            if dry_run:
                log(f"[DRY RUN] Would add {username} to {path} at access_level={m['access_level']}")
                continue
            resp = new.post(f"/projects/{np_['id']}/members", data={
                "user_id": nu[0]["id"], "access_level": m["access_level"]})
            if resp.status_code in (201, 409):
                log(f"Added/confirmed {username} in {path}")
            else:
                failures += 1
                log(f"Failed to add {username} to {path}: {resp.text[:200]}", "ERROR")
        if not dry_run and failures == 0:
            state.mark("project_members", path)


# ---------------------------------------------------------------------------
# STEP 7: WEBHOOKS
# ---------------------------------------------------------------------------

HOOK_FIELDS = [
    "push_events", "tag_push_events", "merge_requests_events", "issues_events",
    "confidential_issues_events", "note_events", "confidential_note_events",
    "job_events", "pipeline_events", "wiki_page_events",
    "enable_ssl_verification", "push_events_branch_filter",
]


def migrate_webhooks(legacy: GitLabAPI, new: GitLabAPI, cfg, state: State, dry_run):
    log("=== STEP 7: Webhooks ===")
    for p in _scoped_projects(legacy, cfg):
        path = p["path_with_namespace"]
        if state.is_done("webhooks", path):
            log(f"Webhooks for {path} already migrated, skipping")
            continue
        hooks = legacy.get_all(f"/projects/{p['id']}/hooks")
        if not hooks:
            if not dry_run:
                state.mark("webhooks", path)
            continue
        np_ = _new_project(new, path)
        if not np_:
            log(f"Project {path} not found on new instance - run projects step first", "WARN")
            continue
        existing_urls = {h["url"] for h in new.get_all(f"/projects/{np_['id']}/hooks")}
        failures = 0
        for h in hooks:
            if h["url"] in existing_urls:
                log(f"Webhook {h['url']} already on {path}, skipping")
                continue
            if dry_run:
                log(f"[DRY RUN] Would create webhook {h['url']} on {path}")
                continue
            data = {"url": h["url"]}
            for f in HOOK_FIELDS:
                if h.get(f) is not None:
                    data[f] = h[f]
            resp = new.post(f"/projects/{np_['id']}/hooks", data=data)
            if resp.status_code == 201:
                log(f"Created webhook {h['url']} on {path}"
                    " (secret token, if any, is NOT readable from legacy - re-enter it by hand)")
            else:
                failures += 1
                log(f"Failed to create webhook {h['url']} on {path}: {resp.text[:200]}"
                    " (if it says 'local network', enable 'Allow requests to the local network from"
                    " webhooks' in the new instance's Admin > Settings > Network)", "ERROR")
        if not dry_run and failures == 0:
            state.mark("webhooks", path)


# ---------------------------------------------------------------------------
# STEP 8: PROTECTED BRANCHES (legacy rules -> new; run AFTER mirror)
# ---------------------------------------------------------------------------

def migrate_protected_branches(legacy: GitLabAPI, new: GitLabAPI, cfg, state: State, dry_run):
    log("=== STEP 8: Protected branches ===")
    for p in _scoped_projects(legacy, cfg):
        path = p["path_with_namespace"]
        if state.is_done("protected_branches", path):
            log(f"Protected branches for {path} already migrated, skipping")
            continue
        rules = legacy.get_all(f"/projects/{p['id']}/protected_branches")
        if not rules:
            if not dry_run:
                state.mark("protected_branches", path)
            continue
        np_ = _new_project(new, path)
        if not np_:
            log(f"Project {path} not found on new instance - run projects step first", "WARN")
            continue
        enc = f"/projects/{np_['id']}/protected_branches"
        failures = 0
        for pb in rules:
            name = pb["name"]
            push_lvl = (pb.get("push_access_levels") or [{}])[0].get("access_level", 40)
            merge_lvl = (pb.get("merge_access_levels") or [{}])[0].get("access_level", 40)
            data = {"name": name, "push_access_level": push_lvl, "merge_access_level": merge_lvl}
            if pb.get("allow_force_push") is not None:
                data["allow_force_push"] = pb["allow_force_push"]
            if dry_run:
                log(f"[DRY RUN] Would protect {name} on {path} (push={push_lvl}, merge={merge_lvl})")
                continue
            # replace any auto-created rule with the legacy one
            new._req("DELETE", f"{enc}/{requests.utils.quote(name, safe='')}")
            resp = new.post(enc, data=data)
            if resp.status_code == 201:
                log(f"Protected {name} on {path} (push={push_lvl}, merge={merge_lvl})")
            else:
                failures += 1
                log(f"Failed to protect {name} on {path}: {resp.text[:200]}", "ERROR")
        if not dry_run and failures == 0:
            state.mark("protected_branches", path)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

STEP_FUNCS = {
    "users": migrate_users,
    "groups": migrate_groups,
    "membership": migrate_membership,
    "projects": migrate_projects,
    "mirror": migrate_mirror,
    "project_members": migrate_project_members,
    "webhooks": migrate_webhooks,
    "protected_branches": migrate_protected_branches,
}
STEP_ORDER = ["users", "groups", "membership", "projects", "mirror",
              "project_members", "webhooks", "protected_branches"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--steps", default=",".join(STEP_ORDER),
                         help="comma-separated subset of: " + ",".join(STEP_ORDER))
    parser.add_argument("--dry-run", action="store_true", help="override config.yaml dry_run to true")
    args = parser.parse_args()

    cfg = load_config(args.config)
    dry_run = args.dry_run or cfg["options"].get("dry_run", True)
    if dry_run:
        log("Running in DRY-RUN mode — no changes will be made.")

    legacy = GitLabAPI(cfg["legacy"]["url"], cfg["legacy"]["token"])
    new = GitLabAPI(cfg["new"]["url"], cfg["new"]["token"])
    state = State()

    steps = [s.strip() for s in args.steps.split(",") if s.strip()]
    for step in STEP_ORDER:
        if step not in steps:
            continue
        opt_key = f"skip_{step}" if step != "membership" else "skip_membership"
        if cfg["options"].get(opt_key):
            log(f"Skipping step '{step}' per config")
            continue
        STEP_FUNCS[step](legacy, new, cfg, state, dry_run)

    log("Done.")


if __name__ == "__main__":
    main()
