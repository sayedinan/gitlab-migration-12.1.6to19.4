#!/usr/bin/env bash
# =============================================================================
# run_migration.sh - run the whole GitLab migration with ONE command, in the
# background, with every command and its output saved to one .md file.
#
# Put this file next to config.yaml and the migration scripts, on the NEW
# GitLab server (Part B needs gitlab-rails), then:
#
#   chmod +x run_migration.sh
#   ./run_migration.sh start            # real run (asks for URLs, tokens, sudo password)
#   ./run_migration.sh start --dry      # rehearsal: dry-run every step, changes nothing
#   ./run_migration.sh status           # where is it now? still running?
#   ./run_migration.sh tail             # watch the log live (Ctrl+C only stops watching)
#   ./run_migration.sh stop             # stop the run (asks to confirm)
#
#   start --from <step>   continue from a step after a failure (steps below)
#   start --resume        run all steps again, allowing existing state files
#
# Steps, in order:
#   preflight part_a part_b validate wikis wikis_verify links_repo links_meta
#   block final_check
#
# Output of every run goes to:  migration_runs/<timestamp>/migration_run.md
# (+ migration_run.raw.log and one log per step in steps/).
# Tokens and the sudo password are masked in the log. URLs are NOT masked.
# =============================================================================
set -uo pipefail
umask 077

SELF="$(readlink -f "${BASH_SOURCE[0]}")"
HERE="$(dirname "$SELF")"
RUNS="$HERE/migration_runs"
RAILS_CFG="/tmp/migration_rails_config.yaml"
ALL_STEPS=(preflight part_a part_b validate wikis wikis_verify links_repo links_meta block final_check)
REQUIRED_FILES=(config.yaml migrate_gitlab.py migrate_metadata.rb validate_migration.py
                migrate_wikis.py fix_links.py fix_links_metadata.rb block.py)

die() { echo "ERROR: $*" >&2; exit 1; }
now() { date '+%Y-%m-%d %H:%M:%S'; }

# --------------------------------------------------------------------------
# helpers shared by start (foreground) and _run (background)
# --------------------------------------------------------------------------
sudo_pw() { printf '%s\n' "$SUDO_PW" | sudo -S -p '' "$@"; }

# api_check <name> <base-url> <token> : verifies the token is a working admin token
api_check() {
  local name="$1" url="$2" tok="$3" body code who ver
  body="$(mktemp)"
  code="$(printf 'header = "PRIVATE-TOKEN: %s"\n' "$tok" \
          | curl -sS -m 25 -K - -o "$body" -w '%{http_code}' "$url/api/v4/user" 2>&1)" || {
    echo "  [FAIL] $name: cannot reach $url -> $code"; rm -f "$body"; return 1; }
  if [ "$code" != "200" ]; then
    echo "  [FAIL] $name: $url/api/v4/user answered HTTP $code (wrong URL, port or token?)"
    rm -f "$body"; return 1
  fi
  who="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("username","?"), "admin" if d.get("is_admin") else "NOT-ADMIN")' "$body" 2>/dev/null)"
  printf 'header = "PRIVATE-TOKEN: %s"\n' "$tok" \
    | curl -sS -m 25 -K - -o "$body" "$url/api/v4/version" 2>/dev/null
  ver="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("version","?"))' "$body" 2>/dev/null)"
  rm -f "$body"
  case "$who" in
    *NOT-ADMIN*) echo "  [FAIL] $name: token works but is NOT an admin token ($who)"; return 1 ;;
    *)           echo "  [ OK ] $name: $url  version=$ver  token user=$who" ;;
  esac
}

# --------------------------------------------------------------------------
# start (foreground part): ask, check, hand over to the background runner
# --------------------------------------------------------------------------
cmd_start() {
  local DRY=0 RESUME=0 FROM=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --dry)    DRY=1 ;;
      --resume) RESUME=1 ;;
      --from)   shift; FROM="${1:-}"; RESUME=1
                [[ " ${ALL_STEPS[*]} " == *" $FROM "* ]] || die "--from needs one of: ${ALL_STEPS[*]}" ;;
      *) die "unknown option: $1" ;;
    esac; shift
  done

  [ -t 0 ] || die "start must be run from an interactive terminal (it asks for secrets)."
  for t in python3 curl flock setsid sudo git mktemp; do
    command -v "$t" >/dev/null 2>&1 || die "missing tool: $t"
  done
  for f in "${REQUIRED_FILES[@]}"; do [ -f "$HERE/$f" ] || die "missing file next to this script: $f"; done

  mkdir -p "$RUNS"
  if [ -f "$RUNS/latest/pid" ] && kill -0 "$(cat "$RUNS/latest/pid")" 2>/dev/null; then
    die "a migration run is already in progress (pid $(cat "$RUNS/latest/pid")). Use: $0 status"
  fi

  echo "GitLab migration runner. Nothing typed here is shown again or saved in plain text."
  echo
  local L_URL L_TOK N_URL N_TOK SUDO_PW
  read -r  -p "Legacy GitLab URL (e.g. http://host:8090): " L_URL
  read -r -s -p "Legacy GitLab admin token (hidden): "      L_TOK; echo
  read -r  -p "New GitLab URL (e.g. http://host): "          N_URL
  read -r -s -p "New GitLab admin token (hidden): "          N_TOK; echo
  read -r -s -p "sudo password for $(id -un) (hidden): "     SUDO_PW; echo
  L_URL="${L_URL%/}"; N_URL="${N_URL%/}"
  [[ "$L_URL" =~ ^https?:// ]] || die "legacy URL must start with http:// or https://"
  [[ "$N_URL" =~ ^https?:// ]] || die "new URL must start with http:// or https://"
  [ -n "$L_TOK" ] && [ -n "$N_TOK" ] && [ -n "$SUDO_PW" ] || die "token / password cannot be empty"

  echo; echo "Checking before anything starts..."
  local bad=0
  api_check legacy "$L_URL" "$L_TOK" || bad=1
  api_check new    "$N_URL" "$N_TOK" || bad=1
  if printf '%s\n' "$SUDO_PW" | sudo -S -k -p '' true 2>/dev/null; then
    echo "  [ OK ] sudo password accepted"
  else
    echo "  [FAIL] sudo password rejected"; bad=1
  fi
  [ "$bad" = 0 ] || die "fix the above and run start again. Nothing was changed."

  local mode="REAL RUN (changes the new GitLab)"; [ "$DRY" = 1 ] && mode="DRY RUN (changes nothing)"
  echo
  echo "  Mode   : $mode"
  echo "  Legacy : $L_URL"
  echo "  New    : $N_URL"
  [ -n "$FROM" ] && echo "  From   : step '$FROM'"
  echo
  local ans; read -r -p "Type YES to start in the background: " ans
  [ "$ans" = "YES" ] || die "cancelled."

  local RUNDIR; RUNDIR="$RUNS/$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$RUNDIR/steps"; chmod 700 "$RUNDIR"
  ln -sfn "$RUNDIR" "$RUNS/latest"

  local SHM=/dev/shm; { [ -d "$SHM" ] && [ -w "$SHM" ]; } || SHM="$RUNDIR"
  local SECF; SECF="$(mktemp -p "$SHM" migration_secrets.XXXXXX)"
  {
    printf 'L_URL=%q\n' "$L_URL";   printf 'L_TOK=%q\n' "$L_TOK"
    printf 'N_URL=%q\n' "$N_URL";   printf 'N_TOK=%q\n' "$N_TOK"
    printf 'SUDO_PW=%q\n' "$SUDO_PW"
    printf 'DRY=%q\n' "$DRY";       printf 'RESUME=%q\n' "$RESUME"; printf 'FROM=%q\n' "$FROM"
    printf 'SHM=%q\n' "$SHM"
  } > "$SECF"
  chmod 600 "$SECF"
  unset L_TOK N_TOK SUDO_PW

  setsid -f nohup "$SELF" _run "$SECF" "$RUNDIR" >"$RUNDIR/launcher.out" 2>&1 </dev/null
  local i; for i in 1 2 3 4 5 6 7 8 9 10; do [ -s "$RUNDIR/pid" ] && break; sleep 0.5; done
  if [ -s "$RUNDIR/pid" ] && kill -0 "$(cat "$RUNDIR/pid")" 2>/dev/null; then
    echo
    echo "Started in the background (pid $(cat "$RUNDIR/pid")). You can close this terminal."
    echo "  Log file : $RUNDIR/migration_run.md"
    echo "  Progress : $0 status"
    echo "  Live log : $0 tail"
  else
    echo "The background process did not start. See $RUNDIR/launcher.out" >&2
    cat "$RUNDIR/launcher.out" >&2 2>/dev/null; rm -f "$SECF"; exit 1
  fi
}

# --------------------------------------------------------------------------
# status / tail / stop
# --------------------------------------------------------------------------
latest_dir() { [ -e "$RUNS/latest" ] || die "no run found yet."; readlink -f "$RUNS/latest"; }

cmd_status() {
  local d pid state=""; d="$(latest_dir)"
  [ -f "$d/status" ] && state="$(cat "$d/status")"
  pid="$(cat "$d/pid" 2>/dev/null || true)"
  echo "Run     : $d"
  echo "Status  : ${state:-unknown}"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then echo "Process : running (pid $pid)"
  else echo "Process : not running"; fi
  echo "Log     : $d/migration_run.md"
  echo; echo "--- last lines of the log ---"
  tail -n 15 "$d/migration_run.md" 2>/dev/null
}

cmd_tail() {
  local d; d="$(latest_dir)"
  echo "Watching $d/migration_run.md  (Ctrl+C stops watching only; the migration keeps running)"
  tail -n 40 -f "$d/migration_run.md"
}

cmd_stop() {
  local d pid ans; d="$(latest_dir)"; pid="$(cat "$d/pid" 2>/dev/null || true)"
  { [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; } || { echo "Nothing is running."; return 0; }
  echo "This stops the migration immediately. If it is inside Part B, re-run later with:"
  echo "  $0 start --from part_b      (Part B is resumable; never start two copies)"
  read -r -p "Type STOP to confirm: " ans; [ "$ans" = "STOP" ] || { echo "cancelled."; return 0; }
  kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null
  echo "Asking sudo to stop any gitlab-rails runner left over (may ask for your password)..."
  sudo pkill -f '[g]itlab-rails runner /tmp/(migrate_metadata|fix_links_metadata)\.rb' 2>/dev/null || true
  echo "STOPPED by user at $(now)" > "$d/status"
  echo "Stopped."
}

# --------------------------------------------------------------------------
# background runner
# --------------------------------------------------------------------------
read -r -d '' MASK_PY <<'PY'
import os, sys, urllib.parse
vals = []
for k in ("MASK_1", "MASK_2", "MASK_3"):
    v = os.environ.get(k, "")
    if len(v) >= 4:
        vals += [v, urllib.parse.quote(v, safe="")]
vals = sorted(set(vals), key=len, reverse=True)
sys.stdin.reconfigure(errors="replace"); sys.stdout.reconfigure(errors="replace")
while True:
    line = sys.stdin.readline()
    if not line:
        break
    for v in vals:
        line = line.replace(v, "***")
    if not line.endswith("\n"):
        line += "\n"
    sys.stdout.write(line); sys.stdout.flush()
PY

mask() { MASK_1="$L_TOK" MASK_2="$N_TOK" MASK_3="$SUDO_PW" python3 -u -c "$MASK_PY"; }
in_here() { ( cd "$HERE" && "$@" ); }
set_status() { printf '%s | step: %s | since %s\n' "$1" "$2" "$(now)" > "$RUNDIR/status"; }

ROWS=(); ERR_SUMMARY=(); FAILED_AT=""; WARNED=0

record_row() { ROWS+=("| $1 | $2 | $3 | $4 | $5 |"); }
record_skip() {
  record_row "$1" "skipped" "-" "-" "-"
  printf '\n---\n\n## %s\n\n_Skipped: %s_\n' "$1" "$2" >> "$MD"
}

# run_step <id> <fatal 1|0> <title> <display-command> -- <command...>
# optional: POST_CHECK=<function> (gets the step log path; non-zero = failed)
run_step() {
  local id="$1" fatal="$2" title="$3" display="$4"; shift 5
  local steplog="$RUNDIR/steps/$id.log" t0=$SECONDS rc errs res
  : > "$steplog"
  set_status RUNNING "$id"
  {
    printf '\n---\n\n## %s\n\n- Started: %s\n\n**Command:**\n\n```bash\n%s\n```\n\n**Output:**\n\n```text\n' \
      "$title" "$(now)" "$display"
  } >> "$MD"
  "$@" 2>&1 | mask | tee -a "$RAW" "$steplog" >> "$MD"
  rc=${PIPESTATUS[0]}
  if [ "$rc" -eq 0 ] && [ -n "${POST_CHECK:-}" ]; then
    "$POST_CHECK" "$steplog" || rc=98
  fi
  errs="$(grep -c '\[ERROR\]' "$steplog" 2>/dev/null)"; errs="${errs:-0}"
  if [ "$rc" -eq 0 ]; then res="OK"; elif [ "$fatal" = 1 ]; then res="FAILED"; else res="WARNING"; WARNED=1; fi
  [ "$rc" -eq 0 ] && [ "$errs" -gt 0 ] && res="OK (see error lines)"
  printf '```\n\n- Finished: %s - exit code **%s**, %ss, %s line(s) containing [ERROR]\n' \
    "$(now)" "$rc" "$((SECONDS - t0))" "$errs" >> "$MD"
  [ "$errs" -gt 0 ] && ERR_SUMMARY+=("$id: $errs [ERROR] line(s) - review $steplog")
  record_row "$id" "$res" "$rc" "$((SECONDS - t0))s" "$errs"
  [ "$rc" -eq 0 ] || [ "$fatal" = 0 ]
}

# rails_run <ruby-file-in-/tmp> [VAR=value ...]
# Secrets go through a 600 file in memory (/dev/shm), never on a command line.
rails_run() {
  local rb="$1"; shift
  local envf rc kv; envf="$(mktemp -p "$SHM" rails_env.XXXXXX)"
  {
    for kv in "$@"; do printf 'export %s=%q\n' "${kv%%=*}" "${kv#*=}"; done
    printf 'exec gitlab-rails runner %q\n' "$rb"
  } > "$envf"
  sudo_pw bash "$envf"; rc=$?
  rm -f "$envf"; return $rc
}

make_config() {
  python3 - "$HERE/config.yaml" "$CFG" "$RAILS_CFG" "$DRY" <<'PY'
import os, sys, yaml
src, dst, rails_dst, dry = sys.argv[1:5]
cfg = yaml.safe_load(open(src)) or {}
cfg.setdefault("legacy", {})["url"] = os.environ["GITLAB_LEGACY_URL"]
cfg.setdefault("new", {})["url"] = os.environ["GITLAB_NEW_URL"]
cfg["legacy"]["token"] = "from-environment"
cfg["new"]["token"] = "from-environment"
cfg.setdefault("options", {})["dry_run"] = (dry == "1")
yaml.safe_dump(cfg, open(dst, "w"), sort_keys=False)
yaml.safe_dump({"project_allowlist": cfg.get("project_allowlist") or []}, open(rails_dst, "w"))
print("project_allowlist:", cfg.get("project_allowlist") or "[] (all projects)")
print("options.dry_run  :", cfg["options"]["dry_run"])
PY
  chmod 600 "$CFG"; chmod 644 "$RAILS_CFG"
}

preflight_checks() {
  local bad=0 t
  echo "Host: $(hostname)   User: $(id -un)   Time: $(now)"
  echo "Mode: $([ "$DRY" = 1 ] && echo DRY RUN || echo REAL RUN)   Resume: $RESUME   From: ${FROM:-start}"
  echo "Legacy: $L_URL"; echo "New   : $N_URL"; echo
  echo "Tools:"
  for t in python3 git git-lfs curl jq; do
    if command -v "$t" >/dev/null 2>&1; then echo "  [ OK ] $t  $("$t" --version 2>&1 | head -1)"
    else [ "$t" = jq ] && echo "  [skip] jq not installed (optional)" || { echo "  [FAIL] $t missing"; bad=1; }; fi
  done
  python3 -c 'import requests, yaml' 2>/dev/null && echo "  [ OK ] python modules requests, yaml" \
    || { echo "  [FAIL] python modules requests/yaml missing (pip install requests pyyaml --break-system-packages)"; bad=1; }
  echo; echo "Sudo / Rails:"
  sudo_pw true 2>/dev/null && echo "  [ OK ] sudo works" || { echo "  [FAIL] sudo failed"; bad=1; }
  sudo_pw bash -c 'command -v gitlab-rails' 2>/dev/null | sed 's/^/  [ OK ] gitlab-rails at /' | grep . \
    || { echo "  [FAIL] gitlab-rails not found - run this on the NEW GitLab server"; bad=1; }
  echo; echo "API access:"
  api_check legacy "$L_URL" "$L_TOK" || bad=1
  api_check new    "$N_URL" "$N_TOK" || bad=1
  echo; echo "Disk:"; df -h "$HERE" /tmp 2>/dev/null | sed 's/^/  /'
  echo; echo "Old state files:"
  if [ "$RESUME" = 1 ]; then
    echo "  (resume mode: existing state files are allowed)"
  else
    for t in "$HERE/migration_state.json" "$HERE/wiki_migration_state.json" /tmp/metadata_migration_state.json; do
      if [ -e "$t" ]; then echo "  [FAIL] $t exists from an earlier run. Remove it for a clean run, or use: start --resume"; bad=1
      else echo "  [ OK ] no $t"; fi
    done
  fi
  return $bad
}

part_b_done() { grep -q 'Done\.' "$1" || { echo "[wrapper] Part B output has no final 'Done.' line - treating as failed"; return 1; }; }

step_preflight() {
  run_step preflight 1 "Step 0 - Preflight checks" "(tools, sudo, tokens, disk, old state files)" -- preflight_checks
}
step_part_a() {
  local flag=""; [ "$DRY" = 1 ] && flag=" --dry-run"
  run_step part_a 1 "Step 1 - Part A: users, groups, projects, repos, labels, ..." \
    "cd $HERE && python3 migrate_gitlab.py --config <generated config>$flag" -- \
    in_here python3 migrate_gitlab.py --config "$CFG" ${flag:+--dry-run}
}
step_part_b() {
  install -m 644 "$HERE/migrate_metadata.rb" /tmp/migrate_metadata.rb
  local extra=(); [ "$DRY" = 1 ] && extra=(DRY_RUN=true)
  POST_CHECK=part_b_done run_step part_b 1 "Step 2 - Part B: issues, merge requests, comments (gitlab-rails)" \
    "sudo GITLAB_LEGACY_URL=$L_URL GITLAB_LEGACY_TOKEN=*** CONFIG_YAML_PATH=$RAILS_CFG ${extra[*]:-} gitlab-rails runner /tmp/migrate_metadata.rb" -- \
    rails_run /tmp/migrate_metadata.rb "GITLAB_LEGACY_URL=$L_URL" "GITLAB_LEGACY_TOKEN=$L_TOK" \
      "CONFIG_YAML_PATH=$RAILS_CFG" ${extra[@]+"${extra[@]}"}
}
step_validate() {
  [ "$DRY" = 1 ] && { record_skip validate "dry run - nothing was migrated to validate"; return 0; }
  run_step validate 1 "Step 3 - Validate (legacy vs new)" \
    "cd $HERE && python3 validate_migration.py --config <generated config>" -- \
    in_here python3 validate_migration.py --config "$CFG"
}
step_wikis() {
  local flag="--apply"; [ "$DRY" = 1 ] && flag=""
  run_step wikis 1 "Step 4 - Migrate wikis" \
    "cd $HERE && python3 migrate_wikis.py --config <generated config> $flag" -- \
    in_here python3 migrate_wikis.py --config "$CFG" ${flag:+--apply}
}
step_wikis_verify() {
  [ "$DRY" = 1 ] && { record_skip wikis_verify "dry run"; return 0; }
  run_step wikis_verify 1 "Step 5 - Verify wiki page counts" \
    "cd $HERE && python3 migrate_wikis.py --config <generated config> --verify" -- \
    in_here python3 migrate_wikis.py --config "$CFG" --verify
}
step_links_repo() {
  local flag="--apply"; [ "$DRY" = 1 ] && flag=""
  run_step links_repo 1 "Step 6 - Fix legacy links in repo files and wikis" \
    "cd $HERE && python3 fix_links.py --wikis $flag" -- \
    in_here python3 fix_links.py --wikis ${flag:+--apply}
}
step_links_meta() {
  install -m 644 "$HERE/fix_links_metadata.rb" /tmp/fix_links_metadata.rb
  local extra=(); [ "$DRY" = 1 ] || extra=(APPLY=1)
  run_step links_meta 1 "Step 7 - Fix legacy links in issues, MRs, comments, milestones (gitlab-rails)" \
    "sudo LEGACY_BASES=$LEGACY_BASES NEW_BASE=$N_URL ${extra[*]:-} gitlab-rails runner /tmp/fix_links_metadata.rb" -- \
    rails_run /tmp/fix_links_metadata.rb "LEGACY_BASES=$LEGACY_BASES" "NEW_BASE=$N_URL" ${extra[@]+"${extra[@]}"}
}
step_block() {
  local flag=""; [ "$DRY" = 1 ] && flag="--dry-run"
  run_step block 1 "Step 8 - Block users that are blocked on legacy (LAST data step)" \
    "cd $HERE && python3 block.py --config <generated config> $flag" -- \
    in_here python3 block.py --config "$CFG" ${flag:+--dry-run}
}
step_final_check() {
  [ "$DRY" = 1 ] && { record_skip final_check "dry run"; return 0; }
  run_step final_check 0 "Step 9 - Final check (API checks only; the link fix added 1 commit per project)" \
    "cd $HERE && python3 validate_migration.py --config <generated config> --skip-commit-check" -- \
    in_here python3 validate_migration.py --config "$CFG" --skip-commit-check
}

cleanup() {
  rm -f "${SECF:-}" "${CFG:-}" "$RAILS_CFG" /tmp/migrate_metadata.rb /tmp/fix_links_metadata.rb 2>/dev/null
  [ -n "${SHM:-}" ] && rm -f "$SHM"/rails_env.* 2>/dev/null
  unset SUDO_PW L_TOK N_TOK
}
on_signal() { set_status "STOPPED (signal)" "${CURRENT:-?}"; printf '\n---\n\n**Run stopped by signal at %s**\n' "$(now)" >> "$MD"; exit 143; }

cmd__run() {
  SECF="$1"; RUNDIR="$2"
  # shellcheck disable=SC1090
  source "$SECF"; rm -f "$SECF"
  trap '' HUP
  trap on_signal TERM INT
  trap cleanup EXIT
  echo $$ > "$RUNDIR/pid"

  exec 9>"$RUNS/.lock"
  flock -n 9 || { echo "another run holds the lock" > "$RUNDIR/launcher.out"; exit 3; }

  MD="$RUNDIR/migration_run.md"; RAW="$RUNDIR/migration_run.raw.log"; CFG="$RUNDIR/config.run.yaml"
  : > "$MD"; : > "$RAW"; chmod 600 "$MD" "$RAW"
  export PYTHONUNBUFFERED=1 GIT_TERMINAL_PROMPT=0
  export GITLAB_LEGACY_URL="$L_URL" GITLAB_LEGACY_TOKEN="$L_TOK" GITLAB_NEW_URL="$N_URL" GITLAB_NEW_TOKEN="$N_TOK"
  LEGACY_BASES="$(python3 -c 'import sys; from urllib.parse import urlparse; u=sys.argv[1]; p=urlparse(u); b=[u]; h=f"{p.scheme}://{p.hostname}"; b+= [h] if h!=u else []; print(",".join(b))' "$L_URL")"
  export LEGACY_BASES NEW_BASE="$N_URL"

  {
    echo "# GitLab migration run"
    echo
    echo "| | |"; echo "|---|---|"
    echo "| Started | $(now) |"
    echo "| Server | $(hostname) (user $(id -un)) |"
    echo "| Mode | $([ "$DRY" = 1 ] && echo 'DRY RUN' || echo 'REAL RUN') |"
    echo "| Legacy | $L_URL |"
    echo "| New | $N_URL |"
    echo "| Starting at | ${FROM:-first step} |"
    echo "| Script | $SELF |"
    echo
    echo "Tokens and the sudo password are masked as \`***\`. Each step below shows the exact command and everything it printed."
  } >> "$MD"

  set_status RUNNING "config"
  { echo; echo "---"; echo; echo "## Setup - generated config"; echo; echo '```text'; make_config 2>&1 | mask; echo '```'; } >> "$MD"

  local started=0 id; [ -z "$FROM" ] && started=1
  for id in "${ALL_STEPS[@]}"; do
    [ "$id" = "$FROM" ] && started=1
    if [ "$started" = 0 ]; then record_skip "$id" "before --from $FROM"; continue; fi
    CURRENT="$id"
    "step_$id" || { FAILED_AT="$id"; break; }
  done

  {
    echo; echo "---"; echo; echo "## Summary"; echo
    echo "| Step | Result | Exit | Time | [ERROR] lines |"; echo "|---|---|---|---|---|"
    printf '%s\n' "${ROWS[@]}"; echo
    if [ -n "$FAILED_AT" ]; then
      echo "**STOPPED at step \`$FAILED_AT\`.** Later steps did not run. Read the output above, fix the cause,"
      echo "then continue with: \`./run_migration.sh start --from $FAILED_AT\`"
    elif [ "$WARNED" = 1 ]; then echo "**Finished with warnings** - see rows marked WARNING."
    else echo "**All steps finished.**"; fi
    if [ "${#ERR_SUMMARY[@]}" -gt 0 ]; then
      echo; echo "Lines containing [ERROR] (some are harmless, e.g. 409 'already a member'):"
      printf -- '- %s\n' "${ERR_SUMMARY[@]}"
    fi
    echo; echo "Finished: $(now)"
  } >> "$MD"

  if [ -n "$FAILED_AT" ]; then set_status "FAILED" "$FAILED_AT"; exit 1; fi
  set_status "$([ "$WARNED" = 1 ] && echo 'DONE with warnings' || echo 'DONE')" "all"
}

case "${1:-help}" in
  start)  shift; cmd_start "$@" ;;
  status) cmd_status ;;
  tail)   cmd_tail ;;
  stop)   cmd_stop ;;
  _run)   shift; cmd__run "$@" ;;
  *)      sed -n '2,28p' "$SELF" | sed 's/^# \{0,1\}//' ;;
esac
