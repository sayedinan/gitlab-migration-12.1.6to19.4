#!/usr/bin/env python3
"""DANGER: deletes EVERYTHING on this GitLab instance except the root user.

This is deliberately separate from delete_users.py, which only touches seed
data and refuses to touch anything else. This script does the opposite: it
wipes every project, every group, and every user (except root), regardless
of whether it came from the seed scripts, the UI, or anywhere else.

Order matters: projects first (top-level personal + group ones), then
groups (deleting a group also cascades any project still inside it, as a
safety net), then users last (hard-deleted, taking any remaining personal
projects/snippets with them).

Usage: python3 wipe_instance.py [--yes]
"""
from seed_common import *


def get_all(path):
    """Paginate through a GitLab list endpoint, collecting every page."""
    results, page = [], 1
    while True:
        body = api("GET", f"{path}{'&' if '?' in path else '?'}per_page=100&page={page}")
        if not body:
            break
        results.extend(body)
        if len(body) < 100:
            break
        page += 1
    return results


def main():
    projects = get_all("/projects?membership=false")
    groups = get_all("/groups?all_available=true")
    top_groups = sorted({g["full_path"] for g in groups if "/" not in g["full_path"]})
    users = [u for u in get_all("/users") if u["username"] != "root"]

    print(f"This will permanently delete:")
    print(f"  {len(projects)} project(s)")
    print(f"  {len(top_groups)} top-level group(s) (with all subgroups/projects inside them)")
    print(f"  {len(users)} user(s) (every user except root)")
    print("Root user and root's own personal projects/settings are left untouched.")
    if not confirm("Type y/yes to confirm, anything else to cancel."):
        sys.exit("Cancelled.")

    print("\n== Projects")
    pending = []
    for p in projects:
        path = p["path_with_namespace"]
        if api("DELETE", f"/projects/{q(path)}") is not None:
            print(f"  x {path} scheduled for deletion")
            pending.append(path)
    for path in pending:
        print(f"  {'gone' if wait_gone(f'/projects/{q(path)}') else 'still deleting'}: {path}")

    print("\n== Groups")
    pending = []
    for full_path in top_groups:
        if api("DELETE", f"/groups/{q(full_path)}") is not None:
            print(f"  x {full_path} scheduled for deletion")
            pending.append(full_path)
    for full_path in pending:
        print(f"  {'gone' if wait_gone(f'/groups/{q(full_path)}') else 'still deleting'}: {full_path}")

    print("\n== Users")
    pending = []
    for u in users:
        if api("DELETE", f"/users/{u['id']}?hard_delete=true") is not None:
            print(f"  x {u['username']}")
            pending.append(u["id"])
    for user_id in pending:
        wait_gone(f"/users/{user_id}", timeout=90)

    print("\nDone. Only root remains.")


if __name__ == "__main__":
    main()
