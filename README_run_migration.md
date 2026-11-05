# run_migration.sh: quick guide

One script that runs the whole GitLab migration (legacy 12.1.6 to new GitLab) in the background and saves every command and its output to one `.md` file.

## Before you start

- Run it on the **new GitLab server** (Part B needs `gitlab-rails`).
- Put `run_migration.sh` in the same folder as `config.yaml` and the migration scripts:
  `migrate_gitlab.py`, `migrate_metadata.rb`, `validate_migration.py`, `migrate_wikis.py`, `fix_links.py`, `fix_links_metadata.rb`, `block.py`.
- Needed on the server: `python3` (with `requests` and `pyyaml`), `git`, `git-lfs`, `curl`, `sudo`.
- Have ready: the legacy URL and admin token, the new URL and admin token, and your sudo password.

```bash
chmod +x run_migration.sh
```

## Commands

| Command | What it does |
|---|---|
| `./run_migration.sh start --dry` | Rehearsal. Dry-runs every step and changes nothing. Do this first. |
| `./run_migration.sh start` | Real migration. |
| `./run_migration.sh status` | Shows the current step, whether it is still running, and the last log lines. |
| `./run_migration.sh tail` | Live view of the log. Ctrl+C only stops watching; the migration keeps going. |
| `./run_migration.sh stop` | Stops the run (asks you to type STOP). |
| `./run_migration.sh start --from <step>` | Continue from a step after a failure. |
| `./run_migration.sh start --resume` | Run all steps again, allowing old state files. |

## What `start` does

1. Asks for the legacy URL and token, the new URL and token, and the sudo password. Tokens and the password are hidden as you type.
2. Checks both tokens (they must be admin tokens) and the sudo password.
3. Shows the two hosts and asks you to type `YES`.
4. Detaches into the background. **You can close the terminal after this.** A server reboot would stop it.

## Steps, in order

| Step | What runs |
|---|---|
| `preflight` | Checks tools, sudo, tokens, disk space and leftover state files |
| `part_a` | `migrate_gitlab.py`: users, groups, projects, repos, labels, etc. |
| `part_b` | `migrate_metadata.rb` via `gitlab-rails`: issues, MRs, comments |
| `validate` | `validate_migration.py`: compares legacy and new |
| `wikis` | `migrate_wikis.py --apply` |
| `wikis_verify` | `migrate_wikis.py --verify` |
| `links_repo` | `fix_links.py --wikis --apply`: legacy links in repo files and wikis |
| `links_meta` | `fix_links_metadata.rb`: legacy links in issues, MRs, comments |
| `block` | `block.py`: blocks the users that are blocked on legacy (last) |
| `final_check` | Final API-only validation (informational) |

## Where everything is saved

Each run gets its own folder, and `migration_runs/latest` always points to the newest one:

```
migration_runs/<timestamp>/
  migration_run.md       <- THE LOG: every command + all output + summary table
  migration_run.raw.log  <- same output as plain text
  steps/<step>.log       <- one log per step
  status                 <- current state (also shown by "status")
  pid                    <- process id of the background run
```

To read the log:

```bash
./run_migration.sh tail                      # live
less migration_runs/latest/migration_run.md  # whole file
```

The old scripts also keep writing their own files in the script folder: `migration.log`, `migration_state.json`, `wiki_migration_state.json`. Part B's state is `/tmp/metadata_migration_state.json`.

What the log contains: for every step, the exact command, everything it printed (as it happens), the exit code and duration. A summary table is at the end. Real usernames, group names, project paths and URLs appear in it. **Tokens and the sudo password are masked as `***`.** The log files are readable only by your user.

## If something fails

- The run stops at the first failed step. The summary at the bottom of `migration_run.md` names it.
- Read that step's output, fix the cause, then: `./run_migration.sh start --from <step>`.
- Never start Part B twice at the same time. The script refuses a second run while one is active.
- `[ERROR]` lines in Part A are counted in the summary but do not stop the run (some are harmless, like `409 already a member`). `validate` is the check that decides if the migration is good.

## Good to know

- A real run refuses to start if an old `migration_state.json`, `wiki_migration_state.json` or `/tmp/metadata_migration_state.json` exists. Delete them for a clean run, or use `--resume`.
- Your `config.yaml` is never edited. The script makes a temporary copy with the URLs filled in and deletes it at the end.
- Secrets are never put on a command line (they do not show in `ps`). They are held in a `600` file in memory (`/dev/shm`) that is deleted when the run ends.
- Part B can take a long time and looks frozen for the first 1 to 3 minutes while Rails starts. That is normal.
