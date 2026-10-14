#!/usr/bin/env python3
"""
fix_links.py - rewrite legacy GitLab URLs inside repo files on the NEW GitLab.

What it does
  For every project on the new instance, looks at doc-type files on the default
  branch (README, *.md, *.markdown, *.txt, *.rst, *.adoc, *.html, .gitmodules),
  replaces legacy base URLs with the new base URL, and makes ONE new commit per
  project. History is never rewritten.

Safety
  DRY RUN by default: prints what would change, commits nothing.
  Add --apply to actually commit.
  Run it AFTER validate_migration.py passes (the extra commit changes commit counts).

Required env vars (hard abort if unset)
  GITLAB_NEW_URL     e.g. http://192.16.0.65
  GITLAB_NEW_TOKEN   admin/root personal access token on the NEW instance (scope: api)
  LEGACY_BASES       comma-separated legacy base URLs, e.g.
                     http://180.210.129.233:8090,http://180.210.129.233

Usage
  export GITLAB_NEW_URL=http://192.16.0.65
  export GITLAB_NEW_TOKEN=xxxx
  export LEGACY_BASES="http://180.210.129.233:8090,http://180.210.129.233"
  python3 fix_links.py                      # dry run, all projects
  python3 fix_links.py --project company/qa/test-framework   # dry run, one project
  python3 fix_links.py --apply              # commit for real

Wiki pages (run after migrate_wikis.py; works any time, safe to repeat)
  python3 fix_links.py --wikis-only                 # dry run, wiki pages only
  python3 fix_links.py --wikis-only --apply         # fix wiki pages only
  python3 fix_links.py --wikis --apply              # repo files AND wiki pages
  Wiki pages are changed through the wiki API, so this makes one commit per
  changed page (not one per project). Page titles and slugs are not touched.

Notes
  - Only http(s) base URLs are rewritten. ssh:// / git@ clone URLs are NOT touched
    (the new instance's git-over-SSH host/port may differ; handle those separately).
  - If the default branch is protected and the token's user may not push,
    the commit is rejected and reported; allow push temporarily and re-run.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


def need(name):
    v = os.environ.get(name, "").strip()
    if not v:
        sys.exit(f"ABORT: env var {name} is not set")
    return v


NEW_URL = need("GITLAB_NEW_URL").rstrip("/")
TOKEN = need("GITLAB_NEW_TOKEN")
OLD_BASES = sorted(
    [b.strip().rstrip("/") for b in need("LEGACY_BASES").split(",") if b.strip()],
    key=len,
    reverse=True,  # longest first so ":8090" variant is replaced before the bare host
)

TEXT_EXTS = (".md", ".markdown", ".txt", ".rst", ".adoc", ".html", ".htm")
SPECIAL_NAMES = (".gitmodules",)
MAX_BYTES = 1_000_000
COMMIT_MESSAGE = "Update links from legacy GitLab to new GitLab"


def api(method, path, data=None, raw=False):
    url = f"{NEW_URL}/api/v4{path}"
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("PRIVATE-TOKEN", TOKEN)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=120) as r:
        content = r.read()
        headers = r.headers
    return (content if raw else json.loads(content)), headers


def paged(path, params=""):
    page = "1"
    sep = "&" if "?" in path else "?"
    while page:
        items, headers = api("GET", f"{path}{sep}per_page=100&page={page}{params}")
        for it in items:
            yield it
        page = headers.get("X-Next-Page", "")


def rewrite(text):
    count = 0
    for old in OLD_BASES:
        count += text.count(old)
        text = text.replace(old, NEW_URL)
    return text, count


def is_candidate(path):
    name = path.rsplit("/", 1)[-1]
    return name in SPECIAL_NAMES or name.lower().endswith(TEXT_EXTS)


def process_project(p, apply):
    pid = p["id"]
    path = p["path_with_namespace"]
    branch = p.get("default_branch")
    if not branch:
        print(f"  {path}: no default branch (empty repo), skipped")
        return 0, 0

    try:
        tree = list(paged(f"/projects/{pid}/repository/tree?recursive=true&ref={urllib.parse.quote(branch)}"))
    except urllib.error.HTTPError as e:
        print(f"  {path}: cannot read tree ({e.code}), skipped")
        return 0, 0

    actions = []
    total_links = 0
    for entry in tree:
        if entry.get("type") != "blob" or not is_candidate(entry["path"]):
            continue
        fp = urllib.parse.quote(entry["path"], safe="")
        try:
            blob, _ = api("GET", f"/projects/{pid}/repository/files/{fp}/raw?ref={urllib.parse.quote(branch)}", raw=True)
        except urllib.error.HTTPError as e:
            print(f"  {path}: cannot read {entry['path']} ({e.code}), skipped")
            continue
        if len(blob) > MAX_BYTES:
            continue
        try:
            text = blob.decode("utf-8")
        except UnicodeDecodeError:
            continue
        new_text, n = rewrite(text)
        if n:
            actions.append({"action": "update", "file_path": entry["path"], "content": new_text})
            total_links += n
            print(f"  {path}: {entry['path']} -> {n} link(s)")

    if not actions:
        return 0, 0

    if apply:
        try:
            api("POST", f"/projects/{pid}/repository/commits", {
                "branch": branch,
                "commit_message": COMMIT_MESSAGE,
                "actions": actions,
            })
            print(f"  {path}: COMMITTED {len(actions)} file(s) on '{branch}'")
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")[:300]
            print(f"  {path}: COMMIT FAILED ({e.code}) {msg}")
            return 0, 1
    return len(actions), 0


def process_wiki(p, apply):
    """Rewrite legacy URLs in wiki pages via the wiki API. Returns (pages_changed, failures)."""
    pid = p["id"]
    path = p["path_with_namespace"]
    try:
        pages, _ = api("GET", f"/projects/{pid}/wikis?with_content=1")
    except urllib.error.HTTPError as e:
        if e.code not in (403, 404):
            print(f"  {path}: cannot list wiki pages ({e.code}), skipped")
        return 0, 0

    changed = 0
    fails = 0
    for page in pages:
        content = page.get("content")
        if not content:
            continue
        new_content, n = rewrite(content)
        if not n:
            continue
        slug = page["slug"]
        print(f"  {path} [wiki]: {slug} -> {n} link(s)")
        if apply:
            try:
                # only 'content' is sent, so title, slug and format stay as they are
                api("PUT", f"/projects/{pid}/wikis/{urllib.parse.quote(slug, safe='')}", {"content": new_content})
                changed += 1
            except urllib.error.HTTPError as e:
                msg = e.read().decode(errors="replace")[:300]
                print(f"  {path} [wiki]: UPDATE FAILED for {slug} ({e.code}) {msg}")
                fails += 1
        else:
            changed += 1
    return changed, fails


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually commit (default is dry run)")
    ap.add_argument("--project", help="only this project path, e.g. company/qa/test-framework")
    ap.add_argument("--wikis", action="store_true", help="also rewrite wiki pages")
    ap.add_argument("--wikis-only", action="store_true", help="rewrite wiki pages only, skip repository files")
    args = ap.parse_args()

    print(f"Mode: {'APPLY' if args.apply else 'DRY RUN'}")
    print(f"Replacing {OLD_BASES} -> {NEW_URL}\n")

    if args.project:
        proj, _ = api("GET", f"/projects/{urllib.parse.quote(args.project, safe='')}")
        projects = [proj]
    else:
        projects = list(paged("/projects?simple=true&order_by=path&sort=asc"))

    do_repo = not args.wikis_only
    do_wiki = args.wikis or args.wikis_only

    files = pages = fails = 0
    for p in projects:
        if do_repo:
            f, x = process_project(p, args.apply)
            files += f
            fails += x
        if do_wiki:
            w, x = process_wiki(p, args.apply)
            pages += w
            fails += x

    print(f"\nProjects scanned: {len(projects)}")
    if do_repo:
        print(f"Files {'committed' if args.apply else 'that would change'}: {files}")
    if do_wiki:
        print(f"Wiki pages {'updated' if args.apply else 'that would change'}: {pages}")
    if fails:
        print(f"Projects with failed commits: {fails}")
        sys.exit(1)
    if not args.apply:
        print("Dry run only. Re-run with --apply to commit.")


if __name__ == "__main__":
    main()
