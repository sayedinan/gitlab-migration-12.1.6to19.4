#!/usr/bin/env python3
"""Delete every seeded project completely: forks first, then personal-namespace
projects, then the team projects (with their wikis, issues, MRs, snippets ...).
Groups and users are left alone. Only projects listed in the seed files are touched.

Usage: python3 delete_projects.py [--yes]
"""
from seed_extra import *


def main():
    targets = ([fork_path(f) for f in FORKS]
               + [s["path"] for s in PERSONAL_SPECS]
               + [s["path"] for s in PROJECTS])
    if not confirm("This will delete these projects (if they exist):\n  " + "\n  ".join(targets)):
        sys.exit("Cancelled.")

    waiting = []
    for path in targets:
        project = find_project(path)
        if not project:
            print(f"  - {path} not found")
            continue
        if api("DELETE", f"/projects/{project['id']}") is not None:
            print(f"  x {path} scheduled for deletion")
            waiting.append(path)

    for path in waiting:
        ok = wait_gone(f"/projects/{q(path)}")
        print(f"  {'gone' if ok else 'still deleting'}: {path}")

    print("\nDone. Paths are free again, so create_projects.py can be re-run.")


if __name__ == "__main__":
    main()
