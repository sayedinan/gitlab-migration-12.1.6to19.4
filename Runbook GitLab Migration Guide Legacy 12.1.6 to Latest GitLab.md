# GitLab Migration Guide: Legacy 12.1.6 to Latest GitLab

A complete, copy-and-paste guide to migrating users, groups, repositories, and issue and merge request history from a legacy GitLab CE 12.1.6 instance to a current GitLab. It was written from a full rehearsal and includes the mistakes that were made along the way, so you can avoid them. Follow the steps in order, and do not skip the dry runs.

**About the scripts:** this guide assumes the patched package (version 2). Besides the original steps it adds forks, labels, milestones, SSH keys and snippets to Part A, copies labels and milestones onto issues and merge requests and recreates merge requests whose source branch was deleted in Part B, and has a validator that checks all of it. If you use the original package, treat those items as manual work (section 10.2) and expect fewer merge requests on the new instance.

## 1. Read this first

### What the migration does

- **Part A (`migrate_gitlab.py`, Python, uses the API and git):** users, groups with the correct hierarchy, group membership, project shells, full repository history (all branches and tags, including Git LFS content), forks (recreated as real forks of their upstream), direct project members, labels, milestones, webhooks, protected branches, users' SSH keys, and personal and project snippets.
- **Part B (`migrate_metadata.rb`, Ruby, runs inside the new GitLab's Rails):** issues, merge requests, and comments, keeping the exact original author and creation time, with labels, milestones and assignees attached. Merge requests whose source branch was deleted on legacy are recreated using a temporary branch.
- **Validation (`validate_migration.py`):** compares legacy and new for users, groups, projects, issue counts, MR counts, commit counts, comments, labels, milestones, snippets, SSH keys and fork links.
- **Wiki migration (`migrate_wikis.py`, Python, run separately):** copies each project's wiki with its full page history and authors (section 10.4).
- **Link fix (`fix_links.py` and `fix_links_metadata.rb`, run last):** replaces addresses of the legacy server that are written inside READMEs, docs, issues and comments, so links on the new instance stay on the new instance (section 10.3).

### Why a custom path was needed

GitLab's built-in project export/import does not work between 12.1.6 and a current version (it fails with "invalid import format"), and the REST API cannot set both the author and the creation date of an issue. So repositories are copied with git, and issues/MRs are recreated directly through Rails.

### What is NOT migrated (plan to handle these by hand)

- Webhook secret tokens (GitLab never lets you read them back; re-enter them on the new instance).
- 2FA enrollment (each user must re-enroll). SSH keys are migrated, but a key the new server rejects (duplicate, too short or an unsupported type) is only logged, so that user must re-add it.
- Issue and MR attachments (`/uploads/...` links in migrated text will be broken).
- Deploy keys and protected tags. Wikis are copied only by the separate migrate\_wikis.py script (section 10.4).
- Links written as full legacy addresses (for example `http://180.210.129.233:8090/...`) inside READMEs, docs, issues and comments are copied unchanged and lead back to legacy until the link fix in section 10.3 is run.
- CI runners and pipeline history (confirmed out of scope).
- Container registry images (the rehearsal legacy had no registry, so check the real one in Step 4).
- Original creation dates of snippets and SSH keys (the API cannot set them), and system-generated activity notes on issues and MRs (real comments are migrated).
- Merge requests opened from a fork into its upstream are only recreated if their source branch exists in the upstream.

### Golden rules

1. **Always do a dry run before a real run.**
2. **Run each script only once at a time.** Starting the same command twice (even in the background) makes both copies write to the same state file and creates duplicate issues. This happened in the rehearsal.
3. **Never delete data through the web UI and then re-run.** The scripts trust their own state files, not the database (see section 12).
4. **Nothing in this process changes the legacy server.** It is only read, so legacy stays usable as a fallback.
5. **Do not press Ctrl+C or Ctrl+Z on a running migration.** `gitlab-rails runner` takes 1 to 3 minutes to boot with no output, which is normal.

## 2. The two servers and what runs where

| Role | Example from the rehearsal | Access |
| --- | --- | --- |
| Legacy GitLab 12.1.6 (Docker) | `180.210.129.233`, UI/API on port `8090`, SSH on `2222` | `http://180.210.129.233:8090` |
| New GitLab (package install) | `192.16.0.65` | user `gitlab`, SSH port `10022`, UI `http://192.16.0.65/` |

Replace these with the real hostnames and ports. **Run everything on the new GitLab server**, because:

- Part A needs network access to both instances.
- Part B needs `gitlab-rails`, which only exists on the new instance (on the legacy Docker host you will get `gitlab-rails: command not found`).

## 3. What you need before starting

- A PC with the migration package (folder `Real Migration` containing `migrate_gitlab.py`, `migrate_metadata.rb`, `validate_migration.py`, `sync_delta.py`, `config.yaml`, `REAL_MIGRATION_RUNBOOK.md`).
- Also in that folder: the two link-fix scripts, `fix_links.py` and `fix_links_metadata.rb` (section 10.3).
- Also in that folder: `migrate_wikis.py`, which copies the wikis (section 10.4).
- **Do not copy the folder `_demo_only_DO_NOT_COPY`.** It holds the fake-data seeding scripts used only for the rehearsal.
- SSH access to the new server with `sudo` rights.
- An admin API token on legacy and an admin API token on the new instance (Step 5).
- Network access: the new server must reach the legacy URL, and the legacy server's firewall must allow it.
- Free disk space on the new server. The mirror step clones several repositories at the same time under `/tmp/gitlab-migration-mirror`, so leave room for roughly the 4 largest repos at once, plus the final size of the new instance (about 70 GB of data in the real migration).
- Software on the new server: `python3`, `pip`, `git`, `git-lfs`, `curl`, and optionally `jq`.
- A maintenance window if legacy will keep being used. Anything pushed to legacy after the mirror step needs a sync pass (section 11).

## 4. Pre-flight checks on the REAL servers (do not skip)

### 4.1 Confirm legacy is reachable from the new server

On the new server:

```bash
curl -sI http://180.210.129.233:8090/users/sign_in | head -1
```

You should see `200` or `302`. If you get a timeout, fix the firewall or routing first.

### 4.2 Check whether legacy has a container registry

On the legacy host (adjust the container name if different):

```bash
docker ps --filter name=gitlab-legacy
docker exec gitlab-legacy grep -n "registry_external_url" /etc/gitlab/gitlab.rb
```

If the line is commented out (starts with `#`), there is no registry. If it is active, registry images are NOT migrated by these scripts and need a separate plan.

### 4.3 Record the legacy admin users

Legacy administrators are recreated as administrators on the new instance. Pull the list now so you can compare it later:

```bash
curl -s -H "PRIVATE-TOKEN: $GITLAB_LEGACY_TOKEN" \
  "http://180.210.129.233:8090/api/v4/users?per_page=100" | \
  python3 -c "import sys,json; [print(u['username'], u.get('is_admin')) for u in json.load(sys.stdin)]"
```

(Run it after you create the token in Step 5.)

### 4.4 Check disk space

```bash
df -h /tmp /var/opt/gitlab
```

## 5. Create the two API tokens

Both tokens must belong to an administrator and have the **`api`** scope.

### Legacy token (GitLab 12.1.6)

1. Log in to the legacy UI as an administrator.
2. Click your avatar, then **Settings**, then **Access Tokens**.
3. Name it `migration`, set an expiry date, tick **api**, and click **Create personal access token**.
4. Copy the token immediately (it is shown only once).

If you cannot log in (for example you lost the root password), create one from inside the container:

```bash
docker exec -it gitlab-legacy gitlab-rails runner "
u = User.find_by_username('root')
t = u.personal_access_tokens.build(name: 'migration', scopes: [:api], expires_at: 1.year.from_now)
t.set_token('REPLACE-WITH-A-LONG-RANDOM-STRING')
t.save!
puts 'ok'
"
```

Generate the random string with `openssl rand -hex 20`. That string is your token.

### New token (current GitLab)

1. Log in to the new GitLab as an administrator (`root`).
2. Avatar, **Edit profile** (or **Preferences**), **Access tokens**, **Add new token**.
3. Name it `migration`, set an expiry, tick **api**, and create it. Copy it.

### Test both tokens (important)

A revoked or wrong token gives `401 invalid_token` on every call, and the scripts will happily print errors while creating nothing.

```bash
read -rs GITLAB_LEGACY_TOKEN; export GITLAB_LEGACY_TOKEN
read -rs GITLAB_NEW_TOKEN;    export GITLAB_NEW_TOKEN

curl -s -H "PRIVATE-TOKEN: $GITLAB_LEGACY_TOKEN" http://180.210.129.233:8090/api/v4/user | head -c 200; echo
curl -s -H "PRIVATE-TOKEN: $GITLAB_NEW_TOKEN"    http://192.16.0.65/api/v4/user | head -c 200; echo
```

Both must print an account (`"username":"root"` or similar), not a `401` message.

## 6. Copy the package from your PC to the new server

Run these on **your PC**. The folder name contains a space, so quote it.

### 6.1 Make a clean zip (leave out the demo scripts)

Linux or macOS:

```bash
zip -r Real_Migration.zip "Real Migration" -x "*/_demo_only_DO_NOT_COPY/*"
```

On Windows, copy the folder, delete `_demo_only_DO_NOT_COPY` from the copy, then right-click and choose **Send to, Compressed (zipped) folder**.

### 6.2 Upload it

Note the capital **-P** for the port with `scp`:

```bash
scp -P 10022 Real_Migration.zip gitlab@192.16.0.65:~/
```

### 6.3 Unpack it on the server

```bash
ssh -p 10022 gitlab@192.16.0.65

unzip -o Real_Migration.zip
mv "Real Migration" real_migration
cd real_migration
ls
```

You should see `config.yaml`, `migrate_gitlab.py`, `migrate_metadata.rb`, `validate_migration.py`, `sync_delta.py`, and `REAL_MIGRATION_RUNBOOK.md`. If `unzip` is missing, run `sudo apt install unzip`.

## 7. Install dependencies and fill in the configuration

### 7.1 Install

```bash
sudo apt update
sudo apt install -y python3-pip git git-lfs jq curl
git lfs install
pip install requests pyyaml --break-system-packages
```

### 7.2 Edit config.yaml

```bash
nano config.yaml
```

Set at least these values (keep tokens OUT of the file; use the environment variables from Step 5):

```yaml
legacy:
  url: "http://180.210.129.233:8090"
new:
  url: "http://192.16.0.65"

options:
  dry_run: true            # keep TRUE until the real run (see 8.2)
  temp_password: "ChangeMe123!"
  force_password_reset: true
  mirror_parallel_jobs: 4
  mirror_work_dir: "/tmp/gitlab-migration-mirror"

project_allowlist: []      # empty means migrate everything
user_skiplist:
  - "ghost"
  - "support-bot"
```

To migrate only some projects, list their full paths under `project_allowlist` (for example `"company/engineering/backend/order-api"`). This one setting is shared by Part A, Part B, and the validator.

**Check the URL actually works.** `config.yaml` (`legacy.url`) and the `GITLAB_LEGACY_URL` environment variable used by Part B are separate settings. Make sure both point at the address you tested in Step 4.1, including the correct port and `http` vs `https`.

## 8. Part A: users, groups, projects, repositories

Make sure the tokens are set in this shell (Step 5).

### 8.1 Dry run

```bash
cd ~/real_migration
python3 migrate_gitlab.py --dry-run
```

Read the output and `migration.log`. Check that:

- The user list looks right, and administrators are marked `(ADMIN)`.
- The groups and projects are the ones you expect.
- Lines such as `[ERROR] API error 404 on GET /groups/...` are normal. The script looks up each item first, and a 404 only means it does not exist yet. It is only a problem if no `Created ...` line follows it.

### 8.2 Real run

The script treats `options.dry_run: true` in `config.yaml` as a dry run even without the flag. **Before the real run, change it to false**:

```bash
sed -i 's/dry_run: true/dry_run: false/' config.yaml
grep dry_run config.yaml
```

Then start it once, and wait for it to finish:

```bash
python3 migrate_gitlab.py
```

The steps run in this order: `users`, `groups`, `membership`, `projects`, `mirror`, `forks`, `project_members`, `labels`, `milestones`, `webhooks`, `protected_branches`, `ssh_keys`, `snippets`. If the output does NOT say `Running in DRY-RUN mode`, it is a real run. For a large mirror step (about 70 GB), consider running it inside `tmux` or `screen` so an SSH drop does not interrupt it.

What good output looks like: `Created user ...`, `Created group ...`, `Created project ...`, `Mirrored <path>` for every project, and a final `Done.` Progress is saved in `migration_state.json`, so if it stops partway you can run the same command again and it continues.

Notes learned in the rehearsal:

- Projects that use Git LFS have their LFS content fetched and pushed before the branches. This is automatic and needs `git-lfs` installed.
- The script temporarily unprotects the default branch for the push and restores the protection afterwards. If it prints `Failed to re-protect branch`, re-protect that branch by hand in the UI.
- Only branches and tags are copied (not GitLab's hidden merge request refs).
- A project that is a fork on legacy is not created in the `projects` step. The `forks` step creates it as a fork of its upstream after the upstream has been mirrored, waits for the fork to finish, then pushes the fork's own branches. The upstream must be in scope (`project_allowlist`).
- Labels and milestones are copied before Part B on purpose, because Part B attaches them to issues and MRs. Finish Part A completely before Part B.
- SSH key and snippet failures are logged as `ERROR` lines and do not stop the run. Typical causes: the same key already belongs to another user, a key type the new server rejects, or a snippet owned by a blocked user.

### 8.3 Run a single step only

```bash
python3 migrate_gitlab.py --steps users,groups
```

To run only the later steps (for example after patching the scripts, on an instance where the earlier steps are already done):

```bash
python3 migrate_gitlab.py --dry-run --steps forks,labels,milestones,ssh_keys,snippets
python3 migrate_gitlab.py --steps forks,labels,milestones,ssh_keys,snippets
```

A project that already exists as a standalone copy of a fork must be deleted (and removed from the state file, section 12) before the `forks` step can recreate it as a fork.

## 9. Part B: issues, merge requests, comments

Run this on the new GitLab server (not on the legacy Docker host).

### 9.1 Put the script in /tmp

`gitlab-rails runner` runs as the `git` system user, which cannot read your home directory. Always run the script from `/tmp`:

```bash
cp ~/real_migration/migrate_metadata.rb /tmp/migrate_metadata.rb
cp ~/real_migration/config.yaml /tmp/config.yaml
chmod 644 /tmp/migrate_metadata.rb /tmp/config.yaml
```

If you edit `migrate_metadata.rb` later, edit the copy in your home folder and copy it to `/tmp` again, so you do not lose changes.

### 9.2 Set the legacy connection

```bash
export GITLAB_LEGACY_URL="http://180.210.129.233:8090"
export GITLAB_LEGACY_TOKEN="<your legacy token>"
```

The script aborts if either is missing.

### 9.3 Make sure no old state file exists

```bash
ls -l /tmp/metadata_migration_state.json
```

If it says `No such file`, you are clean. If it exists from an earlier or failed run, read section 12 before continuing.

Part A must be fully finished first, including the `labels` and `milestones` steps, because Part B attaches them to issues and merge requests. If you add them later, redo Part B (section 12) so existing issues pick them up.

### 9.4 Dry run

On many servers `sudo -E` is blocked, so pass the variables inline:

```bash
sudo GITLAB_LEGACY_URL="$GITLAB_LEGACY_URL" \
     GITLAB_LEGACY_TOKEN="$GITLAB_LEGACY_TOKEN" \
     DRY_RUN=true \
     gitlab-rails runner /tmp/migrate_metadata.rb
```

It will look frozen for 1 to 3 minutes while Rails starts. **Wait; do not press Ctrl+C.** A good dry run lists the issues and merge requests it would create for every project. For a merge request whose source branch was deleted on legacy it prints `source branch missing, would recreate it temporarily`; only a merge request whose target branch is missing is skipped with a warning.

### 9.5 Real run (once, in the foreground)

```bash
sudo GITLAB_LEGACY_URL="$GITLAB_LEGACY_URL" \
     GITLAB_LEGACY_TOKEN="$GITLAB_LEGACY_TOKEN" \
     gitlab-rails runner /tmp/migrate_metadata.rb 2>&1 | tee /tmp/meta_real.log
```

Do not add `&` and do not start a second copy. Wait for the final `Done.` line. Good output is a long list of `Created issue <path>#N -> new iid N` and `Created MR ...` lines. Progress is saved in `/tmp/metadata_migration_state.json`, and a re-run only fills in what is missing.

Then check the log for problems:

```bash
grep -c "Created issue" /tmp/meta_real.log
grep -c "Created MR" /tmp/meta_real.log
grep "ERROR" /tmp/meta_real.log | head
grep "enrichment failed" /tmp/meta_real.log | head
```

You should see no `marked done but not found` errors.

Expect `recreated missing source branch ... temporarily` lines for merge requests whose branches were deleted on legacy. If the original commit is not in the new repository, the merge request is created from the target branch head, so its diff looks empty, but its author, dates and comments are kept. Count them with `grep -c "recreated missing source branch" /tmp/meta_real.log`.

## 10. Validate

```bash
cd ~/real_migration
python3 validate_migration.py --config config.yaml
```

For a faster check that skips the slow commit-count comparison:

```bash
python3 validate_migration.py --config config.yaml --skip-commit-check
```

The validator prints a pass or fail for each check and exits with code 0 only if everything matches. It checks:

- every legacy user exists on the new instance,
- every group exists with the correct nested path,
- every project exists,
- issue counts match exactly per project,
- merge request counts match exactly per project (merge requests with a deleted source branch are recreated, so any difference is a real problem),
- commit counts match exactly on each project's default branch,
- comment counts per project (real comments, not system notes) match,
- every legacy label and milestone exists on the new project,
- project snippet counts, and each user's personal snippet and SSH key counts, match,
- migrated forks are linked to their upstream.

If a check fails, the output shows the numbers (for example `legacy=12 new=11`). Re-run the relevant step; the scripts are safe to repeat.

**A full pass covers users, groups, projects, issues, merge requests, comments, labels, milestones, snippets, SSH keys and fork links. It does not cover webhooks, protected branches, wikis, attachments or 2FA, so do the manual checks below.** In the rehearsal the original validator passed 54 of 54 checks while snippets, SSH keys and milestones were missing entirely, which is why the extra checks exist.

### 10.1 Manual spot checks in the new UI

- Admin Area, Users: the admins match the list from Step 4.3.
- Open a few issues and merge requests: the author and the creation date match legacy, and the comments are present.
- Open an issue that had a label and a milestone on legacy: both are attached.
- Open the commit history of a few projects: the dates and authors match.
- Open a Git LFS project and download one LFS file.
- Open a forked project: it shows the "forked from" link to its upstream.
- Check the group tree and project membership against legacy.
- Check protected branches (Settings, Repository) on a couple of projects.
- Check the webhooks list.
- Check one user's SSH keys and one personal snippet.

### 10.2 Manual follow-up work

- Re-enter the secret token of every webhook. Also point each webhook at the new server if it still targets legacy.
- Ask every user to log in with the temporary password (`ChangeMe123!` unless you changed it), set a new password, re-enable 2FA, and re-add any SSH key the migration log reported as failed.
- Recreate deploy keys and protected tags if you need them. Copy the wikis with section 10.4.
- Run the link fix (section 10.3) as the last data step, after any final sync.
- Update team documentation, CI settings, and any `git remote` URLs (`git remote set-url origin <new url>`).
- Revoke both migration tokens when everything is done.

### 10.3 Fix links that still point to legacy

Links written as full legacy addresses, such as `http://180.210.129.233:8090/company/qa/test-framework/issues`, are copied as written, so on the new instance they lead back to legacy. Two scripts rewrite them. Run them as the very last data step: after validation, after any final sync (section 11), and again after any later Part B run.

| Script | Where it runs | What it rewrites |
| --- | --- | --- |
| `fix_links.py` | Any machine that can reach the new GitLab | Files on each project's default branch: README, `.md`, `.markdown`, `.txt`, `.rst`, `.adoc`, `.html` and `.gitmodules`. One new commit per project; history is not rewritten. |
| `fix_links_metadata.rb` | The new GitLab server, through `gitlab-rails runner` | Issue descriptions, merge request descriptions, comments and milestone descriptions. Authors and `updated_at` stay as they are. |

Both scripts do a dry run unless you ask for the real run, and both replace every address in `LEGACY_BASES` with the new address, longest address first.

**Repository files**

```bash
export GITLAB_NEW_URL=http://192.16.0.65
export GITLAB_NEW_TOKEN=<your new token>
export LEGACY_BASES="http://180.210.129.233:8090,http://180.210.129.233"

python3 fix_links.py --project company/qa/test-framework   # dry run, one project
python3 fix_links.py                                       # dry run, all projects
python3 fix_links.py --apply                               # commit for real
```

The dry run lists each file and how many links it would change. Check one project before using `--apply`. If a commit is rejected because you may not push to the default branch, allow push temporarily and run it again; projects that are already fixed are skipped.

**Issues, merge requests, comments and milestones**

```bash
cp ~/real_migration/fix_links_metadata.rb /tmp/ && chmod 644 /tmp/fix_links_metadata.rb

# dry run
sudo LEGACY_BASES="http://180.210.129.233:8090,http://180.210.129.233" \
     NEW_BASE="http://192.16.0.65" \
     gitlab-rails runner /tmp/fix_links_metadata.rb

# real run
sudo LEGACY_BASES="http://180.210.129.233:8090,http://180.210.129.233" \
     NEW_BASE="http://192.16.0.65" APPLY=1 \
     gitlab-rails runner /tmp/fix_links_metadata.rb
```

It prints one line per record type with the number of records it would change, or did change. It is safe to repeat.

**Limits to know about**

- Run it last. A re-mirror (section 11, `--fix`) replaces the repositories and removes the link-fix commit, so run `fix_links.py` again afterwards. A Part B re-run brings in new issues that still carry legacy links, so run the metadata script again too.
- It adds one commit per project, so the commit counts on the new instance are one higher than on legacy. Run `validate_migration.py` before this step, not after.
- Only the default branch is fixed. Other branches keep their old links.
- Only `http` addresses are replaced. `ssh://` and `git@` clone URLs are not touched.
- Wiki pages are fixed only with --wikis or --wikis-only, after the wikis have been copied (section 10.4).

**Check it worked:** open a project's README on the new instance and click its Issues, Wiki and docs links. The address bar must stay on the new server. A wiki link shows an empty wiki until wikis are migrated.

### 10.4 Migrate wikis

Wikis are separate git repositories (`<project>.wiki.git`), so the main scripts never copied them. `migrate_wikis.py` copies each one with its full page history and authors, and changes nothing on legacy. For every project that has wiki pages on legacy it mirror-clones the legacy wiki, pushes it to the same project path on the new instance, and compares the page counts. Projects without wiki pages are skipped, and every project must already exist on the new instance (Part A).

Run it on the new server, from `~/real_migration`:

```bash
export GITLAB_LEGACY_URL="http://180.210.129.233:8090"
export GITLAB_LEGACY_TOKEN=<your legacy token>
export GITLAB_NEW_URL="http://192.16.0.65"
export GITLAB_NEW_TOKEN=<your new token>

python3 migrate_wikis.py --project company/qa/test-framework           # dry run, one project
python3 migrate_wikis.py                                               # dry run, all projects
python3 migrate_wikis.py --apply --project company/qa/test-framework   # copy one project
python3 migrate_wikis.py --apply                                       # copy everything
python3 migrate_wikis.py --verify                                      # compare page counts, change nothing
```

Copy one project first and open its Wiki on the new instance before running the rest. `validate_migration.py` does not check wikis, so use `--verify`: it prints one line per project and exits with an error if any count differs or a project is missing on the new instance.

**Things to know**

- Legacy wikis use the branch `master` and the new instance uses `main`. The script pushes into whatever the new wiki's default branch is. If the push is refused because the new wiki does not exist yet, it creates a placeholder page through the API and pushes over it.
- Progress is saved in `wiki_migration_state.json`. Wikis already copied are skipped, and so is a new wiki that already has pages. `--force` copies anyway and **overwrites the new wiki**, including edits made on the new instance.
- If people keep editing wikis on legacy, run it again with `--apply --force` at the final sync (section 11).
- Files uploaded through the wiki editor come across with the repository. Images pasted into issue and merge request text do not (section 1).
- Migrated pages still contain legacy links. Fix them with `python3 fix_links.py --wikis-only --apply` (section 10.3), which updates changed pages one commit at a time. It can run any time after the wikis are copied, and again after a re-copy.

## 11. If legacy is still being used after the migration

Nothing syncs automatically. To catch up on changes made on legacy (new users, groups, projects, and commits or branches in projects already mirrored):

```bash
cd ~/real_migration
python3 sync_delta.py --config config.yaml           # report only; safe any time
python3 sync_delta.py --config config.yaml --fix     # clears stale state entries
python3 migrate_gitlab.py --dry-run                  # confirm what will run
python3 migrate_gitlab.py                            # run it (set dry_run: false first)
```

Read the `mirrors` list before using `--fix`: re-mirroring force-pushes and **destroys any commit pushed directly to the new instance** since the migration.

For new issues, MRs, and comments, simply run Part B again (section 9.5). It is incremental and does not duplicate what already exists.

If you have already run the link fix (section 10.3), run it again after every sync pass. A re-mirror removes the link-fix commit, and new issues brought in by Part B still carry legacy links.

A good cutover plan: freeze legacy (stop pushes), do a final sync pass, validate, run the link fix (section 10.3), then switch users over.

## 12. Redoing or undoing a run safely

### The key concept

`migration_state.json` (Part A) and `/tmp/metadata_migration_state.json` (Part B) are what the scripts use to decide whether something "already ran". They do not look at the GitLab database. If you delete data through the UI and re-run, the script trusts the state file and silently skips recreating it. After deleting data, always reset the matching state file too, and back it up first:

```bash
cp migration_state.json migration_state.json.bak-$(date +%s)
cp /tmp/metadata_migration_state.json /tmp/metadata_migration_state.json.bak-$(date +%s)
```

### Redo only Part B (issues and MRs)

Use this if Part B ran more than once at the same time, or it left duplicates. First make sure nothing is running:

```bash
ps aux | grep [m]igrate_metadata
sudo pkill -f migrate_metadata.rb      # only if the line above printed something
```

List the projects and the numbers on the new instance, and check the project IDs:

```bash
sudo gitlab-rails runner '
Project.order(:id).each do |p|
  puts "#{p.id}\t#{p.full_path}\tissues=#{p.issues.count}\tmrs=#{p.merge_requests.count}"
end
'
```

Create `/tmp/wipe_meta.rb`, replacing `44..55` with the real ID range of ONLY the migrated projects (do not wipe projects that are not part of the migration):

```bash
cat > /tmp/wipe_meta.rb <<'EOF'
Project.where(id: 44..55).each do |p|
  p.issues.each(&:destroy)
  p.merge_requests.each(&:destroy)
  puts "cleaned #{p.full_path}"
end
InternalId.where(project_id: 44..55).delete_all
puts "Done."
EOF
chmod 644 /tmp/wipe_meta.rb

sudo gitlab-rails runner /tmp/wipe_meta.rb
sudo rm -f /tmp/metadata_migration_state.json
ls -l /tmp/metadata_migration_state.json     # must say: No such file
```

Then run section 9.5 once.

The `InternalId` line resets GitLab's per-project issue and MR counters, so the numbers restart at 1. Without it, a redo continues from the old numbers (issue `#5` instead of `#1`); the data, authors and dates are still correct, but any `#N` written inside issue text would point at the wrong issue.

### Redo Part A for a project

Delete the project in the UI, then remove its entry from `migration_state.json` (the `projects` and `mirrors` sections), or delete the whole file for a full redo, and run `python3 migrate_gitlab.py` again. Ask for help before hand-editing state if you are unsure.

To redo a fork: delete the fork project in the UI, remove its path from the `projects`, `mirrors` and `forks` sections of `migration_state.json`, then run `python3 migrate_gitlab.py --steps forks`. Labels, milestones, SSH keys and snippets check the new instance before creating anything, so those steps can simply be run again.

### Rollback

Nothing touches legacy. To roll back, simply stop using the new instance (or delete the migrated projects there) and keep working on legacy.

## 13. Troubleshooting

- **`401 invalid_token ... Token was revoked`:** the token was revoked or expired (this often happens after the instance was reset). Create a new token (Step 5) and test it with `curl` before running anything.
- **`404 User with ID or username '...' Not Found` while seeding or acting as users:** the users do not exist on that instance. Create the users first. (This was a rehearsal-only problem from the seed scripts; the real migration does not use them.)
- **`python3 migrate_metadata.rb` gives `SyntaxError`:** the file is Ruby, not Python. Run it with `gitlab-rails runner` as in section 9.
- **`gitlab-rails: command not found`:** you are on the wrong host. Run Part B on the new GitLab server.
- **`sudo: preserving the entire environment is not supported, '-E' is ignored`:** pass the variables inline, as shown in section 9.
- **The runner seems to hang or prints a huge `Interrupt` stack trace:** it was loading Rails and someone pressed Ctrl+C. Rerun it and wait 1 to 3 minutes.
- **Script says `CHANGE_ME` or aborts:** `GITLAB_LEGACY_URL` or `GITLAB_LEGACY_TOKEN` is not set in the command.
- **Permission errors partway through Part B:** the script or config is not in `/tmp`, or is not world-readable. Redo section 9.1.
- **`marked done but not found on new instance`:** a stale state file. Back it up and reset it as in section 12.
- **Duplicate issues or `Another open merge request already exists for this source branch`:** Part B was started more than once at the same time. Stop everything and redo Part B as in section 12.
- **`undefined method '[]' for true (NoMethodError)` in Part B:** the state file is from an older script version. Back it up, compare what exists against legacy, then reset it. Do not just re-run.
- **Mirror push rejected:** the default branch was still protected. The script handles it; if it fails for one project, unprotect the branch in the UI and re-run Part A.
- **LFS errors:** make sure `git-lfs` is installed on the server (`git lfs version`).
- **Dry run did something real, or the real run did nothing:** check `options.dry_run` in `config.yaml`. `true` means dry run even without `--dry-run`.
- **A few merge requests are missing:** the target branch is missing on the new instance, or the recovery step could not find a usable commit. Branches deleted on legacy are normally recreated temporarily, so a missing merge request now needs investigation: search the Part B log for the MR's number.
- **One-off `500 Internal Server Error` from the legacy API:** retry; if it persists for one item, fix that item by hand.
- **`Failed SSH key ...` in Part A:** the key already belongs to another account, is too short, or is a type the new server does not allow. Ask that user to add a new key.
- **`Failed personal snippet ...`:** the owner is blocked or missing on the new instance (Sudo fails for blocked users). Recreate the snippet by hand.
- **`Upstream ... missing on new` in the forks step:** the upstream project has not been created and mirrored yet. Run the `projects` and `mirror` steps first, then `--steps forks`.
- **Issues have no labels or milestones on new:** the `labels` and `milestones` steps ran after Part B. Run them, then redo Part B (section 12).
- **Validator shows `Comments`, `Milestones` or `Snippets` mismatches:** read the numbers in the detail text, run the matching Part A step again (they are safe to repeat), and redo Part B if the difference is in comments.
- **Links on the new instance open the legacy server:** the link fix (section 10.3) has not been run yet, or it ran before a re-mirror or a later Part B run. Run it again.
- **`COMMIT FAILED` from `fix_links.py`:** the token's user may not push to the project's default branch. Allow push temporarily, re-run with `--apply`, then restore the protection.
- **`ABORT: ... not set` from the link-fix scripts:** export `GITLAB_NEW_URL`, `GITLAB_NEW_TOKEN` and `LEGACY_BASES` for the Python script; pass `LEGACY_BASES` and `NEW_BASE` inline to `sudo` for the Ruby one.
- **A migrated wiki shows the empty page on the new instance:** the push failed, or the pages went to a branch that is not the wiki's default branch. Run `python3 migrate_wikis.py --verify`; if the count differs, re-run `--apply --force` for that project and read the message it prints.
- **`clone from legacy failed` in `migrate_wikis.py`:** the legacy token cannot read that project's wiki, or `GITLAB_LEGACY_URL` is wrong. Test the token as in Step 5 and check the URL.
- **`project does not exist on new` in `migrate_wikis.py`:** Part A has not created that project yet. Run Part A first.

## 14. Final checklist

Tick each item before declaring the migration done.

- [ ] Pre-flight checks done (Step 4), and the registry question is answered.
- [ ] Both tokens created and tested with `curl` (Step 5).
- [ ] Package copied to the new server without the demo folder (section 6).
- [ ] Dependencies installed; `config.yaml` filled in with correct URLs.
- [ ] Part A dry run reviewed; `dry_run` set to false; Part A real run finished with `Done.`
- [ ] Part B copied to `/tmp`; dry run reviewed; no stale state file; real run finished once with `Done.` and no `marked done but not found` errors.
- [ ] `validate_migration.py` passes (or each mismatch is explained).
- [ ] Manual spot checks done (section 10.1).
- [ ] Webhook secrets re-entered; users told to reset passwords and re-enable 2FA.
- [ ] Legacy kept intact until the team signs off, and a final sync done if legacy was still in use.
- [ ] Wikis copied (section 10.4) and `migrate_wikis.py --verify` shows every project matching.
- [ ] Link fix (section 10.3) dry run reviewed, then applied as the last step; a README link on the new instance stays on the new server.
- [ ] Migration tokens revoked.
- [ ] The extra steps (forks, labels, milestones, SSH keys, snippets) ran, and the validator's extras section passes.

## 15. Command cheat sheet

All on the new server, from `~/real_migration`, with tokens exported.

```bash
# Part A
python3 migrate_gitlab.py --dry-run
sed -i 's/dry_run: true/dry_run: false/' config.yaml
python3 migrate_gitlab.py

# Part B
cp migrate_metadata.rb config.yaml /tmp/ && chmod 644 /tmp/migrate_metadata.rb /tmp/config.yaml
sudo GITLAB_LEGACY_URL="$GITLAB_LEGACY_URL" GITLAB_LEGACY_TOKEN="$GITLAB_LEGACY_TOKEN" DRY_RUN=true gitlab-rails runner /tmp/migrate_metadata.rb
sudo GITLAB_LEGACY_URL="$GITLAB_LEGACY_URL" GITLAB_LEGACY_TOKEN="$GITLAB_LEGACY_TOKEN" gitlab-rails runner /tmp/migrate_metadata.rb 2>&1 | tee /tmp/meta_real.log

# Validate
python3 validate_migration.py --config config.yaml
```

Wikis (section 10.4), with the four `GITLAB_LEGACY_*` and `GITLAB_NEW_*` variables exported:

```bash
python3 migrate_wikis.py                 # dry run
python3 migrate_wikis.py --apply
python3 migrate_wikis.py --verify
python3 fix_links.py --wikis-only --apply   # then fix links inside wiki pages
```

Link fix, as the very last step (section 10.3):

```bash
export GITLAB_NEW_URL=http://192.16.0.65
export LEGACY_BASES="http://180.210.129.233:8090,http://180.210.129.233"
python3 fix_links.py                 # dry run
python3 fix_links.py --apply

cp fix_links_metadata.rb /tmp/ && chmod 644 /tmp/fix_links_metadata.rb
sudo LEGACY_BASES="$LEGACY_BASES" NEW_BASE="$GITLAB_NEW_URL" gitlab-rails runner /tmp/fix_links_metadata.rb              # dry run
sudo LEGACY_BASES="$LEGACY_BASES" NEW_BASE="$GITLAB_NEW_URL" APPLY=1 gitlab-rails runner /tmp/fix_links_metadata.rb      # real run
```
