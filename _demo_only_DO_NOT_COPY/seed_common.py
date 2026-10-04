#!/usr/bin/env python3
"""Shared config, API helpers and the seed manifest (users, groups, projects).

Names, emails, the company/domain, and the last path segment of each seeded
project are randomly generated on every run (see the "identity generation"
section below), so re-running the seeders produces a different-looking company
each time. Set SEED to an integer to get the same generated data back (useful
for repeatable test runs); leave it unset for fresh randomness every run.

The delete scripts only ever touch names listed in this file, so root and any
real accounts/projects are never affected.
"""
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

GITLAB_URL = (os.environ.get("GITLAB_URL") or sys.exit(
    "Set GITLAB_URL first, e.g.:  export GITLAB_URL=https://gitlab.example.com")).rstrip("/")
TOKEN = os.environ.get("GITLAB_TOKEN") or sys.exit(
    "Set GITLAB_TOKEN first:  read -rs GITLAB_TOKEN; export GITLAB_TOKEN")
PASSWORD = os.environ.get("SEED_PASSWORD", "Sim#Pass12345")
API = GITLAB_URL + "/api/v4"

GUEST, REPORTER, DEVELOPER, MAINTAINER, OWNER = 10, 20, 30, 40, 50

_seed_env = os.environ.get("SEED")
RNG = random.Random(int(_seed_env)) if _seed_env else random.Random()


# ------------------------------------------------------------------ helpers
def q(s):
    return urllib.parse.quote(s, safe="")


def call(method, path, data=None, sudo=None):
    """Returns (status_code, parsed_body). Never raises."""
    headers = {"PRIVATE-TOKEN": TOKEN}
    body = None
    if sudo:
        headers["Sudo"] = str(sudo)
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(API + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            txt = r.read().decode()
            return r.status, (json.loads(txt) if txt else {})
    except urllib.error.HTTPError as e:
        txt = e.read().decode()
        try:
            return e.code, json.loads(txt)
        except ValueError:
            return e.code, txt
    except Exception as e:  # network problems
        return 0, str(e)


def api(method, path, data=None, sudo=None, ok_codes=()):
    """Returns body on success, None on failure (prints the error)."""
    code, body = call(method, path, data, sudo)
    if code == 0 or code >= 400:
        if code not in ok_codes:
            print(f"  ! {method} {path} -> {code} {str(body)[:160]}")
        return None
    return body


def find_user(username):
    body = api("GET", f"/users?username={q(username)}")
    return body[0] if body else None


def find_group(full_path):
    return api("GET", f"/groups/{q(full_path)}", ok_codes=(404,))


def find_project(full_path):
    return api("GET", f"/projects/{q(full_path)}", ok_codes=(404,))


def wait_gone(path, timeout=240):
    """GitLab deletes groups/projects/users in the background; wait for 404."""
    end = time.time() + timeout
    while time.time() < end:
        code, _ = call("GET", path)
        if code == 404:
            return True
        time.sleep(3)
    return False


def confirm(message):
    print(message)
    if "--yes" in sys.argv:
        return True
    return input("Type 'yes' to continue: ").strip().lower() == "yes"


# =================================================== identity generation
# Everything below is randomized per run (or deterministically from SEED).
# Fixed "role keys" (e.g. "dev_lead", "qa_lead") are the stable handles every
# other script uses to refer to a person/project without caring what name or
# slug got picked this time around: seed_extra.py, seed_enrich.py,
# create_users.py and create_projects.py all import U (username lookup) and
# PPATH (project path lookup) from here instead of hardcoding literal names.

FIRST_NAMES = [
    "Tanvir", "Rahim", "Sara", "Nadia", "Imran", "Karim", "Farhana", "Jahid", "Mita", "Zayed",
    "Ali", "Mahmud", "Sadia", "Rafiq", "Nusrat", "Shakil", "Tania", "Hasan", "Priya", "Arif",
    "Mou", "Rezwan", "Lubna", "Faisal", "Anika", "Shuvo", "Ruma", "Kamal", "Dilruba", "Sabbir",
    "Farid", "Meherun", "Tamim", "Shirin", "Nayeem",
]
LAST_NAMES = [
    "Ahmed", "Uddin", "Khan", "Islam", "Hasan", "Sheikh", "Akter", "Alam", "Das", "Karim",
    "Hossain", "Chowdhury", "Rahman", "Sultana", "Haque", "Siddique", "Bhuiyan", "Miah",
    "Talukder", "Kabir",
]
COMPANY_NAMES = [
    ("Nexora Systems", "nexora"), ("BrightPath Technologies", "brightpath"),
    ("Vertex Solutions", "vertex"), ("Skyline Softworks", "skyline"),
    ("Meridian Labs", "meridian"), ("Northwind Digital", "northwind"),
    ("Cascade Technologies", "cascade"), ("Anchorline Software", "anchorline"),
    ("Bluepeak Systems", "bluepeak"), ("Ridgeway Tech", "ridgeway"),
]

# category -> [(slug, description), ...] - one pair is picked per run so the
# project's path segment and its description always stay in sync.
PROJECT_NAME_POOLS = {
    "backend": [
        ("order-api", "Order management REST API"),
        ("payment-api", "Payment processing REST API"),
        ("billing-service", "Customer billing and invoicing service"),
        ("checkout-service", "Checkout and cart REST API"),
        ("ledger-api", "Internal financial ledger API"),
    ],
    "frontend": [
        ("web-portal", "Customer web portal"),
        ("customer-portal", "Customer self-service portal"),
        ("storefront-app", "Storefront web application"),
        ("client-dashboard", "Client-facing dashboard app"),
        ("account-hub", "Customer account management app"),
    ],
    "devops": [
        ("infra-scripts", "Deployment and backup scripts"),
        ("platform-tools", "Internal platform automation tools"),
        ("ops-automation", "Operations automation scripts"),
        ("release-tools", "Release and deployment tooling"),
    ],
    "qa": [
        ("test-suite", "Automated regression tests"),
        ("qa-automation", "QA automation framework"),
        ("regression-suite", "Regression test suite"),
        ("test-framework", "Shared test automation framework"),
    ],
    "design": [
        ("ui-assets", "Brand and UI assets"),
        ("brand-kit", "Brand assets and guidelines"),
        ("design-system", "Shared design system and tokens"),
        ("style-guide", "UI style guide and assets"),
    ],
    "docs": [
        ("docs-wiki", "Internal policies, meeting notes and roadmap"),
        ("internal-docs", "Internal documentation and policies"),
        ("knowledge-base", "Company knowledge base"),
        ("team-wiki", "Team wiki: notes, roadmap and policies"),
    ],
}

COMPANY_NAME, _company_slug = RNG.choice(COMPANY_NAMES)
DOMAIN = f"{_company_slug}.local"

# named (person) roles: role_key -> (username prefix, job title)
_NAMED_ROLES = [
    ("cto", "cto", "CTO"),
    ("pm", "pm", "Product Manager"),
    ("dev_lead", "dev", "Senior Backend Engineer"),
    ("dev2", "dev", "Backend Engineer"),
    ("dev3", "dev", "Backend Engineer"),
    ("frontend_lead", "dev", "Frontend Lead"),
    ("frontend_dev", "dev", "Frontend Engineer"),
    ("devops", "devops", "DevOps Engineer"),
    ("qa_lead", "qa", "QA Lead"),
    ("qa_dev", "qa", "QA Engineer"),
    ("designer", "designer", "UI Designer"),
    ("contractor", "contractor", "Contractor"),
]

_firsts = RNG.sample(FIRST_NAMES, len(_NAMED_ROLES))
_lasts = RNG.sample(LAST_NAMES, len(_NAMED_ROLES))

U = {}          # role_key -> username
NAME = {}       # role_key -> full name
EMAIL = {}      # role_key -> email
JOB_TITLE = {}  # role_key -> job title

for (role_key, prefix, title), first, last in zip(_NAMED_ROLES, _firsts, _lasts):
    username = f"{prefix}.{first.lower()}"
    U[role_key] = username
    NAME[role_key] = f"{first} {last}"
    EMAIL[role_key] = f"{first.lower()}@{DOMAIN}"
    JOB_TITLE[role_key] = title

# role (non-person-named) accounts keep fixed, self-describing usernames
for role_key, username, name in [
    ("it_admin", "it.admin", "IT Administrator"),
    ("eng_manager", "eng.manager", "Engineering Manager"),
    ("ex_employee", "ex.employee", "Old Employee"),
]:
    U[role_key] = username
    NAME[role_key] = name
    EMAIL[role_key] = f"{username}@{DOMAIN}"

JOB_TITLE["it_admin"] = "IT Administrator"
JOB_TITLE["eng_manager"] = "Engineering Manager"
JOB_TITLE["ex_employee"] = "Former Engineer"

EMAIL["contractor"] = f"{NAME['contractor'].split()[0].lower()}@external.local"

# project paths: category -> (group_path, slug, desc), picked once per run
_PROJECT_GROUPS = {
    "order_api": ("company/engineering/backend", "backend"),
    "web_portal": ("company/engineering/frontend", "frontend"),
    "infra_scripts": ("company/engineering/devops", "devops"),
    "test_suite": ("company/qa", "qa"),
    "ui_assets": ("company/design", "design"),
    "docs_wiki": ("company/management", "docs"),
}
PPATH = {}   # project key -> "company/.../<slug>"
PDESC = {}   # project key -> description
for key, (group_path, category) in _PROJECT_GROUPS.items():
    slug, desc = RNG.choice(PROJECT_NAME_POOLS[category])
    PPATH[key] = f"{group_path}/{slug}"
    PDESC[key] = desc


# --------------------------------------------------------------------- users
# username, full name, email, is_admin
USERS = [
    (U["it_admin"],      NAME["it_admin"],      EMAIL["it_admin"],      True),
    (U["cto"],           NAME["cto"],           EMAIL["cto"],           False),
    (U["eng_manager"],   NAME["eng_manager"],   EMAIL["eng_manager"],   False),
    (U["pm"],            NAME["pm"],            EMAIL["pm"],            False),
    (U["dev_lead"],      NAME["dev_lead"],      EMAIL["dev_lead"],      False),
    (U["dev2"],          NAME["dev2"],          EMAIL["dev2"],          False),
    (U["dev3"],          NAME["dev3"],          EMAIL["dev3"],          False),
    (U["frontend_lead"], NAME["frontend_lead"], EMAIL["frontend_lead"], False),
    (U["frontend_dev"],  NAME["frontend_dev"],  EMAIL["frontend_dev"],  False),
    (U["devops"],        NAME["devops"],        EMAIL["devops"],        False),
    (U["qa_lead"],       NAME["qa_lead"],       EMAIL["qa_lead"],       False),
    (U["qa_dev"],        NAME["qa_dev"],        EMAIL["qa_dev"],        False),
    (U["designer"],      NAME["designer"],      EMAIL["designer"],      False),
    (U["contractor"],    NAME["contractor"],    EMAIL["contractor"],    False),
    (U["ex_employee"],   NAME["ex_employee"],   EMAIL["ex_employee"],   False),
]
BLOCKED = [U["ex_employee"]]
USER = {u[0]: {"name": u[1], "email": u[2], "admin": u[3]} for u in USERS}

# -------------------------------------------------------------------- groups
# (full_path, display name, visibility, {username: access_level})
GROUPS = [
    ("company", COMPANY_NAME, "internal",
     {U["cto"]: OWNER, U["it_admin"]: OWNER, U["pm"]: REPORTER}),
    ("company/engineering", "Engineering", "private", {U["eng_manager"]: MAINTAINER}),
    ("company/engineering/backend", "Backend", "private",
     {U["dev_lead"]: MAINTAINER, U["dev2"]: DEVELOPER, U["dev3"]: DEVELOPER}),
    ("company/engineering/frontend", "Frontend", "private",
     {U["frontend_lead"]: MAINTAINER, U["frontend_dev"]: DEVELOPER,
      U["designer"]: REPORTER, U["contractor"]: GUEST}),
    ("company/engineering/devops", "DevOps", "private",
     {U["devops"]: MAINTAINER, U["dev_lead"]: DEVELOPER}),
    ("company/qa", "QA", "private",
     {U["qa_lead"]: MAINTAINER, U["qa_dev"]: DEVELOPER, U["dev_lead"]: REPORTER}),
    ("company/design", "Design", "internal",
     {U["designer"]: MAINTAINER, U["frontend_lead"]: REPORTER}),
    ("company/management", "Management", "private",
     {U["pm"]: MAINTAINER, U["eng_manager"]: DEVELOPER}),
]

# ------------------------------------------------------------------ projects
# authors: first one is the main contributor (also does the merges)
# commits: how many commits the backdated history should contain
PROJECTS = [
    dict(
        path=PPATH["order_api"],
        desc=PDESC["order_api"], vis="private", commits=80,
        authors=[U["dev_lead"], U["dev2"], U["dev3"]],
        topics=["order validation", "pagination", "auth middleware", "db connection pool",
                "error handling", "order status endpoint", "request logging",
                "request timeouts", "health check", "unit tests"],
        files=[
            ("app/main.py", "from flask import Flask\n\napp = Flask(__name__)\n\n\n@app.route('/health')\ndef health():\n    return 'ok'\n"),
            ("app/orders.py", "def create_order(items):\n    return {'id': 1, 'items': items}\n"),
            ("requirements.txt", "flask==1.0.3\npsycopg2==2.8.3\n"),
            ("docs/API.md", "# Order API\n\n- GET /health\n- POST /orders\n"),
            (".gitlab-ci.yml", "stages:\n  - test\n  - build\n\ntest:\n  stage: test\n  script:\n    - echo running tests\n"),
        ],
    ),
    dict(
        path=PPATH["web_portal"],
        desc=PDESC["web_portal"], vis="private", commits=65,
        authors=[U["frontend_lead"], U["frontend_dev"]],
        topics=["login form", "navbar layout", "dark theme", "order table",
                "form validation", "mobile styles", "API client", "error banner",
                "footer links", "accessibility labels"],
        files=[
            ("index.html", "<!doctype html>\n<html>\n<head><title>Portal</title></head>\n<body><div id=\"app\"></div></body>\n</html>\n"),
            ("src/app.js", "document.getElementById('app').textContent = 'Hello Portal';\n"),
            ("src/styles.css", "body {\n  font-family: sans-serif;\n  margin: 0;\n}\n"),
            ("package.json", "{\n  \"name\": \"" + PPATH["web_portal"].split("/")[-1] + "\",\n  \"version\": \"1.0.0\"\n}\n"),
        ],
    ),
    dict(
        path=PPATH["infra_scripts"],
        desc=PDESC["infra_scripts"], vis="private", commits=40,
        authors=[U["devops"], U["dev_lead"]],
        topics=["backup rotation", "deploy script", "disk alerts", "log cleanup",
                "restore steps", "docker compose", "healthcheck", "cron schedule",
                "monitoring thresholds"],
        files=[
            ("backup/db_backup.sh", "#!/bin/bash\npg_dump -U postgres appdb | gzip > /backups/appdb_$(date +%F).sql.gz\n"),
            ("deploy/deploy.sh", "#!/bin/bash\nset -e\ndocker-compose pull\ndocker-compose up -d\n"),
            ("monitoring/check_disk.sh", "#!/bin/bash\ndf -h | awk '$5+0 > 85 {print \"WARN\", $0}'\n"),
            ("README.md", "# Infra scripts\n\nInternal use only.\n"),
        ],
    ),
    dict(
        path=PPATH["test_suite"],
        desc=PDESC["test_suite"], vis="private", commits=45,
        authors=[U["qa_lead"], U["qa_dev"]],
        topics=["login tests", "order tests", "fixtures", "smoke suite", "test data",
                "retry logic", "report output", "API contract checks"],
        files=[
            ("tests/test_login.py", "def test_login():\n    assert True\n"),
            ("tests/test_orders.py", "def test_create_order():\n    assert True\n"),
            ("pytest.ini", "[pytest]\ntestpaths = tests\n"),
            ("TEST_PLAN.md", "# Test plan\n\n- Smoke\n- Regression\n- Release checklist\n"),
        ],
    ),
    dict(
        path=PPATH["ui_assets"],
        desc=PDESC["ui_assets"], vis="internal", commits=30,
        authors=[U["designer"]],
        topics=["color palette", "typography scale", "icon guidelines", "logo variants",
                "spacing tokens", "button styles"],
        files=[
            ("brand/colors.md", "# Colors\n\n- Primary: #1F6FEB\n- Secondary: #FF9F1C\n"),
            ("brand/typography.md", "# Typography\n\n- Headings: Inter\n- Body: Roboto\n"),
            ("icons/README.md", "Icon set guidelines.\n"),
        ],
    ),
    dict(
        path=PPATH["docs_wiki"],
        desc=PDESC["docs_wiki"], vis="private", commits=30,
        authors=[U["pm"], U["eng_manager"]],
        topics=["roadmap", "meeting notes", "code review policy", "onboarding guide",
                "release checklist", "access policy"],
        files=[
            ("roadmap/2026.md", "# Roadmap\n\n- Q1: Portal v1\n- Q2: Order API v2\n- Q3: Infra migration\n"),
            ("policies/code-review.md", "# Code review policy\n\nAt least one approval before merge.\n"),
            ("meetings/standup-notes.md", "# Standup notes\n\n- Blockers: none\n"),
        ],
    ),
]
