#!/usr/bin/env python3
"""Create the seeded projects with a backdated git history, LFS files, a
submodule, wikis, personal-namespace projects and forks, then enrich them
through the API (issues, MRs, settings ...).

Usage:
  python3 create_projects.py                 # everything; existing projects are skipped
  python3 create_projects.py --steps=wiki,issues,mrs
        # run only these steps on projects that already exist
        # steps: wiki access meta issues mrs snippets releases settings forks
        # (issues/MRs are not idempotent: running them twice duplicates them)

Requires: create_users.py already run, `git` and `git-lfs` installed, HTTP
access from this machine to GITLAB_URL.
"""
import random
import re
import subprocess
import tempfile
import urllib.parse

from seed_extra import *
from seed_enrich import enrich, ENRICH_ORDER, warm_up_access, grant_access

VERBS = ["Add", "Fix", "Update", "Refactor", "Improve", "Remove", "Document",
         "Tidy up", "Handle", "Simplify"]
TAGS = [(0.25, "v0.1.0", "Initial preview"),
        (0.60, "v1.0.0", "First stable release"),
        (0.90, "v1.1.0", "Minor improvements")]
PARSED = urllib.parse.urlparse(GITLAB_URL)


# ------------------------------------------------------------------ git glue
def run(cmd, cwd, env=None):
    e = os.environ.copy()
    e["GIT_TERMINAL_PROMPT"] = "0"
    if env:
        e.update(env)
    r = subprocess.run(cmd, cwd=cwd, env=e, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {cmd[1]} failed: {r.stderr.replace(TOKEN, '***').strip()[:2000]}")
    return r.stdout


def remote_url(path, wiki=False):
    return f"{PARSED.scheme}://oauth2:{TOKEN}@{PARSED.netloc}/{path}{'.wiki' if wiki else ''}.git"


def ident_env(username, when):
    u = USER[username]
    return {"GIT_AUTHOR_NAME": u["name"], "GIT_AUTHOR_EMAIL": u["email"], "GIT_AUTHOR_DATE": when,
            "GIT_COMMITTER_NAME": u["name"], "GIT_COMMITTER_EMAIL": u["email"],
            "GIT_COMMITTER_DATE": when}


def commit(repo, username, when, message):
    run(["git", "add", "-A"], repo)
    run(["git", "commit", "-q", "--allow-empty", "-m", message], repo, ident_env(username, when))


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def write(repo, rel, content, mode=None):
    path = os.path.join(repo, rel)
    os.makedirs(os.path.dirname(path) or repo, exist_ok=True)
    mode = mode or ("wb" if isinstance(content, bytes) else "w")
    with open(path, mode) as f:
        f.write(content)


# ---------------------------------------------------------- content helpers
def snippet(path, n, msg):
    """Small appendable chunk that is valid for the file type, or None."""
    ext = path.rsplit(".", 1)[-1] if "." in path else ""
    if ext == "py":
        return f"\n\ndef helper_{n}():\n    \"\"\"{msg}\"\"\"\n    return {n}\n"
    if ext == "js":
        return f"\nfunction helper{n}() {{\n  // {msg}\n  return {n};\n}}\n"
    if ext == "sh":
        return f"\n# {msg}\necho \"step {n} done\"\n"
    if ext == "css":
        return f"\n.block-{n} {{\n  margin: {n % 8}px;\n}}\n"
    if ext == "html":
        return f"\n<!-- {msg} -->\n"
    if ext == "md":
        return f"\n- {msg}\n"
    if ext in ("yml", "yaml", "txt", "ini"):
        return f"\n# {msg}\n"
    return None  # e.g. json: leave untouched


def touch(repo, spec, rng, n, msg):
    editable = [f for f, _ in spec["files"] if snippet(f, 0, "")]
    base = rng.choice(editable)
    ext = base.rsplit(".", 1)[-1]
    if rng.random() < 0.2 and ext in ("py", "js", "sh", "css", "md"):
        name = f"{slug(rng.choice(spec['topics']))}_{n}.{ext}".replace("-", "_")
        rel = os.path.join(os.path.dirname(base), name)
        body = f"# {msg}\n" if ext == "md" else snippet(rel, n, msg).lstrip("\n")
        write(repo, rel, body)
    else:
        write(repo, base, snippet(base, n, msg), "a")


def readme_text(spec):
    name = spec["path"].split("/")[-1]
    return (f"# {name}\n\n{spec['desc']}\n\n## Links\n\n"
            f"- [Issues]({ABS}/{spec['path']}/issues)\n"
            f"- [Wiki]({ABS}/{spec['path']}/wikis/home)\n"
            f"- [Team docs]({ABS}/{PPATH['docs_wiki']})\n")


# ----------------------------------------------------------- code history
def build_history(spec, repo, rng):
    authors = spec["authors"]
    weights = list(range(len(authors), 0, -1))   # first author is the busiest
    n = spec["commits"]
    stamps = timeline(n, START, END, rng)
    state = {"pos": 0, "branches": 0, "unmerged": 0, "tags": set()}
    lfs = LFS.get(spec["path"])

    def nxt():
        t = stamps[min(state["pos"], n - 1)]
        state["pos"] += 1
        return fmt(t)

    def pick():
        return rng.choices(authors, weights)[0]

    def message():
        return f"{rng.choice(VERBS)} {rng.choice(spec['topics'])}"

    maintainer = authors[0]
    run(["git", "init", "-q", "-b", "master"], repo)

    # initial commits
    write(repo, "README.md", readme_text(spec))
    commit(repo, maintainer, nxt(), "Initial commit")
    write(repo, "LICENSE", f"MIT License\n\nCopyright (c) {COMPANY_NAME}\n")
    commit(repo, maintainer, nxt(), "Add LICENSE")
    for rel, content in template_files():
        write(repo, rel, content)
    commit(repo, maintainer, nxt(), "Add issue and merge request templates")

    if lfs:
        patterns, files = lfs
        run(["git", "lfs", "install", "--local"], repo)
        run(["git", "lfs", "track"] + patterns, repo)
        commit(repo, maintainer, nxt(), "Track binary assets with Git LFS")
        for rel, size in files:
            write(repo, rel, rng.randbytes(size))
        commit(repo, authors[-1], nxt(), "Add binary assets")

    for i, (fname, content) in enumerate(spec["files"]):
        write(repo, fname, content)
        commit(repo, authors[i % len(authors)], nxt(),
               "Expand README" if fname == "README.md" else f"Add {fname}")

    if spec["path"] in SUBMODULES:
        rel, target = SUBMODULES[spec["path"]]
        code, info = call("GET", f"/projects/{q(target)}/repository/commits/master")
        sha = info["id"] if code == 200 else "1" * 40
        write(repo, ".gitmodules",
              f"[submodule \"{rel}\"]\n\tpath = {rel}\n\turl = {ABS}/{target}.git\n")
        os.makedirs(os.path.join(repo, rel), exist_ok=True)   # empty dir = uninitialised submodule
        run(["git", "update-index", "--add", "--cacheinfo", f"160000,{sha},{rel}"], repo)
        commit(repo, maintainer, nxt(), f"Add {rel} as a submodule")

    # the rest of the history
    while state["pos"] < n:
        remaining = n - state["pos"]
        if lfs and rng.random() < 0.05:
            rel, size = rng.choice(lfs[1])
            write(repo, rel, rng.randbytes(size))
            commit(repo, pick(), nxt(), f"Update {rel}")
        elif remaining >= 5 and rng.random() < 0.25:
            kind = rng.choice(["feature", "bugfix"])
            topic = rng.choice(spec["topics"])
            state["branches"] += 1
            branch = f"{kind}/{slug(topic)}-{state['branches']}"
            who = pick()
            run(["git", "checkout", "-q", "-b", branch], repo)
            for _ in range(rng.randint(2, min(4, remaining - 1))):
                msg = message()
                touch(repo, spec, rng, state["pos"], msg)
                commit(repo, who, nxt(), msg)
            run(["git", "checkout", "-q", "master"], repo)
            if state["unmerged"] < 2 and rng.random() < 0.15:
                state["unmerged"] += 1            # leave as an open branch
            else:
                when = nxt()
                run(["git", "merge", "--no-ff", "-q", "-m",
                     f"Merge branch '{branch}' into 'master'\n\n{kind.capitalize()}: {topic}",
                     branch], repo, ident_env(maintainer, when))
                run(["git", "branch", "-q", "-d", branch], repo)
        else:
            msg = message()
            touch(repo, spec, rng, state["pos"], msg)
            commit(repo, pick(), nxt(), msg)

        for frac, tag, title in TAGS:
            if tag not in state["tags"] and state["pos"] >= frac * n:
                state["tags"].add(tag)
                when = fmt(stamps[min(state["pos"], n - 1)])
                run(["git", "tag", "-a", tag, "-m", f"{tag}: {title}"], repo,
                    ident_env(maintainer, when))


def push_code(spec, rng):
    with tempfile.TemporaryDirectory() as tmp:
        build_history(spec, tmp, rng)
        run(["git", "remote", "add", "origin", remote_url(spec["path"])], tmp)
        run(["git", "push", "-q", "origin", "--all"], tmp)
        run(["git", "push", "-q", "origin", "--tags"], tmp)
        return run(["git", "rev-list", "--count", "--all"], tmp).strip()


# ------------------------------------------------------------------- wiki
def wiki_page(title, spec):
    name = spec["path"].split("/")[-1]
    if title == "Home":
        return (f"# {name}\n\n{spec['desc']}\n\n![architecture](uploads/architecture.png)\n\n"
                f"- [Setup Guide](Setup-Guide)\n- [Deployment](Deployment)\n"
                f"- [Coding Standards](Coding-Standards)\n- [Runbook](Runbook)\n"
                f"- [Issues]({ABS}/{spec['path']}/issues)\n")
    if title == "Setup-Guide":
        return f"# Setup Guide\n\n1. Clone the repository\n2. Install dependencies\n3. Run the tests for {name}\n"
    if title == "Deployment":
        return "# Deployment\n\nDeployments run through the pipeline.\n\n```\n./deploy/deploy.sh staging\n```\n"
    if title == "Coding-Standards":
        return "# Coding Standards\n\n- Small, focused commits\n- Review before merge\n- Tests for every fix\n"
    return f"# Runbook\n\n## Restarting {name}\n\nSee [Deployment](Deployment).\n"


def make_wiki(spec, project, rng):
    """Seed the wiki through git so its history is backdated too. The API creates
    the first page (which initialises the wiki repo), then the history is
    replaced by a backdated one and force-pushed."""
    authors = spec["authors"]
    wiki = None
    for _ in range(6):     # newly created projects can briefly 404 on the API
        wiki = api("POST", f"/projects/{project['id']}/wikis",
                   {"title": "Home", "content": "# Home\n\nInitialising wiki.", "format": "markdown"},
                   sudo=authors[0], ok_codes=(400, 404))
        if wiki is not None:
            break
        time.sleep(3)
    with tempfile.TemporaryDirectory() as tmp:
        run(["git", "clone", "-q", remote_url(spec["path"], wiki=True), tmp], tempfile.gettempdir())
        run(["git", "checkout", "-q", "--orphan", "rewritten"], tmp)
        run(["git", "rm", "-rfq", "."], tmp)
        pages = ["Home", "Setup-Guide", "Deployment", "Coding-Standards", "Runbook"]
        stamps = timeline(rng.randint(9, 14), START, END, rng)
        for i, when in enumerate(stamps):
            who = rng.choice(authors)
            if i < len(pages):
                title = pages[i]
                write(tmp, f"{title}.md", wiki_page(title, spec))
                if title == "Home":
                    write(tmp, "uploads/architecture.png", tiny_png())
                msg = f"Create {title}"
            else:
                title = rng.choice(pages)
                write(tmp, f"{title}.md", f"\n## Update {i}\n\n{rng.choice(spec['topics']).capitalize()} notes.\n", "a")
                msg = f"Update {title}"
            commit(tmp, who, fmt(when), msg)
        run(["git", "push", "-q", "--force", "origin", "rewritten:master"], tmp)


# ------------------------------------------------- creation orchestration
def wanted(only, step):
    return only is None or step in only


def create_group_project(spec):
    group_path, name = spec["path"].rsplit("/", 1)
    group = find_group(group_path)
    if not group:
        print(f"  ! group {group_path} not found - run create_users.py first")
        return None
    return api("POST", "/projects", {
        "name": name, "path": name, "namespace_id": group["id"], "description": spec["desc"],
        "visibility": spec["vis"], "initialize_with_readme": False})


def create_personal_project(spec):
    owner = find_user(spec["authors"][0])
    if not owner:
        print(f"  ! user {spec['authors'][0]} not found - run create_users.py first")
        return None
    name = spec["path"].split("/")[-1]
    return api("POST", f"/projects/user/{owner['id']}", {
        "name": name, "path": name, "description": spec["desc"], "visibility": spec["vis"]})


def process(spec, create_fn, only, light=False):
    full = spec["path"]
    print(f"== {full}")
    project = find_project(full)
    rng = random.Random(full)
    if project and only is None:
        print("  = already exists, skipping (use --steps=... to run single steps)")
        return
    if not project:
        project = create_fn(spec)
        if not project:
            return
        for _ in range(10):    # wait for the new project to be fully readable via the API
            if api("GET", f"/projects/{project['id']}", ok_codes=(404,)):
                break
            time.sleep(2)
        if not light:
            grant_access(project["id"], spec)   # must happen before warm-up, or shares aren't live yet
            warm_up_access(project["id"], spec)
        try:
            count = push_code(spec, rng)
            print(f"  + code pushed ({count} commits, {START} to {END})")
            if not light:
                make_wiki(spec, project, random.Random(full + "-wiki"))
                print("  + wiki pushed")
        except RuntimeError as e:
            print(f"  ! {e}")
    elif "wiki" in (only or ()) and not light:
        try:
            make_wiki(spec, project, random.Random(full + "-wiki"))
            print("  + wiki rebuilt")
        except RuntimeError as e:
            print(f"  ! {e}")
    enrich(project, spec, only, light)


def make_fork(f):
    full = fork_path(f)
    print(f"== fork {full}")
    if find_project(full):
        print("  = already exists, skipping")
        return
    src = find_project(f["source"])
    if not src:
        print("  ! source project missing")
        return
    fork = api("POST", f"/projects/{src['id']}/fork", {"namespace": f["namespace"]}, sudo=f["user"])
    if not fork:
        return
    for _ in range(40):                       # forking is asynchronous
        info = api("GET", f"/projects/{fork['id']}")
        if info and info.get("import_status") in ("finished", "none"):
            break
        time.sleep(2)
    commit_ok = api("POST", f"/projects/{fork['id']}/repository/commits", {
        "branch": "fix/typo-in-docs", "start_branch": "master",
        "commit_message": "Fix typos in docs",
        "actions": [{"action": "create", "file_path": "docs/fork-notes.md",
                     "content": "# Notes\n\nTypo fixes from a fork.\n"}]}, sudo=f["user"])
    if commit_ok:
        mr = api("POST", f"/projects/{fork['id']}/merge_requests", {
            "source_branch": "fix/typo-in-docs", "target_branch": "master",
            "target_project_id": src["id"], "title": "Fix typos in docs"}, sudo=f["user"])
        if mr:
            print("  + fork created with a merge request to upstream")


def main():
    only = None
    for arg in sys.argv[1:]:
        if arg.startswith("--steps="):
            only = set(arg.split("=", 1)[1].split(","))
            unknown = only - set(ENRICH_ORDER) - {"wiki", "forks"}
            if unknown:
                sys.exit(f"Unknown steps: {sorted(unknown)}")

    # projects that are submodule targets must exist before the ones using them
    ordered = sorted(PROJECTS, key=lambda s: 1 if s["path"] in SUBMODULES else 0)
    for spec in ordered:
        process(spec, create_group_project, only)
    for spec in PERSONAL_SPECS:
        process(spec, create_personal_project, only, light=True)
    if wanted(only, "forks"):
        for f in FORKS:
            make_fork(f)
    print("\nDone.")


if __name__ == "__main__":
    main()
