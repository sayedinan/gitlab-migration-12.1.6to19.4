#!/usr/bin/env python3
"""API-side enrichment of one project: access, labels, milestones, issues,
comments, merge requests, snippets, releases and project settings.

Steps (in this order): access, meta, issues, mrs, snippets, releases, settings
Everything is created as the right users through the Sudo header. Issue and
comment dates are backdated through the API; merge requests cannot be backdated
by the API and will carry today's date.
"""
import random
import re

from seed_extra import *

NOW = dt.datetime.now()
ENRICH_ORDER = ["access", "meta", "issues", "mrs", "snippets", "releases", "settings"]

LABELS = [("bug", "#d9534f"), ("feature", "#5cb85c"), ("urgent", "#ff0000"),
          ("documentation", "#428bca"), ("enhancement", "#8e44ad"), ("wontfix", "#7f8c8d"),
          ("doing", "#f0ad4e"), ("review", "#5bc0de")]

ISSUE_TEMPLATES = [
    ("bug", "Fix {t} not working as expected",
     "## Summary\n{t} behaves incorrectly in some cases.\n\n## Steps to reproduce\n1. Open the app\n"
     "2. Trigger {t}\n3. Observe the error\n\n## Expected\nIt works.\n\n## Actual\nIt fails intermittently."),
    ("feature", "Add {t}", "We need **{t}**.\n\n- [ ] design\n- [ ] implementation\n- [ ] tests\n- [ ] docs"),
    ("enhancement", "Improve {t}",
     "The current implementation of {t} is slow and hard to maintain.\n\n```\nTODO: profile and simplify\n```\n\n"
     "| Area | Status |\n|------|--------|\n| Perf | todo |\n| Tests | todo |"),
    ("documentation", "Document {t}", "Write documentation for {t}, including examples.\n\n> Needed for onboarding."),
    ("urgent", "Production issue with {t}",
     "Customers report problems with {t}. Needs attention today.\n\n- [x] Reproduced\n- [ ] Fix\n- [ ] Hotfix release"),
]
COMMENTS = [
    "Looking into this now.", "Can you share more details, @{u}?", "I can reproduce it on staging.",
    "Fixed in a branch, opening an MR.", "Thanks @{u}, please double-check.",
    "Let's discuss this in the next planning session.", "Blocked by another task, will pick this up next week.",
    "This looks related to #{n}.", "Added a note to the wiki.", "Verified, works on my side.",
    "Moving to the next sprint.", "+1, we hit this too.", "Émoji check 🚀 and unicode: naïve café.",
]


def people(spec):
    return list(dict.fromkeys(spec["authors"] + REPORTERS.get(spec["path"], [])))


def warm_up_access(pid, spec):
    """A newly created project isn't immediately visible via Sudo as its members -
    GitLab propagates each user's access in a background job. Poll as each
    involved user until they can see the project, so later Sudo calls don't 404."""
    for username in people(spec):
        for _ in range(15):
            code, _ = call("GET", f"/projects/{pid}", sudo=username)
            if code == 200:
                break
            time.sleep(2)
        else:
            print(f"  ! {username} still can't see project {pid} after warm-up, continuing anyway")


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


# ------------------------------------------------------------------ access
def step_access(pid, spec, rng, ctx):
    for group_path, level in SHARES.get(spec["path"], []):
        g = find_group(group_path)
        if g:
            api("POST", f"/projects/{pid}/share", {"group_id": g["id"], "group_access": level}, ok_codes=(409,))
    for username, level, expires in MEMBERS.get(spec["path"], []):
        if user_id(username):
            api("POST", f"/projects/{pid}/members",
                {"user_id": user_id(username), "access_level": level, "expires_at": expires}, ok_codes=(409,))


# -------------------------------------------------------------------- meta
def step_meta(pid, spec, rng, ctx):
    for name, color in LABELS:
        api("POST", f"/projects/{pid}/labels", {"name": name, "color": color}, ok_codes=(409,))
    labs = {l["name"]: l["id"] for l in (api("GET", f"/projects/{pid}/labels?per_page=100") or [])}

    span = (END - START).days
    milestones, to_close = [], []
    for frac, title, length, close in [(0.10, "Sprint 1", 14, True), (0.35, "Sprint 2", 14, True),
                                       (0.60, "v1.0 Release", 30, True), (0.85, "Sprint 3", 14, False)]:
        start = START + dt.timedelta(days=int(span * frac))
        m = api("POST", f"/projects/{pid}/milestones", {
            "title": title, "start_date": start.isoformat(),
            "due_date": (start + dt.timedelta(days=length)).isoformat(),
            "description": f"{title} for {spec['path'].split('/')[-1]}"})
        if m:
            milestones.append((m["id"], start))
            if close:
                to_close.append(m["id"])
    ctx["milestones"] = milestones
    ctx["close_milestones"] = to_close      # closed at the very end, after issues are attached

    boards = api("GET", f"/projects/{pid}/boards") or []
    if boards:
        for name in ("doing", "review"):
            if name in labs:
                api("POST", f"/projects/{pid}/boards/{boards[0]['id']}/lists",
                    {"label_id": labs[name]}, ok_codes=(400,))


# ------------------------------------------------------------------ issues
def step_issues(pid, spec, rng, ctx):
    ppl, devs = people(spec), spec["authors"]
    n = ctx.get("n_issues") or rng.randint(12, 18)
    stamps = timeline(n, START, END, rng)
    milestones = ctx.get("milestones", [])
    long_threads = set(rng.sample(range(n), min(2, n)))
    with_upload = set(rng.sample(range(n), min(2, n)))
    ctx["open_issues"] = []

    for i, when in enumerate(stamps):
        kind, title_t, body_t = rng.choice(ISSUE_TEMPLATES)
        topic = rng.choice(spec["topics"])
        author, assignee = rng.choice(ppl), rng.choice(devs)
        desc = body_t.format(t=topic) + f"\n\ncc @{assignee}"
        if i >= 3:
            desc += f"\n\nRelated to #{rng.randint(1, i)}."
        if rng.random() < 0.15 and spec["path"] != PPATH["order_api"]:
            desc += f"\n\nSee also {PPATH['order_api']}#1 for background."
        if rng.random() < 0.25:
            desc += f"\n\nDetails: {ABS}/{spec['path']}/issues"
        if i in with_upload:
            up = upload(pid, "screenshot.png", tiny_png(), sudo=author)
            if up:
                desc += "\n\n" + up["markdown"]

        data = {"title": title_t.format(t=topic), "description": desc, "labels": kind,
                "created_at": iso(when), "confidential": rng.random() < 0.08}
        if user_id(assignee):
            data["assignee_ids"] = [user_id(assignee)]
        if milestones and rng.random() < 0.75:
            cand = [m for m in milestones if m[1] <= when.date()]
            data["milestone_id"] = (cand[-1] if cand else milestones[0])[0]
        if rng.random() < 0.3:
            data["due_date"] = (when.date() + dt.timedelta(days=rng.randint(7, 21))).isoformat()

        issue = api("POST", f"/projects/{pid}/issues", data, sudo=author)
        if not issue:
            continue
        iid = issue["iid"]

        t = when
        count = rng.randint(25, 40) if i in long_threads else rng.choice([0, 1, 2, 2, 3, 4, 5])
        for _ in range(count):
            t += dt.timedelta(hours=rng.randint(1, 30), minutes=rng.randint(0, 59))
            if t > NOW:
                break
            body = rng.choice(COMMENTS).format(u=rng.choice(devs), n=rng.randint(1, max(1, i)))
            api("POST", f"/projects/{pid}/issues/{iid}/notes",
                {"body": body, "created_at": iso(t)}, sudo=rng.choice(ppl))

        if rng.random() < 0.4:
            api("POST", f"/projects/{pid}/issues/{iid}/award_emoji",
                {"name": rng.choice(["thumbsup", "tada", "eyes", "heart", "rocket"])},
                sudo=rng.choice(ppl), ok_codes=(404, 409))
        if rng.random() < 0.2:
            api("POST", f"/projects/{pid}/issues/{iid}/time_estimate", {"duration": "8h"}, sudo=assignee)
            api("POST", f"/projects/{pid}/issues/{iid}/add_spent_time",
                {"duration": f"{rng.randint(1, 6)}h"}, sudo=assignee)
        if rng.random() < 0.45:
            api("PUT", f"/projects/{pid}/issues/{iid}", {"state_event": "close"}, sudo=assignee)
        else:
            ctx["open_issues"].append(iid)


# --------------------------------------------------------- merge requests
def commit_api(pid, branch, message, actions, sudo, start_branch=None):
    data = {"branch": branch, "commit_message": message, "actions": actions}
    if start_branch:
        data["start_branch"] = start_branch
    return api("POST", f"/projects/{pid}/repository/commits", data, sudo=sudo)


def merge_mr(pid, iid, sudo):
    body = None
    for _ in range(8):
        time.sleep(2)     # GitLab needs a moment to compute mergeability
        code, body = call("PUT", f"/projects/{pid}/merge_requests/{iid}/merge",
                          {"should_remove_source_branch": True}, sudo)
        if code == 200:
            return True
    print(f"  ! could not merge MR !{iid}: {str(body)[:120]}")
    return False


def step_mrs(pid, spec, rng, ctx):
    ppl = people(spec)
    a = spec["authors"][0]                                    # maintainer, does the merges
    b = spec["authors"][-1]
    reviewer = next((p for p in ppl if p != a), a)
    topics = list(spec["topics"])
    rng.shuffle(topics)
    tag = rng.randint(100, 999)

    def branch_with_file(kind, topic, author):
        name = f"{kind}/{slug(topic)}-{tag}"
        api("POST", f"/projects/{pid}/repository/branches", {"branch": name, "ref": "master"})
        commit_api(pid, name, f"{kind.capitalize()}: {topic}",
                   [{"action": "create", "file_path": f"docs/{slug(topic)}-{tag}.md",
                     "content": f"# {topic}\n\nNotes about {topic}.\n"}], author)
        return name

    def open_mr(title, source, author, description=""):
        return api("POST", f"/projects/{pid}/merge_requests", {
            "source_branch": source, "target_branch": "master", "title": title,
            "description": description, "remove_source_branch": True}, sudo=author)

    # 1. merged MR that closes an issue
    issues = ctx.get("open_issues", [])
    topic = topics[0]
    br = branch_with_file("feature", topic, b)
    mr = open_mr(f"Implement {topic}", br, b, f"Closes #{issues.pop(0)}" if issues else "Implements the feature.")
    if mr:
        api("POST", f"/projects/{pid}/merge_requests/{mr['iid']}/notes", {"body": "LGTM, merging."}, sudo=a)
        merge_mr(pid, mr["iid"], a)

    # 2. plain merged MR
    topic = topics[1 % len(topics)]
    br = branch_with_file("bugfix", topic, a)
    mr = open_mr(f"Fix {topic}", br, a)
    if mr:
        api("POST", f"/projects/{pid}/merge_requests/{mr['iid']}/notes", {"body": "Approved."}, sudo=reviewer)
        merge_mr(pid, mr["iid"], a)

    # 3. open MR with review discussions (one resolved, one open)
    topic = topics[2 % len(topics)]
    br = branch_with_file("feature", f"{topic} v2", b)
    mr = open_mr(f"Rework {topic}", br, b, "Please review.")
    if mr:
        base = f"/projects/{pid}/merge_requests/{mr['iid']}"
        d1 = api("POST", f"{base}/discussions", {"body": "Can we rename this variable?"}, sudo=reviewer)
        if d1:
            api("POST", f"{base}/discussions/{d1['id']}/notes", {"body": "Done, renamed."}, sudo=b)
            # resolving needs Developer+ access; the reviewer may only hold Reporter, so use the maintainer
            api("PUT", f"{base}/discussions/{d1['id']}", {"resolved": True}, sudo=a)
        api("POST", f"{base}/discussions", {"body": "This needs a test before we merge."}, sudo=reviewer)

    # 4. work-in-progress MR
    topic = topics[3 % len(topics)]
    br = branch_with_file("feature", f"{topic} wip", b)
    open_mr(f"WIP: {topic}", br, b, "Not ready yet.")

    # 5. closed (rejected) MR
    topic = topics[4 % len(topics)]
    br = branch_with_file("feature", f"{topic} idea", b)
    mr = open_mr(f"Experiment: {topic}", br, b)
    if mr:
        api("POST", f"/projects/{pid}/merge_requests/{mr['iid']}/notes",
            {"body": "Closing, we chose a different approach."}, sudo=a)
        api("PUT", f"/projects/{pid}/merge_requests/{mr['iid']}", {"state_event": "close"}, sudo=a)

    # 6. two MRs editing the same line: one merges, the other ends up in conflict
    base_file = "CONTRIBUTING.md"
    commit_api(pid, "master", "Add CONTRIBUTING.md",
               [{"action": "create", "file_path": base_file, "content": "# Contributing\n\nMaintainer: TBD\n"}],
               a)
    ba, bb = f"docs/maintainer-a-{tag}", f"docs/maintainer-b-{tag}"
    api("POST", f"/projects/{pid}/repository/branches", {"branch": ba, "ref": "master"})
    api("POST", f"/projects/{pid}/repository/branches", {"branch": bb, "ref": "master"})
    commit_api(pid, ba, "Set maintainer", [{"action": "update", "file_path": base_file,
               "content": f"# Contributing\n\nMaintainer: {a} (primary)\n"}], a)
    commit_api(pid, bb, "Set maintainer", [{"action": "update", "file_path": base_file,
               "content": f"# Contributing\n\nMaintainer: {reviewer} (backup)\n"}], reviewer if reviewer in spec["authors"] else a)
    mra = open_mr("Set primary maintainer", ba, a)
    mrb = open_mr("Set backup maintainer", bb, a)
    if mra:
        merge_mr(pid, mra["iid"], a)
    if mrb:
        api("POST", f"/projects/{pid}/merge_requests/{mrb['iid']}/notes",
            {"body": "Looks like this conflicts now."}, sudo=reviewer)


# ---------------------------------------------------------------- snippets
def step_snippets(pid, spec, rng, ctx):
    name = spec["path"].split("/")[-1]
    items = [
        (f"{name}: quick check", "check.sh", "#!/bin/bash\nset -e\necho \"checking " + name + "\"\n"),
        (f"{name}: sample query", "query.sql", "SELECT id, status, created_at\nFROM orders\nORDER BY created_at DESC\nLIMIT 20;\n"),
    ]
    for title, fname, code in items:
        author = rng.choice(spec["authors"])
        s = api("POST", f"/projects/{pid}/snippets",
                {"title": title, "file_name": fname, "code": code,
                 "visibility": rng.choice(["internal", "private"])}, sudo=author)
        if s:
            api("POST", f"/projects/{pid}/snippets/{s['id']}/notes",
                {"body": "Handy, thanks for sharing."}, sudo=rng.choice(people(spec)))


# ---------------------------------------------------------------- releases
def step_releases(pid, spec, rng, ctx):
    a = spec["authors"][0]
    notes = {"v1.0.0": "## v1.0.0\n\nFirst stable release.\n\n- Core features\n- Documentation\n",
             "v1.1.0": "## v1.1.0\n\nMinor improvements and fixes.\n\n- Bug fixes\n- Performance tweaks\n"}
    for tag, text in notes.items():
        if not api("GET", f"/projects/{pid}/repository/tags/{q(tag)}", ok_codes=(404,)):
            continue   # tag was never pushed (e.g. the LFS push failed) - nothing to release
        api("POST", f"/projects/{pid}/releases",
            {"name": tag, "tag_name": tag, "ref": tag, "description": text}, sudo=a, ok_codes=(409,))


# ---------------------------------------------------------------- settings
_SHARED_KEY = {}
_slug = lambda key: PPATH[key].split("/")[-1]
MERGE_METHOD = {_slug("order_api"): "rebase_merge", _slug("test_suite"): "ff"}
STRICT_DISCUSSIONS = (_slug("order_api"), _slug("web_portal"))
DEPLOY_KEY_PROJECTS = (_slug("order_api"), _slug("web_portal"), _slug("infra_scripts"))
CI_PROJECTS = (_slug("order_api"), _slug("web_portal"), _slug("infra_scripts"))


def step_settings(pid, spec, rng, ctx):
    name = spec["path"].split("/")[-1]

    # protected branches and tags
    api("POST", f"/projects/{pid}/repository/branches", {"branch": "release/1.0", "ref": "v1.0.0"}, ok_codes=(400,))
    api("POST", f"/projects/{pid}/protected_branches",
        {"name": "release/*", "push_access_level": 40, "merge_access_level": 40}, ok_codes=(409,))
    api("POST", f"/projects/{pid}/protected_tags", {"name": "v*", "create_access_level": 40}, ok_codes=(409,))

    # webhooks (real, resolvable target - this instance has no outbound DNS, so
    # hostnames like example.com fail to resolve; point at the instance's own IP instead)
    self_ip = ABS.split("//", 1)[-1]
    for url, events in [(f"http://{self_ip}/hooks/push", {"push_events": True, "merge_requests_events": True}),
                        (f"http://{self_ip}/hooks/notify", {"issues_events": True, "note_events": True})]:
        api("POST", f"/projects/{pid}/hooks",
            dict(url=url, enable_ssl_verification=False, token="hook-secret-123", **events))

    # deploy keys (one per project plus one shared across projects)
    if name in DEPLOY_KEY_PROJECTS:
        key = gen_ssh_key(f"deploy@{name}")
        if key:
            api("POST", f"/projects/{pid}/deploy_keys", {"title": f"{name}-deploy", "key": key, "can_push": False})
        if "id" in _SHARED_KEY:
            api("POST", f"/projects/{pid}/deploy_keys/{_SHARED_KEY['id']}/enable")
        else:
            shared = gen_ssh_key("shared-ci")
            r = api("POST", f"/projects/{pid}/deploy_keys",
                    {"title": "shared-ci-key", "key": shared, "can_push": False}) if shared else None
            if r:
                _SHARED_KEY["id"] = r["id"]

    # CI/CD variables, schedules, triggers
    for key, value, protected, masked in [("DEPLOY_ENV", "staging", False, False),
                                          ("DB_HOST", f"db.{DOMAIN}", False, False),
                                          ("DB_PASSWORD", "SimDbPass2024", True, True),
                                          ("API_TOKEN", "sim-token-abcdef123456", True, True)]:
        api("POST", f"/projects/{pid}/variables",
            {"key": key, "value": value, "protected": protected, "masked": masked}, ok_codes=(400,))
    if name in CI_PROJECTS:
        api("POST", f"/projects/{pid}/pipeline_schedules",
            {"description": "Nightly build", "ref": "master", "cron": "0 2 * * *",
             "cron_timezone": "UTC", "active": True})
        api("POST", f"/projects/{pid}/triggers", {"description": "deploy trigger"})

    # badges
    api("POST", f"/projects/{pid}/badges", {
        "link_url": ABS + "/%{project_path}/pipelines",
        "image_url": ABS + "/%{project_path}/badges/%{default_branch}/pipeline.svg"})

    # project options
    settings = {"merge_method": MERGE_METHOD.get(name, "merge"),
                "only_allow_merge_if_all_discussions_are_resolved": name in STRICT_DISCUSSIONS,
                "request_access_enabled": True, "printing_merge_request_link_enabled": True,
                "tag_list": ["company", spec["path"].split("/")[-2]]}
    if name == _slug("ui_assets"):
        settings["builds_enabled"] = False
    api("PUT", f"/projects/{pid}", settings)


STEPS = {"access": step_access, "meta": step_meta, "issues": step_issues, "mrs": step_mrs,
         "snippets": step_snippets, "releases": step_releases, "settings": step_settings}


def grant_access(pid, spec):
    """Share/add-member step, run up front so warm_up_access has something real to check."""
    step_access(pid, spec, None, {})


def enrich(project, spec, only=None, light=False):
    """Run the enrichment steps. `only` limits them to a set of step names;
    `light` is for personal projects (labels and a few issues only)."""
    pid = project["id"]
    if not light:
        grant_access(pid, spec)
        warm_up_access(pid, spec)
    rng = random.Random(spec["path"] + "-enrich")
    ctx = {"n_issues": 4} if light else {}
    order = ["meta", "issues"] if light else ENRICH_ORDER
    if light and spec["authors"][0] in BLOCKED:
        order = ["meta"]          # a blocked user cannot be impersonated with Sudo
    for step in order:
        if only is None or step in only:
            print(f"  . {step}")
            try:
                STEPS[step](pid, spec, rng, ctx)
            except Exception as e:      # keep going, the other steps are independent
                print(f"  ! step {step} crashed: {e}")
    for mid in ctx.get("close_milestones", []):
        api("PUT", f"/projects/{pid}/milestones/{mid}", {"state_event": "close"})
