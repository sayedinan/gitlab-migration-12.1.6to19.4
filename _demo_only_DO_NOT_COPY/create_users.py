#!/usr/bin/env python3
"""Create the seeded users, the group hierarchy and the memberships, plus
per-user and per-group extras: profile fields, a second email, SSH keys,
group labels/milestones/variables and personal snippets.
Safe to re-run: existing users/groups are reused and extras are not duplicated."""
import random

from seed_extra import *

JOBS = {U[role_key]: title for role_key, title in JOB_TITLE.items()}
ALT_EMAIL_USERS = [U["dev_lead"], U["frontend_lead"], U["devops"], U["qa_lead"]]
GROUP_LABELS = [("bug", "#d9534f"), ("feature", "#5cb85c"), ("urgent", "#ff0000"),
                ("documentation", "#428bca")]
GROUP_MILESTONES = {
    "company": [("Company Planning Q1", 0.05, 60), ("Company Planning Q3", 0.55, 60)],
    "company/engineering": [("Platform 2.0", 0.30, 120)],
}
PERSONAL_SNIPPETS = [
    (U["dev_lead"], "Git aliases", "aliases.txt",
     "git config --global alias.st status\ngit config --global alias.co checkout\n", "private"),
    (U["devops"], "Nginx reverse proxy", "nginx.conf",
     "location / {\n  proxy_pass http://127.0.0.1:8080;\n}\n", "internal"),
    (U["qa_lead"], "Bug report template", "bug.md",
     "**Steps**\n**Expected**\n**Actual**\n", "public"),
]


def make_users():
    uid = {}
    print("== Users")
    for username, name, email, admin in USERS:
        existing = find_user(username)
        if existing:
            uid[username] = existing["id"]
            print(f"  = {username} (already exists)")
            continue
        u = api("POST", "/users", {
            "username": username, "name": name, "email": email,
            "password": PASSWORD, "skip_confirmation": True,
            "admin": admin, "projects_limit": 20,
            "organization": COMPANY_NAME, "bio": name,
        })
        if u:
            uid[username] = u["id"]
            print(f"  + {username}")
    return uid


def make_groups(uid):
    print("== Groups and memberships")
    gid = {}
    for full, name, visibility, members in GROUPS:
        group = find_group(full)
        if group:
            print(f"  = {full} (already exists)")
        else:
            data = {"name": name, "path": full.split("/")[-1],
                    "visibility": visibility, "description": f"{name} group"}
            if "/" in full:
                parent = gid.get(full.rsplit("/", 1)[0])
                if not parent:
                    print(f"  ! parent of {full} missing, skipping")
                    continue
                data["parent_id"] = parent
            group = api("POST", "/groups", data)
            if not group:
                continue
            print(f"  + {full}")
        gid[full] = group["id"]
        for username, level in members.items():
            if username in uid:
                api("POST", f"/groups/{group['id']}/members",
                    {"user_id": uid[username], "access_level": level}, ok_codes=(409,))
    return gid


def user_extras(uid):
    print("== Profiles, second emails, SSH keys")
    for username, job in JOBS.items():
        if username in uid:
            api("PUT", f"/users/{uid[username]}", {
                "job_title": job, "location": "HQ Office",
                "twitter": username.replace(".", "_"), "linkedin": username.replace(".", "-")})
    for username in ALT_EMAIL_USERS:
        if username in uid and not api("GET", f"/users/{uid[username]}/emails"):
            api("POST", f"/users/{uid[username]}/emails",
                {"email": f"{username.replace('.', '_')}@alt.{DOMAIN}", "skip_confirmation": True})
    for username, user_id_ in uid.items():
        if api("GET", f"/users/{user_id_}/keys"):
            continue                                   # already has keys
        rng = random.Random(username)
        for i in range(rng.randint(1, 3)):
            kind = "ed25519" if i == 0 else rng.choice(["ed25519", "rsa"])
            key = gen_ssh_key(f"{username}@workstation-{i + 1}", kind)
            if not key:
                return
            api("POST", f"/users/{user_id_}/keys", {"title": f"{username} workstation {i + 1}", "key": key})


def group_extras(gid):
    print("== Group labels, milestones, variables")
    span = (END - START).days
    company = gid.get("company")
    if company:
        for name, color in GROUP_LABELS:
            api("POST", f"/groups/{company}/labels", {"name": name, "color": color}, ok_codes=(409,))
        for key, value, masked in [("COMPANY_REGISTRY", f"registry.{DOMAIN}", False),
                                   ("SLACK_WEBHOOK", "https-hooks-slack-sim-token-123", True)]:
            api("POST", f"/groups/{company}/variables",
                {"key": key, "value": value, "masked": masked}, ok_codes=(400,))
    for path, items in GROUP_MILESTONES.items():
        if path not in gid:
            continue
        existing = {m["title"] for m in (api("GET", f"/groups/{gid[path]}/milestones") or [])}
        for title, frac, length in items:
            if title in existing:
                continue
            start = START + dt.timedelta(days=int(span * frac))
            api("POST", f"/groups/{gid[path]}/milestones", {
                "title": title, "start_date": start.isoformat(),
                "due_date": (start + dt.timedelta(days=length)).isoformat()})


def personal_snippets(uid):
    print("== Personal snippets")
    for user, title, fname, content, vis in PERSONAL_SNIPPETS:
        if user not in uid:
            continue
        have = {s["title"] for s in (api("GET", "/snippets", sudo=user) or [])}
        if title not in have:
            api("POST", "/snippets", {"title": title, "file_name": fname, "content": content,
                                      "visibility": vis}, sudo=user)


def main():
    uid = make_users()
    for username in BLOCKED:
        if username in uid:
            api("POST", f"/users/{uid[username]}/block", ok_codes=(201,))
            print(f"  x {username} blocked")
    gid = make_groups(uid)
    user_extras(uid)
    group_extras(gid)
    personal_snippets(uid)
    print(f"\nDone. {len(uid)} users, {len(gid)} groups. Default password: {PASSWORD}")


if __name__ == "__main__":
    main()
