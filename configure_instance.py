#!/usr/bin/env python3
"""Configure instance-level things that a real server has and that do NOT
migrate with git: application settings, a broadcast message, a system hook and
an OAuth application.

  python3 configure_instance.py          # apply (saves the old settings first)
  python3 configure_instance.py --undo   # remove the message/hook/app and restore the saved settings
"""
import json

from seed_extra import *

BACKUP = "instance_settings_before.json"
MARK = "[seed]"
HOOK_URL = f"http://{GITLAB_URL.split('//', 1)[-1]}/system-hook"
APP_NAME = f"{COMPANY_NAME} CI (seed)"

SETTINGS = {
    "signup_enabled": False,
    "default_project_visibility": "private",
    "default_group_visibility": "private",
    "default_projects_limit": 20,
    "max_attachment_size": 30,
    "gravatar_enabled": False,
    "sign_in_text": f"Welcome to {COMPANY_NAME} GitLab. Authorized use only.",
    "help_page_text": f"Contact {EMAIL['it_admin']} for access requests.",
    "session_expire_delay": 10080,
}


def apply():
    current = api("GET", "/application/settings")
    if current is None:
        sys.exit("Could not read application settings (is the token an admin token?)")
    if not os.path.exists(BACKUP):
        with open(BACKUP, "w") as f:
            json.dump({k: current.get(k) for k in SETTINGS}, f, indent=2)
        print(f"  saved current values to {BACKUP}")
    if api("PUT", "/application/settings", SETTINGS) is not None:
        print("  + application settings updated")

    if not any(m["message"].startswith(MARK) for m in (api("GET", "/broadcast_messages") or [])):
        now = dt.datetime.now()
        api("POST", "/broadcast_messages", {
            "message": f"{MARK} Scheduled maintenance on the last Friday of the month.",
            "starts_at": (now - dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "ends_at": (now + dt.timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "color": "#1F6FEB", "font": "#FFFFFF"})
        print("  + broadcast message")

    if not any(h["url"] == HOOK_URL for h in (api("GET", "/hooks") or [])):
        r = api("POST", "/hooks", {"url": HOOK_URL, "push_events": True, "tag_push_events": True,
                                   "enable_ssl_verification": False})
        print("  + system hook" if r is not None else "  ! system hook failed, see error above")

    if not any(a.get("application_name") == APP_NAME for a in (api("GET", "/applications") or [])):
        api("POST", "/applications", {"name": APP_NAME, "redirect_uri": f"http://ci.{DOMAIN}/callback",
                                      "scopes": "api read_user"})
        print("  + OAuth application")


def undo():
    for m in api("GET", "/broadcast_messages") or []:
        if m["message"].startswith(MARK):
            api("DELETE", f"/broadcast_messages/{m['id']}")
            print("  x broadcast message removed")
    for h in api("GET", "/hooks") or []:
        if h["url"] == HOOK_URL:
            api("DELETE", f"/hooks/{h['id']}")
            print("  x system hook removed")
    for a in api("GET", "/applications") or []:
        if a.get("application_name") == APP_NAME:
            api("DELETE", f"/applications/{a['id']}")
            print("  x OAuth application removed")
    if os.path.exists(BACKUP):
        with open(BACKUP) as f:
            old = {k: v for k, v in json.load(f).items() if v is not None}
        if api("PUT", "/application/settings", old) is not None:
            print("  + settings restored from backup")


if __name__ == "__main__":
    print("== Instance configuration")
    undo() if "--undo" in sys.argv else apply()
    print("Done.")
