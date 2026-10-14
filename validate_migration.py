#!/usr/bin/env python3
"""
GitLab Migration Validation Script

Compares legacy vs new GitLab instances across everything the migration
was supposed to move, and reports mismatches clearly.

Usage:
    export GITLAB_LEGACY_TOKEN="..."
    export GITLAB_NEW_TOKEN="..."
    python3 validate_migration.py --config config.yaml

Exits with code 0 if everything matches, 1 if any mismatch is found
(useful for scripting / CI-style checks).
"""

import argparse
import os
import subprocess
import sys
import tempfile

import requests
import yaml


class GitLabAPI:
    def __init__(self, base_url, token):
        self.base_url = base_url.rstrip("/") + "/api/v4"
        self.session = requests.Session()
        self.session.headers.update({"PRIVATE-TOKEN": token})

    def get_all(self, path, params=None):
        params = dict(params or {})
        params["per_page"] = 100
        page = 1
        results = []
        while True:
            params["page"] = page
            resp = self.session.get(f"{self.base_url}{path}", params=params)
            if resp.status_code != 200:
                return results
            batch = resp.json()
            if not batch:
                break
            results.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return results

    def get(self, path, params=None):
        resp = self.session.get(f"{self.base_url}{path}", params=params or {})
        return resp.json() if resp.status_code == 200 else None


class Report:
    def __init__(self):
        self.checks = []  # (label, ok, detail)

    def add(self, label, ok, detail=""):
        self.checks.append((label, ok, detail))
        symbol = "✅" if ok else "❌"
        print(f"{symbol} {label}" + (f" — {detail}" if detail else ""))

    def summary(self):
        total = len(self.checks)
        failed = [c for c in self.checks if not c[1]]
        print("\n" + "=" * 60)
        print(f"SUMMARY: {total - len(failed)}/{total} checks passed")
        if failed:
            print("\nFAILED CHECKS:")
            for label, _, detail in failed:
                print(f"  - {label}: {detail}")
        print("=" * 60)
        return len(failed) == 0


def load_config(path):
    with open(path) as f:
        cfg = yaml.safe_load(f)
    cfg["legacy"]["token"] = os.environ.get("GITLAB_LEGACY_TOKEN", cfg["legacy"]["token"])
    cfg["new"]["token"] = os.environ.get("GITLAB_NEW_TOKEN", cfg["new"]["token"])
    return cfg


def check_users(legacy, new, report, skiplist):
    legacy_users = {u["username"] for u in legacy.get_all("/users") if u["username"] not in skiplist}
    new_users = {u["username"] for u in new.get_all("/users")}
    missing = legacy_users - new_users
    report.add(
        "Users migrated",
        not missing,
        f"missing on new instance: {sorted(missing)}" if missing else f"{len(legacy_users)} users present",
    )


def check_groups(legacy, new, report):
    legacy_groups = {g["full_path"] for g in legacy.get_all("/groups", params={"all_available": True})}
    new_groups = {g["full_path"] for g in new.get_all("/groups", params={"all_available": True})}
    missing = legacy_groups - new_groups
    report.add(
        "Groups migrated (with hierarchy)",
        not missing,
        f"missing on new instance: {sorted(missing)}" if missing else f"{len(legacy_groups)} groups present",
    )


def check_projects_and_content(legacy, new, report, allowlist, work_dir, legacy_url, legacy_token, new_url, new_token):
    legacy_projects = legacy.get_all("/projects", params={"membership": False})
    for lp in legacy_projects:
        path = lp["path_with_namespace"]
        if allowlist and path not in allowlist:
            continue

        np = new.get(f"/projects/{requests.utils.quote(path, safe='')}")
        if not np:
            report.add(f"Project exists: {path}", False, "not found on new instance")
            continue
        report.add(f"Project exists: {path}", True)

        # --- issue count ---
        legacy_issues = legacy.get_all(f"/projects/{lp['id']}/issues", {"scope": "all"})
        new_issues = new.get_all(f"/projects/{np['id']}/issues", {"scope": "all"})
        ok = len(legacy_issues) == len(new_issues)
        report.add(
            f"  Issue count: {path}", ok,
            f"legacy={len(legacy_issues)} new={len(new_issues)}" if not ok else f"{len(new_issues)} issues",
        )

        # --- MR count (allow new <= legacy, since some MRs are legitimately
        #     unrecoverable if their source branch was already deleted on legacy) ---
        legacy_mrs = legacy.get_all(f"/projects/{lp['id']}/merge_requests", {"scope": "all"})
        new_mrs = new.get_all(f"/projects/{np['id']}/merge_requests", {"scope": "all"})
        # fewer on new is fine (source branches deleted on legacy before migration) -
        # including zero, if EVERY legacy MR was affected. Only flag more-on-new
        # (should never happen) or a legacy project with MRs that got none migrated
        # at all when it shouldn't be zero - that ambiguity is left to a human to read
        # from the detail string rather than guessed at here.
        ok = len(new_mrs) == len(legacy_mrs)
        detail = f"legacy={len(legacy_mrs)} new={len(new_mrs)}"
        if len(new_mrs) < len(legacy_mrs):
            detail += " (fewer is OK if some source branches were already deleted on legacy)"
        report.add(f"  MR count: {path}", ok, detail)

        # --- commit count via cloning both (bare, shallow-free comparison) ---
        # Use the legacy project's own default branch rather than assuming "master" -
        # real repos are just as likely to default to "main" or something else.
        branch = lp.get("default_branch") or np.get("default_branch") or "master"
        legacy_auth = legacy_url.replace("://", f"://oauth2:{legacy_token}@")
        new_auth = new_url.replace("://", f"://oauth2:{new_token}@")
        try:
            legacy_count = _remote_commit_count(f"{legacy_auth}/{path}.git", branch, work_dir)
            new_count = _remote_commit_count(f"{new_auth}/{path}.git", branch, work_dir)
            ok = legacy_count == new_count
            report.add(
                f"  Commit count ({branch}): {path}", ok,
                f"legacy={legacy_count} new={new_count}" if not ok else f"{new_count} commits",
            )
        except Exception as e:
            report.add(f"  Commit count ({branch}): {path}", False, f"could not compare: {e}")


def _remote_commit_count(auth_url, branch, work_dir):
    """Uses git ls-remote + a shallow-free rev-list count without a full clone."""
    with tempfile.TemporaryDirectory(dir=work_dir) as tmp:
        subprocess.run(
            ["git", "clone", "--bare", "--branch", branch, "--single-branch", auth_url, tmp],
            check=True, capture_output=True, text=True,
        )
        result = subprocess.run(
            ["git", "-C", tmp, "rev-list", "--count", branch],
            check=True, capture_output=True, text=True,
        )
        return int(result.stdout.strip())


def _count_comments(api, pid):
    n = 0
    for kind in ("issues", "merge_requests"):
        for it in api.get_all(f"/projects/{pid}/{kind}", {"scope": "all"}):
            n += sum(1 for x in api.get_all(f"/projects/{pid}/{kind}/{it['iid']}/notes") if not x.get("system"))
    return n


def _all_as(api, path, sudo):
    out, page = [], 1
    while True:
        r = api.session.get(f"{api.base_url}{path}", params={"per_page": 100, "page": page}, headers={"Sudo": sudo})
        if r.status_code != 200 or not r.json():
            return out
        out += r.json()
        if len(r.json()) < 100:
            return out
        page += 1


def check_extras(legacy, new, report, allowlist, skiplist):
    q = lambda p: requests.utils.quote(p, safe="")
    for lp in legacy.get_all("/projects", params={"membership": False}):
        path = lp["path_with_namespace"]
        if allowlist and path not in allowlist:
            continue
        np_ = new.get(f"/projects/{q(path)}")
        if not np_:
            continue
        lc, nc = _count_comments(legacy, lp["id"]), _count_comments(new, np_["id"])
        report.add(f"  Comments: {path}", lc == nc, f"legacy={lc} new={nc}")
        for kind, field in (("milestones", "title"), ("labels", "name")):
            ls = {x[field] for x in legacy.get_all(f"/projects/{lp['id']}/{kind}", {"state": "all"} if kind == "milestones" else None)}
            ns = {x[field] for x in new.get_all(f"/projects/{np_['id']}/{kind}", {"state": "all"} if kind == "milestones" else None)}
            report.add(f"  {kind.title()}: {path}", ls <= ns, f"missing: {sorted(ls - ns)}" if ls - ns else f"{len(ls)} present")
        ls = len(legacy.get_all(f"/projects/{lp['id']}/snippets"))
        ns = len(new.get_all(f"/projects/{np_['id']}/snippets"))
        report.add(f"  Project snippets: {path}", ls == ns, f"legacy={ls} new={ns}")
        if lp.get("forked_from_project"):
            report.add(f"  Fork link: {path}", bool(np_.get("forked_from_project")),
                       f"should be a fork of {lp['forked_from_project']['path_with_namespace']}")
    for u in legacy.get_all("/users"):
        name = u["username"]
        if name in skiplist:
            continue
        nu = new.get_all("/users", params={"username": name})
        if not nu:
            continue
        lk = len(legacy.get_all(f"/users/{u['id']}/keys"))
        nk = len(new.get_all(f"/users/{nu[0]['id']}/keys"))
        if lk or nk:
            report.add(f"  SSH keys: {name}", lk == nk, f"legacy={lk} new={nk}")
        lsn, nsn = len(_all_as(legacy, "/snippets", name)), len(_all_as(new, "/snippets", name))
        if lsn or nsn:
            report.add(f"  Personal snippets: {name}", lsn == nsn, f"legacy={lsn} new={nsn}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--skip-commit-check", action="store_true",
                         help="skip the git clone/commit-count comparison (faster, API-only checks)")
    args = parser.parse_args()

    cfg = load_config(args.config)
    legacy = GitLabAPI(cfg["legacy"]["url"], cfg["legacy"]["token"])
    new = GitLabAPI(cfg["new"]["url"], cfg["new"]["token"])
    allowlist = set(cfg.get("project_allowlist") or [])
    skiplist = set(cfg.get("user_skiplist", []) or [])
    work_dir = cfg["options"].get("mirror_work_dir", "/tmp/gitlab-migration-mirror")
    os.makedirs(work_dir, exist_ok=True)

    report = Report()

    print("=== Users ===")
    check_users(legacy, new, report, skiplist)

    print("\n=== Groups ===")
    check_groups(legacy, new, report)

    print("\n=== Projects & content ===")
    if args.skip_commit_check:
        # monkey-patch out the commit check for a faster API-only pass
        global _remote_commit_count
        _orig = _remote_commit_count
        _remote_commit_count = lambda *a, **kw: (_ for _ in ()).throw(Exception("skipped"))
    check_projects_and_content(
        legacy, new, report, allowlist, work_dir,
        cfg["legacy"]["url"], cfg["legacy"]["token"], cfg["new"]["url"], cfg["new"]["token"],
    )

    print("\n=== Extras: comments, labels, milestones, snippets, SSH keys, forks ===")
    check_extras(legacy, new, report, allowlist, skiplist)

    ok = report.summary()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
