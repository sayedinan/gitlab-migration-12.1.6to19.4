# GitLab Migration Runbook — REAL Migration (v12.1.6 → new instance)

**Purpose:** Migrate the company's real legacy GitLab CE v12.1.6 instance to the
new instance, preserving repo history, users, groups, permissions, issues,
merge requests, and comments (with original authors and timestamps intact).

**Status as of writing:** This is the **patched** version of the scripts,
fixed after two rounds of code review against the rehearsal package. All 6
known issues below are fixed in the scripts in this folder. Read this whole
document before running anything against the real servers.

**Who this is for:** Anyone picking this up cold. No prior context assumed.

---

## 0. What changed vs. the rehearsal package — read this first

The three real migration scripts plus `config.yaml` live in this folder's top
level. The demo/seed scripts (`create_users.py`, `create_projects.py`,
`delete_users.py`, `delete_projects.py`, `seed_common.py`, `seed_enrich.py`,
`seed_extra.py`, `configure_instance.py`) are in `_demo_only_DO_NOT_COPY/`.
They only build/tear down the fake rehearsal environment — never copy that
subfolder to a real server (`delete_users.py` / `delete_projects.py` are
destructive).

Six issues were found across two code-review passes on the rehearsal
scripts. **All six are now fixed** in the copies in this folder:

| # | Issue | Fix |
|---|---|---|
| 1 | Protected branches were unprotected for the mirror push but never re-protected afterward | `migrate_gitlab.py` now records each branch's protection settings before removing them and restores them in a `finally` block, whether the push succeeds, fails, or times out |
| 2 | `migrate_metadata.rb` had its own hardcoded `PROJECT_ALLOWLIST = []`, ignoring `config.yaml` | `migrate_metadata.rb` now reads `project_allowlist` from `config.yaml` directly (see Step 2.3 below for the one extra setup step this requires) |
| 3 | A failing MR comment got logged as `"Failed to create MR..."` even though the MR existed, and never got marked done → re-run created a duplicate MR | MR creation and its comments are now tracked and rescued **separately** — a comment failure never touches the MR's own completion state |
| 4 | Legacy `admin` users were recreated as ordinary users on the new instance, with no flag or warning | `migrate_users` now reads each legacy user's `is_admin` flag and passes `admin: true` when creating the matching user on the new instance |
| 5 | A failing issue comment crashed the whole script *and* left the issue unmarked → re-run created a duplicate issue (worse than originally thought — see Step 6) | Same fix as #3, applied to issues: issue creation is tracked and rescued independently of its comments |
| 6 | No `timeout=` on any `git`/`git lfs` subprocess call — a stalled connection could hang a worker thread (and the whole script) forever | All git subprocess calls now have a 30-minute timeout (`GIT_SUBPROCESS_TIMEOUT` near the top of `migrate_gitlab.py` — raise it if your real repos are very large) |

### 0.1 — Revision 3 additions (after the final-simulation review)

| # | Change | Why |
|---|---|---|
| 7 | `migrate_metadata.rb`: `Net::HTTP` now uses `use_ssl` for https URLs | Before, an https legacy URL made every legacy call fail silently (non-success = "no more pages"), so the script migrated **zero** issues/MRs and still printed "Done." |
| 8 | `migrate_metadata.rb`: closed issues get `closed_at`/`closed_by`; merged/closed MRs get `merged_at`/`merged_by`/closed timestamp; issue+MR assignees are set | These were dropped before. Done in a separate rescue after the item is saved to state, so a failure only logs a WARN and can never duplicate an item. **Only applied when an item is first created** — items created by an earlier run are not back-filled. Not yet validated on a 19.x instance: check the log for `enrichment failed` WARNs on the simulation. |
| 9 | `migrate_gitlab.py`: `State.mark` uses `setdefault` | An older `migration_state.json` without the new categories would have raised `KeyError`. |
| 10 | New steps `project_members`, `webhooks`, `protected_branches` (run after `mirror`) | Direct project members, webhooks and legacy protected-branch rules were not migrated by anything. Webhook **secret tokens cannot be read** from the legacy API — re-enter them by hand where receivers check them. If webhook creation fails with a local-network error, enable "Allow requests to the local network from webhooks and integrations" in the new instance's Admin > Settings > Network. Protected branches: only role-based rules (push/merge access level, force-push flag) are copied; the auto-created rule for the same branch name is replaced by the legacy one. |

**Still NOT migrated:** labels, milestones, wikis, snippets, deploy keys, protected tags,
issue/MR attachments (`/uploads/...` links in text will be broken), fork relationships.

**State file compatibility:** issue #3/#5's fix changed the *shape* of
`state["issues"]`/`state["mrs"]` entries in `migrate_metadata.rb` (from
`true` to `{"iid" => N}`). If you ever ran the old unpatched script against
a real instance and it left behind `/tmp/metadata_migration_state.json`,
**delete that file** before running this patched version — don't mix old
and new state files.

### 0.2 — Lessons from the first real run (read before you touch state files or the UI)

**State files track the scripts' own progress, not the GitLab database.**
`migration_state.json` (Part A) and `/tmp/metadata_migration_state.json`
(Part B) are the *only* things that decide whether a step "already ran."
Deleting projects, groups or users through the web UI does **not** touch
either file. If you delete data through the UI and then re-run a script,
it will see its own state file, conclude everything is already done, and
silently skip recreating anything. If you ever need to redo a migration
after clearing data in the UI, you must also delete both state files
first (back them up, don't just discard them — see "Rollback" below).

**Recognize this exact crash if you see it — it means mixed state files:**
```
migrate_metadata.rb:160:in `block (2 levels) in <main>': undefined method `[]' for true (NoMethodError)
      issue = Issue.find_by(project: project, iid: existing["iid"])
```
This happened for real, on `/tmp/metadata_migration_state.json`. It means
that file has at least one legacy-shape (`true`) entry for an issue or MR,
alongside new-shape (`{"iid" => N}`) entries created by this patched
script. **Do not just re-run it** — some projects may already have
duplicated data from an earlier, unpatched run. Back up the state file,
identify which projects have `true` entries, check whether those
issues/MRs already exist on the new instance by comparing titles and
timestamps against legacy, and only then correct or reset the affected
entries. Ask for help reconciling rather than guessing — a wrong guess
here creates duplicate issues that are tedious to clean up by hand.

**Part B (`migrate_metadata.rb`) only ever runs on the new instance.**
It uses `gitlab-rails runner`, which requires a full Rails/GitLab
installation. The legacy side (a plain Docker container in our case) does
not have this and the command will fail immediately with
`gitlab-rails: command not found`. If you see that error, you're on the
wrong host — go to the new instance.

**Part A's `[ERROR] API error 404` lines during creation are expected,
not failures.** Before creating a group/project, the script first checks
whether it already exists by fetching it; a 404 there just means "doesn't
exist yet, proceed to create." Only treat it as a real problem if there is
**no** `Created ...` line immediately after it for that same item.

**Confirm the legacy URL/scheme actually in use before Part B.**
`config.yaml`'s `legacy.url` (read by Part A and by `validate_migration.py`)
and the `GITLAB_LEGACY_URL` environment variable (read by
`migrate_metadata.rb`) are two separate settings that are never
cross-checked. In our real run these ended up pointing at different
ports, and only one of them actually worked. Before running Part B,
verify with `curl` which URL/scheme reaches the legacy API, and use that
exact value in `GITLAB_LEGACY_TOKEN`'s companion `GITLAB_LEGACY_URL` — don't
assume it matches `config.yaml`.

---

## 1. Before you touch anything — read this

**Why export/import won't work:** GitLab's built-in "export project / import
project" feature fails between versions this far apart (legacy 12.1.6 → new
19.x gave `"invalid import format"` in testing). Don't waste time on it.

**The actual approach — two layers, because they need different tools:**
1. **Git repo history** (commits, branches, tags) → moved via `git mirror`
   push. Fast, reliable, no data loss.
2. **Everything GitLab stores in its database** (users, groups, permissions,
   issues, MRs, comments) → moved via custom scripts, because it doesn't
   live in git at all.

**Why not just use the GitLab API for issues/MRs?** The REST API lets you
set the *author* of an issue/MR (via a `Sudo:` header) OR the *original
creation date* (`created_at`, admin-only) — but never both in the same
call. The only way to get both right is `gitlab-rails runner`, which talks
directly to GitLab's database models, bypassing that API limitation.

**What's explicitly OUT of scope** (confirmed with the team):
- CI/CD runners and pipeline history — confirmed not needed (team lead
  sign-off)
- Container registry — **must be checked on the real legacy server**, see
  Step 1.1. The rehearsal environment had none configured, so this was
  never actually tested against real registry data.
- Labels, milestones, wikis, snippets, deploy keys, protected tags, attachments — not touched by any script here. If needed, they'd
  need a separate script following the same GET-from-legacy/create-on-new
  pattern as `migrate_metadata.rb`.

### Step 1.1 — Pre-flight checks on the REAL servers (do this first)

```bash
# On the legacy server, check if it's containerized:
docker ps | grep gitlab

# If it's a container (adjust name to match):
docker exec <container_name> gitlab-ctl status | grep registry
docker exec <container_name> grep -i "registry_external_url" /etc/gitlab/gitlab.rb

# If it's a native install:
sudo gitlab-ctl status | grep registry
sudo grep -i "registry_external_url" /etc/gitlab/gitlab.rb
```
If a registry service IS running, this runbook does not cover migrating it
— stop and plan that separately (broadly: enumerate images via the
registry API, then `skopeo copy` each one to the new instance's registry).

Also confirm:
- [ ] Admin/root API tokens for **both** legacy and new instances (Settings
      → Access Tokens, scope: `api`)
- [ ] Which host has network access to *both* GitLab instances (Part A runs
      from here)
- [ ] How to reach the *new* instance's server directly — SSH or console —
      since Part B runs there via `gitlab-rails runner`
- [ ] `git-lfs` is installed on the Part A host: `git lfs version` (install
      via `sudo apt install git-lfs -y` if missing)
- [ ] Rough scope: how many users, groups, projects, and total repo size —
      affects timing and whether you need a maintenance window
- [ ] **New:** get a list of legacy users with `is_admin: true` (via
      `GET /users?admin=true` with an admin token, or the admin UI) so you
      know who to expect as an admin on the new instance and can confirm
      after the run — see Step 6

---

## 2. The three scripts

| File | Runs where | Runs how | Does what |
|---|---|---|---|
| `migrate_gitlab.py` | Host with network access to both GitLab instances | `python3 migrate_gitlab.py` | Users (incl. admin flag) → Groups → Membership → Project shells → Repo mirror (incl. LFS) → Direct project members → Webhooks → Protected branch rules |
| `migrate_metadata.rb` | The NEW instance's server | `sudo gitlab-rails runner migrate_metadata.rb` | Issues → MRs → Comments, with exact original author + timestamp, scoped by `config.yaml`'s `project_allowlist` |
| `validate_migration.py` | Same host as `migrate_gitlab.py` | `python3 validate_migration.py --config config.yaml` | Compares legacy vs new, reports any mismatches |

**Run Part A completely before Part B.** Part B assumes users, groups,
projects, and repo branches already exist on the new instance.

---

## 3. Setup

### 3.1 — Get `migrate_gitlab.py` + `config.yaml` onto the right host

```bash
scp config.yaml migrate_gitlab.py <user>@<host-with-dual-access>:~/
```

Install dependencies there:
```bash
pip install requests pyyaml --break-system-packages
```

### 3.2 — Fill in `config.yaml` with REAL values

The copy in this folder still has placeholder URLs — fill these in for
real before running anything:

```yaml
legacy:
  url: "http://<real-legacy-host>:<port>"
new:
  url: "http://<real-new-host>"
options:
  dry_run: true   # ALWAYS start with this true
```

Set tokens as environment variables instead of in the file (safer):
```bash
export GITLAB_LEGACY_TOKEN="<legacy-admin-token>"
export GITLAB_NEW_TOKEN="<new-admin-token>"
```

If you want to restrict this migration to specific projects, set
`project_allowlist` in `config.yaml` — as of this patched version, this is
the **only** place you need to set it (see 3.3).

### 3.3 — Get `migrate_metadata.rb` onto the new instance's server

```bash
scp migrate_metadata.rb config.yaml <user>@<new-instance-host>:~/
```

**Important — permissions gotcha:** `gitlab-rails runner` internally drops
privileges to the `git` system user. If the script, its state file, or
`config.yaml` live in your personal home directory, it will fail with a
permissions error partway through. Always copy both to `/tmp` and make
them world-readable:
```bash
cp ~/migrate_metadata.rb /tmp/migrate_metadata.rb
cp ~/config.yaml /tmp/config.yaml
chmod 644 /tmp/migrate_metadata.rb /tmp/config.yaml
```

`migrate_metadata.rb` reads `/tmp/config.yaml` by default for
`project_allowlist` (override the path with `CONFIG_YAML_PATH` if you put
it somewhere else). If `/tmp/config.yaml` isn't there, it logs a warning
and defaults to migrating all projects — don't rely on that; copy the file.

**Other configuration:** the script's legacy URL/token still come from
environment variables, and it refuses to run (`abort`) if either is unset:
```bash
export GITLAB_LEGACY_URL="http://<real-legacy-host>:<port>"
export GITLAB_LEGACY_TOKEN="<legacy-admin-token>"
```

---

## 4. Run Part A — dry run first, always

On the host with access to both instances:
```bash
python3 migrate_gitlab.py --dry-run
```
Read the log output (`migration.log`) carefully. Look especially for:
- Any user logged as being created — check against your admin list from
  Step 1.1 whether it should say `(ADMIN)` next to it
- Groups/projects that can't find their parent/namespace yet (usually
  resolves itself once earlier steps have actually run)

When the dry run looks right, run for real:
```bash
python3 migrate_gitlab.py
```
Resumable — tracks progress in `migration_state.json`, safe to re-run.

Steps, in order: `users, groups, membership, projects, mirror, project_members, webhooks,
protected_branches`. You can also run a subset at a time, e.g. to check user creation
before touching anything else:
```bash
python3 migrate_gitlab.py --steps users
```

**Branch protection note:** the mirror step now automatically restores
each project's original branch protection after pushing (or after a
failed/timed-out push) — you shouldn't need to manually re-protect
anything. If the log shows `Failed to re-protect branch ... — re-apply
protection manually` for a specific project, that one needs a manual
follow-up; everything else should come back protected automatically.

---

## 5. Run Part B — dry run first, always

On the new instance's server:
```bash
export GITLAB_LEGACY_URL="http://<real-legacy-host>:<port>"
export GITLAB_LEGACY_TOKEN="<legacy-admin-token>"
DRY_RUN=true sudo -E gitlab-rails runner /tmp/migrate_metadata.rb
```
If `-E` doesn't preserve env vars through `sudo` (some systems block it),
pass them inline instead:
```bash
sudo GITLAB_LEGACY_URL="http://<real-legacy-host>:<port>" GITLAB_LEGACY_TOKEN="<legacy-admin-token>" DRY_RUN=true gitlab-rails runner /tmp/migrate_metadata.rb
```

Read through the output. Expect some MRs to be skipped with a `references
missing branch(es)` warning — normal if the source branch was already
deleted on legacy (e.g., auto-deleted after merge). Verify against legacy's
own branch list if unsure:
```bash
curl -s --header "PRIVATE-TOKEN: $GITLAB_LEGACY_TOKEN" \
  "<legacy-url>/api/v4/projects/<project-id>/repository/branches" | jq -r '.[].name'
```

---

## 6. Run Part B for real

```bash
sudo GITLAB_LEGACY_URL="http://<real-legacy-host>:<port>" GITLAB_LEGACY_TOKEN="<legacy-admin-token>" gitlab-rails runner /tmp/migrate_metadata.rb
```

Also resumable — tracks progress in `/tmp/metadata_migration_state.json`.

**What happens on failure now (this is the fixed behavior):**
- **If an issue's own creation fails:** the issue isn't marked done, so a
  re-run correctly retries just that issue. Unchanged from before.
- **If a comment on an issue or MR fails to save:** this is now handled
  per-comment. The failure is logged (`Failed to create note ... — issue/MR
  itself is fine`) and the script **keeps going** — it no longer crashes
  the run, and the parent issue/MR is never re-created on a later run,
  because its completion state was already saved before its comments were
  touched. You may end up with an issue/MR that's missing one comment; the
  log tells you exactly which one, and re-running will pick that comment
  back up (or it can be added by hand).
- **No more duplicate issues or MRs** from this failure mode. If you *do*
  see a duplicate for any other reason, it's a new bug, not this one —
  worth reporting/investigating rather than assumed-safe.

### After it finishes — spot checks
- Open an issue/MR that existed on legacy, compare its author and creation
  date on the new instance against the original
- Open a couple of comments on an MR — same check
- **New:** check the new instance's admin list (Admin Area → Users, filter
  by "Admin") against the list you pulled in Step 1.1. Every legacy admin
  should appear here now automatically — if any are missing, promote them
  by hand and note it for follow-up on why the flag didn't come through
  (most likely: the legacy `GET /users` call didn't return `is_admin` for
  that account — check the token used has admin scope)

---

## 7. Final verification pass — automated

```bash
scp validate_migration.py <user>@<host-with-dual-access>:~/
python3 validate_migration.py --config config.yaml
```

This checks, and prints ✅/❌ for each:
- Every legacy user exists on new
- Every legacy group (with correct nested path) exists on new
- Every project exists on new
- Issue count matches exactly, per project
- MR count matches or is fewer on new, per project (fewer — including
  zero — is only ever flagged if new has *more* than legacy, which should
  never happen; this was itself a bug in the original validator that's
  fixed in this copy)
- Commit count matches exactly, per project, on that project's actual
  default branch (auto-detected, not hardcoded to `master`)

Faster, API-only pass (skips the slow commit-count clone-and-compare):
```bash
python3 validate_migration.py --config config.yaml --skip-commit-check
```

**If anything comes back ❌:** the printed detail tells you exactly what
mismatched (e.g. `legacy=12 new=11`) — go back to the relevant migration
step and re-run it; all scripts are idempotent and safe to re-run.

**A full ✅ pass does not mean the migration is complete.** The validator
only checks users, groups, projects, issue counts, MR counts and commit
counts. It has **no coverage at all** for:
- Webhooks and their secret tokens (secrets can never be read back from
  legacy — re-enter them by hand)
- Protected branch rules
- Labels, milestones, wikis, snippets, deploy keys, protected tags
- Issue/MR attachments (`/uploads/...` links in migrated text will be
  broken)
- SSH keys and 2FA enrollment (not migrated by any script — every user
  must re-add these themselves)
- Closed/merged timestamps and assignees on issues/MRs (best-effort, only
  applied to items created in that same run — grep the Part B log for
  `enrichment failed`)

Treat "62/62 checks passed" as "the core data moved correctly," and work
through this list separately before treating the migration as finished.

---

## 7.5. Keeping new in sync if legacy stays live after migration

Nothing here runs continuously. If people keep using legacy after the
migration (new users, new groups, new projects, new commits/branches on
already-migrated projects), new will silently fall behind — the scripts
only look for `state`-marked-done vs not, and won't notice legacy content
changing under an already-mirrored project on their own.

`sync_delta.py` closes that gap for existence + repo drift (it does NOT
touch issues/MRs/comments — see below):

```bash
python3 sync_delta.py --config config.yaml            # report only, safe to run anytime
python3 sync_delta.py --config config.yaml --fix      # clears stale state entries
python3 migrate_gitlab.py --dry-run                   # confirm exactly what will run
python3 migrate_gitlab.py                             # do it
```

Read the "mirrors" list in the report before using `--fix` — re-mirroring
force-pushes, so it will destroy any commits pushed directly to a project
on **new** since the migration. `sync_delta.py` never proposes deleting
anything, and it does not detect content deleted on legacy (only new or
changed content).

For new issues/MRs/comments on legacy, no drift-detection is needed — just
re-run Part B directly, it's already incremental and safe to repeat:
```bash
cp migrate_metadata.rb config.yaml /tmp/ && chmod 644 /tmp/migrate_metadata.rb /tmp/config.yaml
sudo GITLAB_LEGACY_URL="<legacy URL confirmed working — see §0.2>" GITLAB_LEGACY_TOKEN="$GITLAB_LEGACY_TOKEN" \
  gitlab-rails runner /tmp/migrate_metadata.rb
```

---

## 8. Rollback / safety notes

- Nothing in this process modifies or deletes anything on the **legacy**
  instance — it's all read + push-to-new. Legacy stays intact and usable
  as a fallback throughout.
- If something goes wrong on the new instance mid-migration, deleting the
  affected project(s) in the UI is not enough by itself — see §0.2. You
  must also clear that project's entries from both state files (or reset
  them entirely for a full redo), or the scripts will believe the deleted
  data still exists and skip recreating it. Always back up a state file
  (`cp migration_state.json migration_state.json.bak-$(date +%s)`) before
  editing or deleting it.
- Keep both instances' API tokens rotated after the migration is done —
  don't leave long-lived admin tokens active longer than needed.
- New instance admins created via Step 4/6's fix have full instance-admin
  rights immediately — make sure that list is exactly right before/soon
  after the run; don't leave it to a "check later."

---

## 9. Who to contact / what's already been decided

- CI/CD migration: confirmed **not needed** (team lead sign-off)
- Container registry: **must be checked on the real legacy server**
  (Step 1.1) — never actually validated against real registry data
- No SSO/LDAP users involved — plain username/email recreation is
  sufficient
- Instance admin flag: legacy admins are now recreated as admins
  automatically (Step 4) — confirm the resulting list matches expectations
  (Step 6)
- Labels and milestones: **not migrated** by anything here — confirm with
  the team whether they're in scope; would need a separate script
