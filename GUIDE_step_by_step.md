# GitLab migration: step-by-step guide (with `run_migration.sh`)

From preparation to the final checks. Follow it in order.

## Part 1: Before the run (preflight)

### 1. Get these from your team lead

You do not need to know them in advance. Just have them at hand when you run `start`.

- Legacy GitLab URL (with port, e.g. `http://host:8090`)
- New GitLab URL
- Legacy **admin** token and new **admin** token. Both need the `api` scope. A non-admin token is rejected by the script.
- SSH login to the **new** GitLab server and its sudo password

### 2. Check the servers (on the new server)

Network to legacy (expect `200` or `302`):

```bash
curl -sI <LEGACY_URL>/users/sign_in | head -1
```

Free disk space. The mirror step clones about 4 large repos at the same time in `/tmp`, and the new instance will grow by about 70 GB:

```bash
df -h /tmp /var/opt/gitlab
```

Container registry on legacy (ask the team lead or check on the legacy host). If it is active, registry images are **not** migrated by these scripts:

```bash
docker exec gitlab-legacy grep -n "registry_external_url" /etc/gitlab/gitlab.rb
```

A line starting with `#` means no registry.

### 3. Copy the package to the new server

On your PC. Do **not** include the `_demo_only_DO_NOT_COPY` folder:

```bash
zip -r Real_Migration.zip "Real Migration" -x "*/_demo_only_DO_NOT_COPY/*"
scp -P <SSH_PORT> Real_Migration.zip <user>@<NEW_SERVER>:~/
```

On the new server:

```bash
ssh -p <SSH_PORT> <user>@<NEW_SERVER>
unzip -o Real_Migration.zip
mv "Real Migration" real_migration
cd real_migration
```

Put `run_migration.sh` and this guide in this same folder, then:

```bash
chmod +x run_migration.sh
ls
```

You should see `config.yaml`, `migrate_gitlab.py`, `migrate_metadata.rb`, `validate_migration.py`, `migrate_wikis.py`, `fix_links.py`, `fix_links_metadata.rb`, `block.py` and `run_migration.sh`.

### 4. Install what is needed

```bash
sudo apt update
sudo apt install -y python3-pip git git-lfs jq curl unzip
git lfs install
pip install requests pyyaml --break-system-packages
```

### 5. Edit `config.yaml`

```bash
nano config.yaml
```

You do **not** need to set the URLs, tokens or `dry_run`. The script fills those in itself and never edits your file. Only check:

- `project_allowlist`: empty `[]` means migrate everything. List full project paths to migrate only some.
- `user_skiplist`: users that must not be migrated (e.g. `ghost`, `support-bot`).
- The temporary password for migrated users (`temp_password`). Users must change it at first login.

### 6. Plan the timing

If people keep pushing to legacy during the migration, anything pushed after the repo copy needs a sync pass afterwards (see Part 5). Ask the team lead for a quiet window.

## Part 2: Rehearsal (do this first)

```bash
./run_migration.sh start --dry
```

1. Type the legacy URL, legacy token, new URL, new token, and your sudo password when asked. Tokens and password are hidden as you type.
2. It checks both tokens and the sudo password, shows the two hosts, and asks you to type `YES`.
3. After `YES` it runs in the background. You can close the terminal.

Watch it:

```bash
./run_migration.sh status
./run_migration.sh tail        # Ctrl+C stops watching only
```

When `status` says `DONE`, read the log (`migration_runs/latest/migration_run.md`). A dry run changes nothing, so `validate` and the final check are skipped. If a step failed, fix it and run `start --dry` again.

## Part 3: The real run

If the dry run state files were left behind, remove them first (a real run refuses to start with old state):

```bash
ls migration_state.json wiki_migration_state.json /tmp/metadata_migration_state.json 2>/dev/null
```

Then:

```bash
./run_migration.sh start
```

Answer the same prompts and type `YES`. Then close the terminal if you like. It keeps running. To check in later, SSH back in and run:

```bash
cd ~/real_migration
./run_migration.sh status
```

What happens, in order:

| # | Step | What it does |
|---|---|---|
| 1 | `preflight` | Checks tools, sudo, tokens, disk, old state files |
| 2 | `part_a` | Users, groups, projects, repos (with LFS), forks, labels, milestones, webhooks, protected branches, SSH keys, snippets |
| 3 | `part_b` | Issues, merge requests, comments (the first 1 to 3 minutes look frozen while Rails starts; that is normal) |
| 4 | `validate` | Compares legacy and new. **If this fails, the run stops.** |
| 5 | `wikis` | Copies the wikis |
| 6 | `wikis_verify` | Compares wiki page counts |
| 7 | `links_repo` | Fixes legacy links in repo files and wikis |
| 8 | `links_meta` | Fixes legacy links in issues, MRs, comments |
| 9 | `block` | Blocks the users who are blocked on legacy |
| 10 | `final_check` | Final API checks (informational) |

With about 70 GB of data, expect Part A to take the longest.

## Part 4: When it finishes

1. `./run_migration.sh status` should say `DONE`.
2. Open `migration_runs/latest/migration_run.md` and scroll to **Summary**. Every step should say OK. `[ERROR] lines` are listed for review; some are harmless (for example `409 already a member`).
3. Quick look in the new GitLab UI:
   - Admin Area, Users: the right users, and the Blocked tab matches legacy
   - A few issues and merge requests: author, date and comments are right
   - A few projects: commit history and branches are right
   - An LFS project: download one LFS file
   - A README: its links stay on the new server
4. Manual follow-up (these are not migrated):
   - Re-enter webhook secret tokens
   - Users must log in with the temporary password, set a new one, re-enable 2FA, and re-add any SSH key the log reported as failed
   - Issue and MR attachments, deploy keys, protected tags, and CI runners are not migrated
5. Clean up:
   - Revoke both migration tokens
   - The log has real usernames, group and project paths and URLs. Keep it somewhere safe, or delete `migration_runs/` when done
   - Remove `/tmp/gitlab-migration-mirror` if it is still there

## Part 5: If something goes wrong

The run stops at the first failed step. The **Summary** at the bottom of the log names it.

1. Read that step's output in the log (or `migration_runs/latest/steps/<step>.log`).
2. Fix the cause.
3. Continue from that step:

```bash
./run_migration.sh start --from <step>
```

| Problem | What to do |
|---|---|
| `start` says a token or URL failed | Check the URL, port and `http` vs `https`, and that the token is an admin token with `api` scope. Nothing was changed. |
| `start` says sudo password rejected | Re-run and retype it. |
| Preflight says old state file exists | Delete it for a clean run, or use `start --resume`. |
| `part_a` shows many `[ERROR]` lines | Not always a problem. Check the `validate` result. |
| `validate` fails (e.g. `legacy=12 new=11`) | Read which check failed. Re-run with `start --from part_a` (safe to repeat), then it validates again. |
| `part_b` has no final `Done.` | It crashed. Read the output, then `start --from part_b`. Never run two copies at once. |
| Wiki step fails | Fix the project it names, then `start --from wikis`. |
| You need to stop it | `./run_migration.sh stop` (type `STOP`). Part B can be continued later with `start --from part_b`. |

Legacy was still used during the migration? New pushes, issues and users need a sync pass. Run `python3 sync_delta.py --config config.yaml`, then `start --from part_a`, and finish with `start --from links_repo` so the link fix runs again at the end. (Set the tokens first: `read -rs GITLAB_LEGACY_TOKEN; export GITLAB_LEGACY_TOKEN`, and the same for `GITLAB_NEW_TOKEN`.)
