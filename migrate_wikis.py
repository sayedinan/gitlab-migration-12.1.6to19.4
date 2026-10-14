#!/usr/bin/env python3
"""
migrate_wikis.py - copy project wikis from the LEGACY GitLab to the NEW GitLab.

A project's wiki is its own git repository (<project>.wiki.git). The main
migration scripts never copy it, so this script does, with full page history
and authors. Nothing on legacy is changed.

For every project that has wiki pages on legacy it:
  1. mirror-clones the legacy wiki repo,
  2. pushes it to the same project path on the new instance (into whatever the
     new wiki's default branch is: legacy uses 'master', new uses 'main'),
  3. compares the page count on both sides.
Projects with no wiki pages are skipped. Projects that do not exist on the new
instance are reported (run Part A first).

DRY RUN by default. Add --apply to really copy.

Required env vars (hard abort if unset)
  GITLAB_LEGACY_URL    e.g. http://180.210.129.233:8090
  GITLAB_LEGACY_TOKEN  admin token on legacy (scope: api; read_repository is enough for cloning)
  GITLAB_NEW_URL       e.g. http://192.16.0.65
  GITLAB_NEW_TOKEN     admin token on new (scope: api)

Usage
  python3 migrate_wikis.py --project company/qa/test-framework   # dry run, one project
  python3 migrate_wikis.py                                       # dry run, all projects
  python3 migrate_wikis.py --apply --project company/qa/test-framework
  python3 migrate_wikis.py --apply                               # copy everything
  python3 migrate_wikis.py --verify                              # compare page counts, change nothing
  python3 migrate_wikis.py --apply --force                       # re-copy, OVERWRITING new wikis

Options
  --config config.yaml   use its project_allowlist (needs pyyaml); --project overrides it
  --force                copy even if the new wiki already has pages or was copied before.
                         This overwrites the new wiki, including edits made on the new instance.

Progress is kept in wiki_migration_state.json (current directory); wikis already
copied are skipped on re-run unless --force is given.
Run it on the new GitLab server (or any machine that can reach both instances and has git).
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

STATE_FILE = "wiki_migration_state.json"


def need(name):
    v = os.environ.get(name, "").strip()
    if not v:
        sys.exit(f"ABORT: env var {name} is not set")
    return v


LEGACY_URL = need("GITLAB_LEGACY_URL").rstrip("/")
LEGACY_TOKEN = need("GITLAB_LEGACY_TOKEN")
NEW_URL = need("GITLAB_NEW_URL").rstrip("/")
NEW_TOKEN = need("GITLAB_NEW_TOKEN")
SECRETS = [LEGACY_TOKEN, urllib.parse.quote(LEGACY_TOKEN, safe=""), NEW_TOKEN, urllib.parse.quote(NEW_TOKEN, safe="")]


def scrub(text):
    for s in SECRETS:
        if s:
            text = text.replace(s, "***")
    return text


class Api:
    def __init__(self, base, token):
        self.base = base
        self.token = token

    def call(self, method, path, data=None):
        """Returns (status, parsed_json_or_text, headers). HTTP errors are returned, not raised."""
        url = f"{self.base}/api/v4{path}"
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("PRIVATE-TOKEN", self.token)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                raw, status, headers = r.read(), r.status, r.headers
        except urllib.error.HTTPError as e:
            raw, status, headers = e.read(), e.code, e.headers
        try:
            return status, json.loads(raw), headers
        except ValueError:
            return status, raw.decode(errors="replace"), headers

    def paged(self, path):
        sep = "&" if "?" in path else "?"
        page = "1"
        while page:
            status, items, headers = self.call("GET", f"{path}{sep}per_page=100&page={page}")
            if status != 200:
                raise RuntimeError(f"GET {path} -> {status}")
            for it in items:
                yield it
            page = headers.get("X-Next-Page", "")


legacy = Api(LEGACY_URL, LEGACY_TOKEN)
new = Api(NEW_URL, NEW_TOKEN)


def wiki_pages(api, pid):
    """List of wiki pages, or None if the wiki is disabled / not available."""
    status, body, _ = api.call("GET", f"/projects/{pid}/wikis")
    if status == 200:
        return body
    if status in (403, 404):
        return None
    raise RuntimeError(f"wiki list for project {pid} -> {status}")


def wiki_git_url(base, token, project_path):
    u = urllib.parse.urlsplit(base)
    netloc = f"oauth2:{urllib.parse.quote(token, safe='')}@{u.netloc}"
    path = u.path.rstrip("/") + "/" + project_path + ".wiki.git"
    return urllib.parse.urlunsplit((u.scheme, netloc, path, "", ""))


def git(args, cwd=None):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    p = subprocess.run(["git"] + args, cwd=cwd, env=env, capture_output=True, text=True)
    return p.returncode, scrub((p.stdout or "") + (p.stderr or ""))


def new_default_branch(url):
    """Default branch of the new wiki repo, or None if it does not exist yet."""
    rc, out = git(["ls-remote", "--symref", url, "HEAD"])
    if rc != 0:
        return None
    for line in out.splitlines():
        if line.startswith("ref:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].startswith("refs/heads/"):
                return parts[1][len("refs/heads/"):]
    return None


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp, STATE_FILE)


def copy_wiki(path, nid, legacy_count, force_note=""):
    """Clone legacy wiki and push to new. Returns (ok, message)."""
    tmp = tempfile.mkdtemp(prefix="wikimig-")
    repo = os.path.join(tmp, "wiki.git")
    try:
        rc, out = git(["clone", "--mirror", wiki_git_url(LEGACY_URL, LEGACY_TOKEN, path), repo])
        if rc != 0:
            return False, f"clone from legacy failed: {out.strip()[-300:]}"

        rc, out = git(["symbolic-ref", "HEAD"], cwd=repo)
        src = out.strip() if rc == 0 and out.strip().startswith("refs/heads/") else "refs/heads/master"
        rc, _ = git(["rev-parse", "--verify", src], cwd=repo)
        if rc != 0:
            return False, f"legacy wiki has no branch {src}"

        new_url = wiki_git_url(NEW_URL, NEW_TOKEN, path)
        target = new_default_branch(new_url) or "main"
        rc, out = git(["push", "--force", new_url, f"{src}:refs/heads/{target}"], cwd=repo)

        if rc != 0:
            # the new wiki may not exist yet: create it with a placeholder page, then push over it
            status, body, _ = new.call("POST", f"/projects/{nid}/wikis", {
                "title": "migration-placeholder",
                "content": "placeholder, replaced by the migrated wiki",
                "format": "markdown",
            })
            if status not in (200, 201):
                return False, f"push failed ({out.strip()[-200:]}) and placeholder page failed ({status})"
            target = new_default_branch(new_url) or target
            rc, out = git(["push", "--force", new_url, f"{src}:refs/heads/{target}"], cwd=repo)
            if rc != 0:
                return False, f"push to new failed: {out.strip()[-300:]}"
        return True, f"pushed {src} -> {target}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def process(p, apply, force, verify, state):
    """Returns (status, message). status: ok, would, skip, missing, warn, error"""
    path = p["path_with_namespace"]
    lp = wiki_pages(legacy, p["id"])
    if not lp:
        return "skip", "no wiki pages on legacy"

    status, newp, _ = new.call("GET", "/projects/" + urllib.parse.quote(path, safe=""))
    if status != 200:
        return "missing", "project does not exist on new (run Part A first)"
    nid = newp["id"]
    np_ = wiki_pages(new, nid)

    if verify:
        n_new = len(np_) if np_ else 0
        if n_new == len(lp):
            return "ok", f"legacy={len(lp)} new={n_new}"
        return "error", f"MISMATCH legacy={len(lp)} new={n_new}"

    if not force:
        if path in state:
            return "skip", f"already copied ({state[path].get('new_pages', '?')} pages); --force to redo"
        if np_:
            return "skip", f"new wiki already has {len(np_)} page(s); --force to overwrite"

    if not apply:
        extra = "; wiki disabled on new, would enable it" if np_ is None else ""
        return "would", f"would copy {len(lp)} page(s){extra}"

    if np_ is None:
        status, _, _ = new.call("PUT", f"/projects/{nid}", {"wiki_access_level": "enabled"})
        if status not in (200, 201):
            return "error", f"wiki is disabled on new and could not be enabled ({status})"

    ok, msg = copy_wiki(path, nid, len(lp))
    if not ok:
        return "error", msg

    after = wiki_pages(new, nid) or []
    state[path] = {"legacy_pages": len(lp), "new_pages": len(after), "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    save_state(state)
    if len(after) != len(lp):
        return "warn", f"{msg}; page count differs: legacy={len(lp)} new={len(after)}"
    return "ok", f"{msg}; {len(after)} page(s)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="really copy (default is dry run)")
    ap.add_argument("--verify", action="store_true", help="compare page counts only, change nothing")
    ap.add_argument("--force", action="store_true", help="copy even if already done or the new wiki has pages (overwrites)")
    ap.add_argument("--project", action="append", help="project path, repeatable")
    ap.add_argument("--config", help="config.yaml; uses project_allowlist")
    args = ap.parse_args()

    allow = list(args.project or [])
    if not allow and args.config:
        try:
            import yaml
            with open(args.config) as f:
                allow = (yaml.safe_load(f) or {}).get("project_allowlist") or []
        except ImportError:
            sys.exit("ABORT: --config needs pyyaml (pip install pyyaml --break-system-packages)")

    mode = "VERIFY" if args.verify else ("APPLY" if args.apply else "DRY RUN")
    print(f"Mode: {mode}")
    print(f"Legacy: {LEGACY_URL}   New: {NEW_URL}\n")

    if allow:
        projects = []
        for path in allow:
            status, proj, _ = legacy.call("GET", "/projects/" + urllib.parse.quote(path, safe=""))
            if status != 200:
                print(f"{path}: not found on legacy ({status})")
                continue
            projects.append(proj)
    else:
        projects = list(legacy.paged("/projects?simple=true&order_by=path&sort=asc"))

    state = load_state()
    counts = {}
    for p in projects:
        path = p["path_with_namespace"]
        try:
            st, msg = process(p, args.apply, args.force, args.verify, state)
        except Exception as e:  # keep going on one bad project
            st, msg = "error", scrub(str(e))
        counts[st] = counts.get(st, 0) + 1
        if st != "skip" or len(projects) <= 20:
            print(f"[{st.upper():7}] {path}: {msg}")

    print("\nSummary: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if counts.get("error") or counts.get("warn"):
        sys.exit(1)
    if args.verify and counts.get("missing"):
        sys.exit(1)
    if mode == "DRY RUN":
        print("Dry run only. Re-run with --apply to copy.")


if __name__ == "__main__":
    main()
