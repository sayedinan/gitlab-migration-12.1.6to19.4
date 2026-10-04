#!/usr/bin/env python3
"""Delete the seeded groups and users completely.

- Deleting a group also deletes every subgroup and project inside it.
- Users are hard-deleted (their issues, MRs, comments and snippets go too).
- Only names listed in seed_common.py are touched; root is never included.
- Aborts if a seeded group contains projects that are NOT in the manifest,
  unless you pass --force.

Usage: python3 delete_users.py [--yes] [--force]
"""
from seed_common import *


def main():
    manifest_projects = {p["path"] for p in PROJECTS}
    top_groups = sorted({g[0].split("/")[0] for g in GROUPS})

    # safety check for foreign projects inside the groups we are about to delete
    for top in top_groups:
        group = find_group(top)
        if not group:
            continue
        projects = api("GET", f"/groups/{group['id']}/projects?include_subgroups=true&per_page=100") or []
        extra = [p["path_with_namespace"] for p in projects
                 if p["path_with_namespace"] not in manifest_projects]
        if extra and "--force" not in sys.argv:
            print(f"Group '{top}' contains projects that are not part of the seed:")
            for e in extra:
                print("   ", e)
            sys.exit("Aborting. Re-run with --force if you really want them deleted too.")

    if not confirm(f"This will delete groups {top_groups} (with all subgroups and projects) "
                   f"and {len(USERS)} seeded users."):
        sys.exit("Cancelled.")

    print("== Groups")
    for top in top_groups:
        group = find_group(top)
        if not group:
            print(f"  - {top} not found")
            continue
        api("DELETE", f"/groups/{group['id']}")
        print(f"  x {top} scheduled for deletion, waiting...")
        print("    gone" if wait_gone(f"/groups/{q(top)}") else "    still deleting (check later)")

    print("== Users")
    pending = []
    for username, *_ in USERS:
        user = find_user(username)
        if not user:
            print(f"  - {username} not found")
            continue
        if api("DELETE", f"/users/{user['id']}?hard_delete=true") is not None:
            print(f"  x {username}")
            pending.append(user["id"])
    for user_id in pending:
        wait_gone(f"/users/{user_id}", timeout=90)

    print("\nDone.")


if __name__ == "__main__":
    main()
