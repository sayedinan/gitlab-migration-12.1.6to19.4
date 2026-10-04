#!/usr/bin/env python3
"""
sync_delta.py — find what changed on LEGACY since the last migration run,
and heal migration_state.json so a normal re-run of migrate_gitlab.py
picks up exactly that (and only that).

This script does NOT create or push anything itself. It only:
  1. Compares legacy vs new for users, groups, projects (existence).
  2. For existing projects, compares each branch's HEAD commit on legacy
     vs new to catch new commits/branches pushed to legacy post-migration.
  3. Clears the relevant entries in migration_state.json when it finds a
     mismatch, so migrate_gitlab.py's own idempotent logic re-does exactly
     those items on its next run.

Run this ON THE NEW INSTANCE, in the same folder as migrate_gitlab.py and
config.yaml (it imports migrate_gitlab.py directly, so run it from there):

    python3 sync_delta.py --config config.yaml            # report only
    python3 sync_delta.py --config config.yaml --fix       # report + heal state
    python3 migrate_gitlab.py --dry-run                    # confirm what will run
    python3 migrate_gitlab.py                              # do it

Then re-run Part B (migrate_metadata.rb) as usual — it's incremental per
issue/MR/comment and safe to run repeatedly; it will pick up new items
created on legacy since the last run without duplicating old ones.

WHAT THIS DELIBERATELY DOES NOT COVER:
  - New comments/issues/MRs on legacy — already handled by re-running
    migrate_metadata.rb directly, no state healing needed for those.
  - Direct pushes to the NEW instance's projects. Re-mirroring
    force-pushes, so if anyone has pushed directly to new since the
    migration, --fix will show it as "would re-mirror" and doing so WILL
    overwrite that work. Read every re-mirror line before running --fix.
  - Users/groups/projects deleted on LEGACY (not created). This script
    only looks for things that are new or changed on legacy; it will not
    propose deleting anything on new.
"""
import argparse
import json
import sys

from migrate_gitlab import GitLabAPI, State, load_config, log


def branch_heads(api: GitLabAPI, project_id):
    """{branch_name: commit_sha} for a project, or {} if unreachable."""
    branches = api.get_all(f"/projects/{project_id}/repository/branches")
    return {b["name"]: b["commit"]["id"] for b in branches}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--fix", action="store_true",
                     help="Actually clear stale state entries. Without this flag, report only.")
    args = ap.parse_args()

    cfg = load_config(args.config)
    legacy = GitLabAPI(cfg["legacy"]["url"], cfg["legacy"]["token"])
    new = GitLabAPI(cfg["new"]["url"], cfg["new"]["token"])
    state = State()

    changes = {"users": [], "groups": [], "projects": [], "mirrors": []}

    # ---- users ----
    legacy_users = {u["username"] for u in legacy.get_all("/users")}
    new_users = {u["username"] for u in new.get_all("/users")}
    for username in sorted(legacy_users):
        if username in new_users:
            continue
        if state.is_done("users", username):
            changes["users"].append(username)
            log(f"User '{username}' marked done in state but missing on new — will re-add", "WARN")
        # else: brand new user, migrate_gitlab.py will pick it up on its own, nothing to heal

    # ---- groups ----
    legacy_groups = {g["full_path"] for g in legacy.get_all("/groups", params={"all_available": True})}
    new_groups = {g["full_path"] for g in new.get_all("/groups", params={"all_available": True})}
    for path in sorted(legacy_groups):
        if path in new_groups:
            continue
        if state.is_done("groups", path):
            changes["groups"].append(path)
            log(f"Group '{path}' marked done in state but missing on new — will re-add", "WARN")

    # ---- projects (existence + drift) ----
    allowlist = set(cfg.get("project_allowlist") or [])
    legacy_projects = legacy.get_all("/projects", params={"membership": False})
    new_project_paths = {p["path_with_namespace"] for p in new.get_all("/projects", params={"membership": False})}

    for lp in legacy_projects:
        path = lp["path_with_namespace"]
        if allowlist and path not in allowlist:
            continue

        if path not in new_project_paths:
            if state.is_done("projects", path) or state.is_done("mirrors", path):
                changes["projects"].append(path)
                log(f"Project '{path}' marked done in state but missing on new — will re-create + re-mirror", "WARN")
            continue  # brand new project: normal run already handles it, nothing to heal

        # project exists both sides — check for commit/branch drift
        if not state.is_done("mirrors", path):
            continue  # never successfully mirrored yet; normal run will handle it
        np_ = new.get(f"/projects/{path.replace('/', '%2F')}")
        if not np_:
            continue
        legacy_heads = branch_heads(legacy, lp["id"])
        new_heads = branch_heads(new, np_["id"])
        if legacy_heads != new_heads:
            new_branches = set(legacy_heads) - set(new_heads)
            moved = {b for b in (set(legacy_heads) & set(new_heads)) if legacy_heads[b] != new_heads[b]}
            detail = []
            if new_branches:
                detail.append(f"new branch(es) {sorted(new_branches)}")
            if moved:
                detail.append(f"moved branch(es) {sorted(moved)}")
            changes["mirrors"].append(path)
            log(f"Project '{path}' has {' and '.join(detail)} on legacy not on new — will re-mirror (force-push!)", "WARN")

    # ---- summary ----
    total = sum(len(v) for v in changes.values())
    print("\n" + "=" * 60)
    print(f"SUMMARY: {total} drifted item(s) found")
    for cat, items in changes.items():
        if items:
            print(f"  {cat}: {len(items)} -> {items}")
    print("=" * 60)

    if total == 0:
        print("Nothing to heal. A plain 'python3 migrate_gitlab.py --dry-run' will show")
        print("only genuinely new users/groups/projects (if any) as pending.")
        return

    if not args.fix:
        print("\nRun again with --fix to clear these from migration_state.json.")
        print("Re-mirroring force-pushes — review the 'mirrors' list above before doing so")
        print("if anyone may have pushed directly to the new instance.")
        return

    for username in changes["users"]:
        state.data.get("users", {}).pop(username, None)
    for path in changes["groups"]:
        state.data.get("groups", {}).pop(path, None)
    for path in changes["projects"]:
        state.data.get("projects", {}).pop(path, None)
        state.data.get("mirrors", {}).pop(path, None)
    for path in changes["mirrors"]:
        state.data.get("mirrors", {}).pop(path, None)
    state.save()
    print(f"\nHealed {total} entries in {state.path}.")
    print("Now run: python3 migrate_gitlab.py --dry-run   (then, if it looks right, without --dry-run)")


if __name__ == "__main__":
    main()
