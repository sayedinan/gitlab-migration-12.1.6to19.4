#!/usr/bin/env python3
"""Extra seed data and helpers for the realism stages.

Imports seed_common and patches PROJECTS in place, so every script that
imports this module sees the same data.

Optional environment variables:
  HISTORY_START   first activity date, default 2024-01-08
  HISTORY_END     last activity date,  default today minus 14 days
  COMMIT_TZ       timezone offset written into commits, default +0000
"""
import datetime as dt
import struct
import subprocess
import tempfile
import uuid
import zlib

from seed_common import *

ABS = GITLAB_URL   # absolute links to the old server are placed in content on purpose
START = dt.date.fromisoformat(os.environ.get("HISTORY_START", "2024-01-08"))
END = (dt.date.fromisoformat(os.environ["HISTORY_END"]) if "HISTORY_END" in os.environ
       else dt.date.today() - dt.timedelta(days=14))
TZ = os.environ.get("COMMIT_TZ", "+0000")

# ------------------------------------------------------------------ data
# Git LFS: project path -> (patterns, [(file, size_in_bytes)])
LFS = {
    PPATH["web_portal"]: (
        ["*.png", "*.pdf"],
        [("assets/logo.png", 150_000), ("assets/banner.png", 260_000), ("docs/brand-guide.pdf", 400_000)]),
    PPATH["ui_assets"]: (
        ["*.png", "*.psd", "*.pdf"],
        [("icons/icon-set.png", 220_000), ("logos/logo-master.psd", 600_000), ("brand/brand-book.pdf", 500_000)]),
}

# Submodule: project path -> (path inside repo, project it points to)
SUBMODULES = {
    PPATH["web_portal"]: ("vendor/ui-assets", PPATH["ui_assets"]),
}

# People who can open issues / comment on a project besides its authors
# (all of them get access through group membership, a group share or a direct membership)
REPORTERS = {
    PPATH["order_api"]:     [U["qa_lead"], U["qa_dev"], U["eng_manager"], U["pm"]],
    PPATH["web_portal"]:    [U["qa_lead"], U["qa_dev"], U["designer"], U["pm"]],
    PPATH["infra_scripts"]: [U["eng_manager"], U["pm"], U["cto"]],
    PPATH["test_suite"]:    [U["dev_lead"], U["pm"]],
    PPATH["ui_assets"]:     [U["frontend_lead"], U["pm"], U["cto"]],
    PPATH["docs_wiki"]:     [U["cto"], U["eng_manager"]],
}

# Projects shared with another group: project -> [(group path, access level)]
SHARES = {
    PPATH["order_api"]:  [("company/qa", REPORTER)],
    PPATH["web_portal"]: [("company/qa", REPORTER), ("company/design", REPORTER)],
}

# Direct project members: project -> [(username, access level, expires_at)]
MEMBERS = {
    PPATH["order_api"]: [(U["contractor"], REPORTER, "2026-12-31")],
}

# Projects in users' personal namespaces
PERSONAL_SPECS = [
    dict(path=f"{U['dev_lead']}/scratch-notes", desc="Personal scratch notes and experiments",
         vis="private", commits=18, authors=[U["dev_lead"]],
         topics=["notes", "experiments", "snippets", "todo list"],
         files=[("notes/ideas.md", "# Ideas\n\n- caching layer\n- retry policy\n"),
                ("scripts/try.py", "print('experiment')\n")]),
    dict(path=f"{U['frontend_lead']}/css-experiments", desc="CSS and layout experiments",
         vis="private", commits=22, authors=[U["frontend_lead"]],
         topics=["grid layout", "animations", "dark mode", "flexbox demo"],
         files=[("demo/index.html", "<!doctype html>\n<html><body><h1>Demo</h1></body></html>\n"),
                ("demo/style.css", "h1 {\n  color: #1F6FEB;\n}\n")]),
    dict(path=f"{U['devops']}/home-lab-scripts", desc="Home lab automation",
         vis="private", commits=16, authors=[U["devops"]],
         topics=["backup job", "network check", "docker helpers", "cron tasks"],
         files=[("net_check.sh", "#!/bin/bash\nping -c 1 8.8.8.8\n"),
                ("README.md", "# Home lab\n\nPersonal scripts.\n")]),
    dict(path=f"{U['ex_employee']}/old-prototype", desc="Prototype left behind by a former employee",
         vis="private", commits=14, authors=[U["ex_employee"]],
         topics=["prototype", "cleanup", "mock data"],
         files=[("proto/app.py", "def main():\n    print('prototype')\n"),
                ("proto/data.txt", "sample\ndata\n")]),
]

# Forks: source project, user who forks it, target namespace
FORKS = [
    dict(source=PPATH["web_portal"], user=U["frontend_dev"], namespace=U["frontend_dev"]),
    dict(source=PPATH["order_api"], user=U["dev2"], namespace=U["dev2"]),
]


def fork_path(f):
    return f"{f['namespace']}/{f['source'].split('/')[-1]}"


# ------------------------------------------- patches to the base manifest
def spec_of(path):
    return next(s for s in PROJECTS if s["path"] == path)


# order-api's pipeline includes a template from another project (cross-project include)
_order = spec_of(PPATH["order_api"])
_order["files"] = [
    (f, "include:\n  - project: '" + PPATH["infra_scripts"] + "'\n    file: '/ci/templates.yml'\n\n"
        "stages:\n  - test\n  - build\n\ntest:\n  stage: test\n  script:\n    - echo running tests\n")
    if f == ".gitlab-ci.yml" else (f, c)
    for f, c in _order["files"]
]
spec_of(PPATH["infra_scripts"])["files"].append(
    ("ci/templates.yml", ".deploy_template:\n  script:\n    - echo deploy\n"))


def template_files():
    return [
        (".gitlab/issue_templates/Bug.md",
         "## Summary\n\n## Steps to reproduce\n\n## Expected behaviour\n\n## Actual behaviour\n"),
        (".gitlab/merge_request_templates/Default.md",
         "## What does this MR do?\n\n## Related issues\n\nCloses #\n\n## Checklist\n\n- [ ] Tests added\n- [ ] Docs updated\n"),
    ]


# --------------------------------------------------------------- helpers
_UID = {}


def user_id(username):
    if username not in _UID:
        u = find_user(username)
        _UID[username] = u["id"] if u else None
    return _UID[username]


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt(t):
    """Timestamp format understood by git for GIT_*_DATE."""
    return t.strftime("%Y-%m-%d %H:%M:%S ") + TZ


def timeline(n, start, end, rng):
    """n unique timestamps between start and end: mostly weekdays, office hours,
    with a few quiet gaps."""
    days = (end - start).days
    if days < 60:
        sys.exit("HISTORY_START and HISTORY_END must be at least 60 days apart.")
    gaps = []
    for _ in range(3):
        g0 = start + dt.timedelta(days=rng.randint(30, max(31, days - 30)))
        gaps.append((g0, g0 + dt.timedelta(days=rng.randint(10, 21))))
    stamps = set()
    while len(stamps) < n:
        d = start + dt.timedelta(days=rng.randint(0, days))
        if any(a <= d <= b for a, b in gaps):
            continue
        if d.weekday() >= 5 and rng.random() < 0.85:
            continue
        stamps.add(dt.datetime.combine(
            d, dt.time(rng.randint(9, 18), rng.randint(0, 59), rng.randint(0, 59))))
    return sorted(stamps)


def tiny_png(color=(31, 111, 235), size=64):
    """A valid solid-colour PNG, used for issue attachments and wiki images."""
    raw = b"".join(b"\x00" + bytes(color) * size for _ in range(size))

    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def upload(project_id, filename, data, sudo=None):
    """Upload a file to a project (issue attachment). Returns the API answer, which
    includes ready-made markdown."""
    boundary = uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"{filename}\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode()
    body += data + f"\r\n--{boundary}--\r\n".encode()
    headers = {"PRIVATE-TOKEN": TOKEN, "Content-Type": f"multipart/form-data; boundary={boundary}"}
    if sudo:
        headers["Sudo"] = str(sudo)
    req = urllib.request.Request(f"{API}/projects/{project_id}/uploads", data=body,
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"  ! upload failed: {e}")
        return None


_SSH_WARNED = []


def gen_ssh_key(comment, kind="ed25519"):
    """Generate a real public key with ssh-keygen (private key is discarded)."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "k")
        cmd = (["ssh-keygen", "-q", "-t", kind] + (["-b", "2048"] if kind == "rsa" else [])
               + ["-N", "", "-C", comment, "-f", path])
        try:
            subprocess.run(cmd, check=True, capture_output=True)
        except (OSError, subprocess.CalledProcessError):
            if not _SSH_WARNED:
                print("  ! ssh-keygen not available, skipping SSH/deploy keys")
                _SSH_WARNED.append(1)
            return None
        with open(path + ".pub") as f:
            return f.read().strip()
