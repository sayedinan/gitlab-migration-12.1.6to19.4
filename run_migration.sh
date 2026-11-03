#!/usr/bin/env bash
#
# run_migration.sh - one-command GitLab migration (legacy 12.1.6 -> new GitLab)
#
# Run it ON THE NEW GitLab SERVER, from the folder that holds the migration
# scripts (migrate_gitlab.py, migrate_metadata.rb, validate_migration.py,
# migrate_wikis.py, fix_links.py, fix_links_metadata.rb, config.yaml).
#
#   ./run_migration.sh dry      rehearsal: preflight + Part A + Part B in dry-run mode (changes nothing)
#   ./run_migration.sh start    the real migration, in the BACKGROUND (survives closing the terminal)
#   ./run_migration.sh status   which phase is running / what passed or failed
#   ./run_migration.sh tail     follow the live transcript
#   ./run_migration.sh stop     stop the running migration (safe to resume afterwards)
#
# Options for dry / start:
#   --fg              run in the foreground instead (debugging)
#   --resume          continue on top of state left by an earlier run
#   --from PHASE      start at this phase (implies --resume)
#   --skip P1,P2      skip phases
#   --keep-going      do not stop when a phase ends with errors (dry mode does this by default)
#
# Phases (real run, in order):
#   preflight  partA  partB  validate  wikis  wikis_verify  links_files  links_meta
#
# Inputs: asked ONCE at start, typed hidden, held only in the memory of the background
# process (never written to the transcript or the package):
#   legacy GitLab URL + admin token, new GitLab URL + admin token (api scope), your sudo password.
# They can also be provided as environment variables (GITLAB_LEGACY_URL, GITLAB_LEGACY_TOKEN,
# GITLAB_NEW_URL, GITLAB_NEW_TOKEN, MIG_SUDO_PW) or in an optional migration.env next to this script.
#
# Output: ~/migration_runs/<mode>_<timestamp>/migration_transcript.md  - every command and its full
# output, as it happens, plus <run>.tar.gz at the end. Tokens, the sudo password and (by default)
# both URLs are replaced by placeholders. Set MIG_SHOW_URLS=1 to keep URLs visible.
# Config comes from config.yaml (allowlist, options); the real URLs are NOT read from it.
#
# Other knobs (environment): MIG_RUNS_DIR, MIG_DISK_FACTOR (default 12 = 1.2x), MIG_NO_RUBY_SYNC=1,
# LEGACY_BASES (comma list of legacy URL variants used by the link fixer).

set -uo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(dirname "$SCRIPT_PATH")"
export MIG_SCRIPT_DIR="$SCRIPT_DIR"
RUNS_DIR="${MIG_RUNS_DIR:-$HOME/migration_runs}"
STATE_DIR="$RUNS_DIR/state"
LOCK_FILE="$RUNS_DIR/.lock"

REAL_PHASES=(preflight partA partB validate wikis wikis_verify links_files links_meta)
DRY_PHASES=(preflight partA partB)
declare -A PHASE_DESC=(
  [preflight]="checks: tools, sudo, both APIs and tokens, disk space, leftover state"
  [partA]="users, groups, memberships, projects, git mirror, members, webhooks, protected branches, extras"
  [partB]="issues, merge requests, comments (Rails console script)"
  [validate]="compare legacy vs new (users, groups, projects, issues, MRs, commits)"
  [wikis]="copy wiki repositories"
  [wikis_verify]="compare wiki page counts"
  [links_files]="rewrite legacy links in repo files and wikis"
  [links_meta]="rewrite legacy links in issues, MRs, comments, milestones"
)

die() { echo "ERROR: $*" >&2; exit 1; }
ts()  { date '+%Y-%m-%d %H:%M:%S'; }

usage() { sed -n '2,37p' "$SCRIPT_PATH" | sed 's/^# \{0,1\}//'; }

# ----------------------------------------------------------------------------------------
# status / tail / stop
# ----------------------------------------------------------------------------------------
latest_run() { [ -L "$RUNS_DIR/latest" ] && readlink -f "$RUNS_DIR/latest"; }

is_running() { ( exec 9>"$LOCK_FILE" 2>/dev/null && ! flock -n 9 ) 2>/dev/null; }

cmd_status() {
  local run; run="$(latest_run)" || true
  [ -n "${run:-}" ] && [ -d "$run" ] || die "no runs found in $RUNS_DIR"
  if is_running; then echo "RUNNING"; else echo "NOT RUNNING"; fi
  echo "run folder: $run"
  echo
  cat "$run/STATUS" 2>/dev/null
  echo
  echo "--- last lines of the transcript ($run/migration_transcript.md) ---"
  tail -n 15 "$run/migration_transcript.md" 2>/dev/null
}

cmd_tail() {
  local run; run="$(latest_run)" || true
  [ -n "${run:-}" ] && [ -f "$run/migration_transcript.md" ] || die "no transcript found in $RUNS_DIR"
  echo "following $run/migration_transcript.md  (Ctrl+C only stops watching, not the migration)"
  tail -n 60 -f "$run/migration_transcript.md"
}

cmd_stop() {
  local run pid; run="$(latest_run)" || true
  [ -n "${run:-}" ] && [ -f "$run/pid" ] || die "no run to stop"
  pid="$(cat "$run/pid")"
  kill -0 "$pid" 2>/dev/null || die "the last run (pid $pid) is not running"
  echo "stopping run $run (pid $pid) ..."
  kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid"
  local i; for i in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
  kill -0 "$pid" 2>/dev/null && echo "still stopping - check: $0 status" || echo "stopped. Resume later with: $0 start --resume --from <phase>"
}

# ----------------------------------------------------------------------------------------
# launcher (foreground part): collect inputs, then start the worker in the background
# ----------------------------------------------------------------------------------------
ask() { # ask VAR "prompt" [plain]
  local var=$1 prompt=$2 val=""
  if [ -z "${!var:-}" ]; then
    [ -t 0 ] || die "$var is not set and there is no terminal to ask on."
    read -rs -p "$prompt: " val; echo
    [ "${3:-}" = plain ] || val="${val//[[:space:]]/}"
    printf -v "$var" '%s' "$val"
  fi
  [ -n "${!var}" ] || die "$var is empty"
}

select_phases() { # sets PHASES (array)
  local -a base; local p s started=0
  if [ "$MODE" = dry ]; then base=("${DRY_PHASES[@]}"); else base=("${REAL_PHASES[@]}"); fi
  if [ -n "$FROM" ]; then
    printf '%s\n' "${base[@]}" | grep -qx "$FROM" || die "--from: unknown phase '$FROM' for $MODE (${base[*]})"
    RESUME=1
  else started=1; fi
  for s in ${SKIP//,/ }; do printf '%s\n' "${REAL_PHASES[@]}" | grep -qx "$s" || die "--skip: unknown phase '$s'"; done
  PHASES=()
  for p in "${base[@]}"; do
    [ "$p" = "$FROM" ] && started=1
    [ $started -eq 1 ] || continue
    case ",$SKIP," in *",$p,"*) continue;; esac
    PHASES+=("$p")
  done
  [ ${#PHASES[@]} -gt 0 ] || die "no phases selected"
}

launch() {
  local c; for c in python3 flock awk setsid nohup; do command -v "$c" >/dev/null || die "required tool missing: $c"; done
  mkdir -p "$RUNS_DIR" "$STATE_DIR"; chmod 700 "$RUNS_DIR" "$STATE_DIR"
  select_phases
  exec 9>"$LOCK_FILE" || die "cannot open lock file $LOCK_FILE"
  flock -n 9 || die "a migration run is already in progress. See: $0 status   (never start two at once)"

  if [ -f "$SCRIPT_DIR/migration.env" ]; then
    [ "$(stat -c %a "$SCRIPT_DIR/migration.env")" = "600" ] || echo "WARNING: migration.env should be chmod 600" >&2
    # shellcheck disable=SC1091
    set -a; . "$SCRIPT_DIR/migration.env"; set +a
  fi
  if [ -z "${GITLAB_LEGACY_URL:-}${GITLAB_LEGACY_TOKEN:-}${GITLAB_NEW_URL:-}${GITLAB_NEW_TOKEN:-}" ]; then
    echo "Enter the connection details (typing is hidden; nothing is saved to disk):"
  fi
  ask GITLAB_LEGACY_URL   "  Legacy GitLab URL (e.g. http://host:8090)"
  ask GITLAB_LEGACY_TOKEN "  Legacy GitLab admin token (api scope)"
  ask GITLAB_NEW_URL      "  New GitLab URL"
  ask GITLAB_NEW_TOKEN    "  New GitLab admin token (api scope)"
  GITLAB_LEGACY_URL="${GITLAB_LEGACY_URL%/}"; GITLAB_NEW_URL="${GITLAB_NEW_URL%/}"
  case "$GITLAB_LEGACY_URL$GITLAB_NEW_URL" in http*://*http*://*) ;; *) die "both URLs must start with http:// or https://";; esac
  [ "$GITLAB_LEGACY_URL" != "$GITLAB_NEW_URL" ] || die "legacy and new URL are identical"

  if [ "$EUID" -ne 0 ]; then
    command -v sudo >/dev/null || die "sudo is not installed and you are not root"
    ask MIG_SUDO_PW "  sudo password for $(id -un)" plain
    { printf '%s\n' "$MIG_SUDO_PW" 2>/dev/null | sudo -S -p '' -k true; } 2>/dev/null || die "sudo password rejected (or this user may not use sudo)"
  fi

  local base n=1; base="${MODE}_$(date +%Y%m%d_%H%M%S)"; MIG_RUN_ID="$base"
  while ! mkdir -m 700 "$RUNS_DIR/$MIG_RUN_ID" 2>/dev/null; do
    n=$((n + 1)); MIG_RUN_ID="${base}_$n"; [ $n -lt 50 ] || die "cannot create a run folder in $RUNS_DIR"
  done
  MIG_RUN_DIR="$RUNS_DIR/$MIG_RUN_ID"
  ln -sfn "$MIG_RUN_DIR" "$RUNS_DIR/latest"
  export GITLAB_LEGACY_URL GITLAB_LEGACY_TOKEN GITLAB_NEW_URL GITLAB_NEW_TOKEN MIG_SUDO_PW MIG_RUN_ID MIG_RUN_DIR
  export MIG_MODE="$MODE" MIG_PHASES="${PHASES[*]}" MIG_RESUME="$RESUME" MIG_KEEP_GOING="$KEEP"
  export MIG_CLI="$0 $CMD ${ORIG_ARGS:-}"

  if [ "$FG" -eq 1 ]; then
    worker_main
    exit $?
  fi
  setsid nohup "$SCRIPT_PATH" _worker </dev/null >/dev/null 2>&1 &
  disown 2>/dev/null || true
  local i; for i in 1 2 3 4 5 6 7 8 9 10; do [ -f "$MIG_RUN_DIR/pid" ] && break; sleep 0.5; done
  echo
  echo "Started in the background (pid $(cat "$MIG_RUN_DIR/pid" 2>/dev/null || echo '?')). You can close this terminal."
  echo "  Phases     : ${PHASES[*]}"
  echo "  Transcript : $MIG_RUN_DIR/migration_transcript.md"
  echo "  Watch live : $0 tail"
  echo "  Check      : $0 status"
  echo "  Stop       : $0 stop"
}

# ----------------------------------------------------------------------------------------
# worker (background part)
# ----------------------------------------------------------------------------------------
setup_redaction() {
  local -a P=() R=(); local sep=$'\037' i
  add() { [ "${#1}" -ge 4 ] && { P+=("$1"); R+=("$2"); }; return 0; }
  host_of() { sed -E 's#^[a-zA-Z]+://([^/:]+).*#\1#' <<<"$1"; }
  add "${MIG_SUDO_PW:-}" "***SUDO-PASSWORD***"
  add "$GITLAB_LEGACY_TOKEN" "***LEGACY-TOKEN***"
  add "$GITLAB_NEW_TOKEN" "***NEW-TOKEN***"
  if [ "${MIG_SHOW_URLS:-0}" != 1 ]; then
    add "$GITLAB_LEGACY_URL" "<LEGACY_URL>"
    add "$GITLAB_NEW_URL" "<NEW_URL>"
    local lh nh; lh="$(host_of "$GITLAB_LEGACY_URL")"; nh="$(host_of "$GITLAB_NEW_URL")"
    case "$lh" in localhost|127.0.0.1) ;; *) add "$lh" "<LEGACY_HOST>";; esac
    case "$nh" in localhost|127.0.0.1) ;; *) add "$nh" "<NEW_HOST>";; esac
  fi
  RED_PATS=""; RED_REPS=""
  for ((i=0; i<${#P[@]}; i++)); do
    RED_PATS+="${P[$i]}$sep"; RED_REPS+="${R[$i]}$sep"
  done
  export RED_PATS RED_REPS
}

redact() {
  awk '
  BEGIN { n = split(ENVIRON["RED_PATS"], P, "\037"); split(ENVIRON["RED_REPS"], R, "\037") }
  {
    line = $0
    for (i = 1; i <= n; i++) {
      p = P[i]; if (p == "") continue
      out = ""
      while ((k = index(line, p)) > 0) { out = out substr(line, 1, k - 1) R[i]; line = substr(line, k + length(p)) }
      line = out line
    }
    print line; fflush()
  }'
}

md() { printf '%s\n' "$*" >> "$MD"; }

# run_cmd "<text shown in the transcript>" command args...
run_cmd() {
  local disp="$1"; shift
  local out="$WORK/out.$((++CMD_N)).txt" t0=$SECONDS rc errs
  { printf '\n**[%s]** command %d\n\n```bash\n$ %s\n```\n\n````text\n' "$(ts)" "$CMD_N" "$disp"; } >> "$MD"
  "$@" 2>&1 </dev/null | redact | tee "$out" >> "$MD"
  rc=${PIPESTATUS[0]}
  printf '````\n\n_exit code **%d** - %ds - %s_\n' "$rc" "$((SECONDS - t0))" "$(ts)" >> "$MD"
  errs=$(grep -cE '\[ERROR\]|FAILED|Traceback \(most recent' "$out" || true)
  PH_ERRS=$((PH_ERRS + errs)); [ "$rc" -ne 0 ] && [ "$PH_RC" -eq 0 ] && PH_RC=$rc
  LAST_OUT="$out"
  return "$rc"
}

as_root() {
  if [ "$EUID" -eq 0 ]; then "$@"; else printf '%s\n' "$MIG_SUDO_PW" | sudo -S -p '' "$@"; fi
}

# rails_run SCRIPT NAME=VALUE...  : run a Rails-runner script as root; the NAME=VALUE pairs (tokens, URLs)
# travel over stdin, so they never appear in 'ps' or in a file.
rails_run() {
  local script=$1; shift
  local mark="MIGMARK$RANDOM$RANDOM$RANDOM" kv
  local inner='m=$1; s=$2
while IFS= read -r l; do [ "$l" = "$m" ] && break; done
while IFS= read -r l; do [ "$l" = "$m-END" ] && break; export "$l"; done
exec gitlab-rails runner "$s"'
  {
    [ "$EUID" -ne 0 ] && printf '%s\n' "$MIG_SUDO_PW"
    printf '%s\n' "$mark"
    for kv in "$@"; do printf '%s\n' "$kv"; done
    printf '%s\n' "$mark-END"
  } | {
    if [ "$EUID" -eq 0 ]; then bash -c "$inner" _ "$mark" "$script"
    else sudo -S -p '' bash -c "$inner" _ "$mark" "$script"; fi
  }
}

write_status() {
  local i
  {
    echo "mode=$MODE"; echo "started=$START_TS"; echo "state=$RUN_STATE"; echo "current=${CURRENT:-}"
    echo "phases=${PHASES[*]}"
    for ((i=0; i<${#R_NAME[@]}; i++)); do
      printf 'phase %-13s %-7s exit=%s error_lines=%s %ss\n' "${R_NAME[$i]}" "${R_RES[$i]}" "${R_RC[$i]}" "${R_ERR[$i]}" "${R_DUR[$i]}"
    done
  } > "$RUN_DIR/STATUS.tmp" && mv "$RUN_DIR/STATUS.tmp" "$RUN_DIR/STATUS"
}

# ---- helper functions that run inside run_cmd ----
snapshot() {
  echo "host: $(hostname) | user: $(id -un) | $(date)"
  uname -srm
  echo "--- disk"; df -h "$RUNS_DIR" /tmp /var/opt/gitlab 2>/dev/null
  echo "--- memory"; free -h 2>/dev/null | head -2
  echo "--- versions"; python3 --version; git --version; git lfs version 2>&1 | head -1
  [ -r /opt/gitlab/version-manifest.txt ] && head -1 /opt/gitlab/version-manifest.txt
  return 0
}

check_tools() {
  local bad=0 t
  for t in python3 git git-lfs awk flock; do
    if command -v "$t" >/dev/null; then echo "ok       $t"; else echo "MISSING  $t"; bad=1; fi
  done
  python3 - <<'PY' || bad=1
import importlib, sys
bad = 0
for m in ("requests", "yaml"):
    try: importlib.import_module(m); print("ok       python module", m)
    except Exception: print("MISSING  python module", m, "(pip install requests pyyaml --break-system-packages)"); bad = 1
sys.exit(bad)
PY
  if as_root bash -c 'command -v gitlab-rails' >/dev/null 2>&1; then echo "ok       gitlab-rails (as root)"; else echo "MISSING  gitlab-rails - this must run on the new GitLab server"; bad=1; fi
  return $bad
}

preflight_api() {
python3 - <<'PY'
import os, sys, json, urllib.request, urllib.error
def get(base, tok, path):
    req = urllib.request.Request(base.rstrip("/") + "/api/v4" + path, headers={"PRIVATE-TOKEN": tok})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.load(r), r.headers
    except urllib.error.HTTPError as e:
        return e.code, None, e.headers
    except Exception as e:
        return 0, str(e), {}
bad = 0; total = 0
for side in ("legacy", "new"):
    base, tok = os.environ["GITLAB_%s_URL" % side.upper()], os.environ["GITLAB_%s_TOKEN" % side.upper()]
    st, ver, _ = get(base, tok, "/version")
    if st != 200:
        print("%-6s FAIL: cannot read /version (status %s) %s" % (side, st, ver if st == 0 else "- check URL and token")); bad = 1; continue
    st, me, _ = get(base, tok, "/user")
    admin = (me or {}).get("is_admin") if st == 200 else None
    print("%-6s GitLab %s, token user '%s', admin=%s" % (side, ver.get("version"), (me or {}).get("username"), admin))
    if st != 200: print("%-6s FAIL: token rejected (status %s)" % (side, st)); bad = 1
    elif admin is False: print("%-6s FAIL: token user is not an admin" % side); bad = 1
    counts = []
    for label, path in (("users", "/users?per_page=1"), ("groups", "/groups?all_available=true&per_page=1"), ("projects", "/projects?per_page=1")):
        s, _, h = get(base, tok, path)
        counts.append("%s=%s" % (label, h.get("X-Total", "?") if s == 200 else "?"))
    print("%-6s counts: %s" % (side, " ".join(counts)))
    if side == "legacy":
        page = 1
        while page <= 200:
            s, rows, _ = get(base, tok, "/projects?statistics=true&per_page=100&page=%d" % page)
            if s != 200 or not rows: break
            for p in rows:
                stt = p.get("statistics") or {}
                total += (stt.get("repository_size") or 0) + (stt.get("lfs_objects_size") or 0)
            page += 1
        print("legacy repositories + LFS total: %.1f GB" % (total / 1073741824.0))
print("TOTAL_BYTES=%d" % total)
sys.exit(bad)
PY
}

disk_check() {
  local need=${DISK_NEED:-0} target=/var/opt/gitlab avail f="${MIG_DISK_FACTOR:-12}"
  [ -d "$target" ] || target="$RUNS_DIR"
  avail=$(df --output=avail -B1 "$target" | tail -1 | tr -d ' ')
  echo "free on $target: $((avail / 1073741824)) GB; legacy data: $((need / 1073741824)) GB; required: legacy x $f/10"
  if [ "$need" -gt 0 ] && [ "$avail" -lt $((need * f / 10)) ]; then
    echo "NOT ENOUGH DISK SPACE on the new server. Free some space, or override with MIG_DISK_FACTOR / --skip preflight."
    return 1
  fi
  echo "disk space OK"
}

state_check() {
  local found=0 f
  for f in "$STATE_DIR/migration_state.json" "$STATE_DIR/wiki_migration_state.json" /tmp/metadata_migration_state.json; do
    [ -e "$f" ] && { echo "FOUND leftover state: $f"; found=1; }
  done
  if [ $found -eq 1 ]; then
    echo "A previous run left state behind. Continue it with --resume, or move the file(s) aside to start clean."
    return 1
  fi
  echo "no leftover state - clean start"
}

make_config() {
  python3 - "$CFG" "$MODE" "$ALLOWLIST_FILE" <<'PY'
import os, sys, yaml
out, mode, allow_out = sys.argv[1:4]
cfg = yaml.safe_load(open(os.path.join(os.environ["MIG_SCRIPT_DIR"], "config.yaml")))
cfg.setdefault("legacy", {})["url"] = os.environ["GITLAB_LEGACY_URL"]; cfg["legacy"]["token"] = "from-env"
cfg.setdefault("new", {})["url"] = os.environ["GITLAB_NEW_URL"];       cfg["new"]["token"] = "from-env"
cfg.setdefault("options", {})["dry_run"] = (mode == "dry")
fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    try: yaml.safe_dump(cfg, f, sort_keys=False)
    except TypeError: yaml.safe_dump(cfg, f)
allow = cfg.get("project_allowlist") or []
with open(allow_out, "w") as f: yaml.safe_dump({"project_allowlist": allow}, f)
os.chmod(allow_out, 0o644)
print("run config written (private, mode 600): dry_run=%s, project_allowlist=%d entr%s" % (mode == "dry", len(allow), "y" if len(allow) == 1 else "ies"))
PY
}

# ---- phases ----
phase_preflight() {
  run_cmd "system snapshot (host, user, disk, memory, tool versions)" snapshot || return
  run_cmd "check required tools and python modules" check_tools || return
  run_cmd "API check: both GitLabs reachable, tokens valid + admin, object counts, legacy data size (URLs/tokens hidden)" preflight_api || return
  DISK_NEED=$(grep -m1 '^TOTAL_BYTES=' "$LAST_OUT" | cut -d= -f2); DISK_NEED=${DISK_NEED:-0}
  run_cmd "disk space check on the new server" disk_check || return
  if [ "$MODE" = real ] && [ "$RESUME" != 1 ]; then
    run_cmd "leftover-state check (use --resume to continue an earlier run)" state_check || return
  fi
}

phase_partA() {
  local -a a=(python3 -u "$SCRIPT_DIR/migrate_gitlab.py" --config "$CFG"); local d="python3 migrate_gitlab.py --config <private run config>"
  [ "$MODE" = dry ] && { a+=(--dry-run); d+=" --dry-run"; }
  run_cmd "$d" "${a[@]}"
}

rails_env_common() { # sets REN array: shim + extra
  REN=()
  [ "${MIG_NO_RUBY_SYNC:-0}" = 1 ] || REN+=("RUBYOPT=-r$SYNC_RB")
}

phase_partB() {
  local sc="/tmp/${MIG_RUN_ID}_migrate_metadata.rb"
  run_cmd "install -m 644 migrate_metadata.rb $sc" install -m 644 "$SCRIPT_DIR/migrate_metadata.rb" "$sc" || return
  rails_env_common
  REN+=("GITLAB_LEGACY_URL=$GITLAB_LEGACY_URL" "GITLAB_LEGACY_TOKEN=$GITLAB_LEGACY_TOKEN" "CONFIG_YAML_PATH=$ALLOWLIST_FILE")
  local d="sudo gitlab-rails runner $sc   # env passed over stdin: GITLAB_LEGACY_URL, GITLAB_LEGACY_TOKEN, CONFIG_YAML_PATH, RUBYOPT"
  [ "$MODE" = dry ] && { REN+=("DRY_RUN=true"); d+=", DRY_RUN=true"; }
  echo_wait_note
  run_cmd "$d" rails_run "$sc" "${REN[@]}"
}
echo_wait_note() { md ""; md "_Rails takes 1-3 minutes to start; the output appears after that._"; }

phase_validate()     { run_cmd "python3 validate_migration.py --config <private run config>" python3 -u "$SCRIPT_DIR/validate_migration.py" --config "$CFG"; }
phase_wikis()        { run_cmd "python3 migrate_wikis.py --apply --config <private run config>" python3 -u "$SCRIPT_DIR/migrate_wikis.py" --apply --config "$CFG"; }
phase_wikis_verify() { run_cmd "python3 migrate_wikis.py --verify --config <private run config>" python3 -u "$SCRIPT_DIR/migrate_wikis.py" --verify --config "$CFG"; }
phase_links_files()  { run_cmd "LEGACY_BASES=<derived from legacy URL> python3 fix_links.py --apply --wikis" python3 -u "$SCRIPT_DIR/fix_links.py" --apply --wikis; }

phase_links_meta() {
  local sc="/tmp/${MIG_RUN_ID}_fix_links_metadata.rb"
  run_cmd "install -m 644 fix_links_metadata.rb $sc" install -m 644 "$SCRIPT_DIR/fix_links_metadata.rb" "$sc" || return
  rails_env_common
  REN+=("LEGACY_BASES=$LEGACY_BASES" "NEW_BASE=$GITLAB_NEW_URL" "APPLY=1")
  run_cmd "sudo gitlab-rails runner $sc   # env passed over stdin: LEGACY_BASES, NEW_BASE, APPLY=1, RUBYOPT" rails_run "$sc" "${REN[@]}"
}

run_phase() { # run_phase name idx total ; returns 1 = stop the run
  local name=$1 idx=$2 total=$3 res t0=$SECONDS
  CURRENT=$name; RUN_STATE=RUNNING; write_status
  PH_RC=0; PH_ERRS=0
  md ""; md "---"; md ""; md "## Phase $idx/$total - \`$name\` - ${PHASE_DESC[$name]}"; md "_started $(ts)_"
  "phase_$name"
  local dur=$((SECONDS - t0))
  if [ "$PH_RC" -ne 0 ]; then res=FAILED; elif [ "$PH_ERRS" -gt 0 ]; then res=ERRORS; else res=OK; fi
  R_NAME+=("$name"); R_RES+=("$res"); R_RC+=("$PH_RC"); R_ERR+=("$PH_ERRS"); R_DUR+=("$dur")
  md ""; md "**Result of \`$name\`: $res** (exit code $PH_RC, $PH_ERRS error line(s), ${dur}s)"
  write_status
  [ "$res" = OK ] && return 0
  if [ "$name" = preflight ]; then md "**Stopping: preflight did not pass.**"; return 1; fi
  if [ "$KEEP" = 1 ]; then md "_--keep-going: continuing despite $res._"; return 0; fi
  md "**Stopping after \`$name\` ($res).** Fix the cause, then re-run: \`$0 start --resume --from $name\`"
  return 1
}

finalize() {
  [ "${FINALIZED:-0}" = 1 ] && return; FINALIZED=1
  local i f
  mkdir -p "$RUN_DIR/artifacts"
  for f in "$STATE_DIR/migration_state.json" "$STATE_DIR/migration.log" "$STATE_DIR/wiki_migration_state.json" /tmp/metadata_migration_state.json; do
    [ -r "$f" ] && redact < "$f" > "$RUN_DIR/artifacts/$(basename "$f")"
  done
  md ""; md "---"; md ""; md "## Summary"; md ""
  md "| # | Phase | Result | Exit | Error lines | Seconds |"; md "|---|-------|--------|------|-------------|---------|"
  for ((i=0; i<${#R_NAME[@]}; i++)); do
    md "| $((i+1)) | ${R_NAME[$i]} | ${R_RES[$i]} | ${R_RC[$i]} | ${R_ERR[$i]} | ${R_DUR[$i]} |"
  done
  md ""; md "Finished: $(ts) - overall: **$FINAL_TEXT**"
  if [ "$FINAL_TEXT" = "ALL PHASES OK" ] && [ "$MODE" = real ]; then
    md ""; md "Manual follow-up (not automated): re-enter webhook secret tokens; users log in with the temporary password and re-add failed SSH keys; update CI settings / git remotes; revoke both migration tokens."
  fi
  RUN_STATE="$FINAL_STATE"; CURRENT=""; write_status
  rm -rf "$PRIVATE" "$WORK" /tmp/"${MIG_RUN_ID}"_* 2>/dev/null
  ( cd "$RUNS_DIR" && tar -czf "$MIG_RUN_ID.tar.gz" "$MIG_RUN_ID" 2>/dev/null )
}

on_signal() {
  md ""; md "**[$(ts)] Received $1 - run stopped by user/system.** Resume with: \`$0 start --resume --from ${CURRENT:-partA}\`"
  FINAL_TEXT="STOPPED ($1)"; FINAL_STATE="STOPPED"
  finalize; exit 130
}

worker_main() {
  MODE=$MIG_MODE; RESUME=$MIG_RESUME; KEEP=$MIG_KEEP_GOING; RUN_DIR=$MIG_RUN_DIR
  read -r -a PHASES <<<"$MIG_PHASES"
  [ "$MODE" = dry ] && [ -z "$KEEP" ] && KEEP=1
  MD="$RUN_DIR/migration_transcript.md"; PRIVATE="$RUN_DIR/.private"; WORK="$RUN_DIR/.work"
  mkdir -p -m 700 "$PRIVATE" "$WORK"; echo $$ > "$RUN_DIR/pid"
  CFG="$PRIVATE/config.run.yaml"; ALLOWLIST_FILE="/tmp/${MIG_RUN_ID}_allowlist.yaml"; SYNC_RB="/tmp/${MIG_RUN_ID}_sync.rb"
  printf '$stdout.sync = true\n$stderr.sync = true\n' > "$SYNC_RB"; chmod 644 "$SYNC_RB"
  PH_RC=0; PH_ERRS=0; CMD_N=0; R_NAME=(); R_RES=(); R_RC=(); R_ERR=(); R_DUR=(); RUN_STATE=RUNNING; FINAL_TEXT="ABORTED"; FINAL_STATE="ABORTED"
  START_TS="$(ts)"
  export PYTHONUNBUFFERED=1 GIT_TERMINAL_PROMPT=0
  trap 'on_signal TERM' TERM; trap 'on_signal INT' INT; trap 'on_signal HUP' HUP
  setup_redaction
  if [ -z "${LEGACY_BASES:-}" ]; then
    local np; np="$(sed -E 's#^(https?://[^/:]+):[0-9]+#\1#' <<<"$GITLAB_LEGACY_URL")"
    LEGACY_BASES="$GITLAB_LEGACY_URL"; [ "$np" != "$GITLAB_LEGACY_URL" ] && LEGACY_BASES="$GITLAB_LEGACY_URL,$np"
  fi
  export LEGACY_BASES
  cd "$STATE_DIR" || exit 1

  md "# GitLab migration run - $MODE"; md ""
  md "| | |"; md "|---|---|"
  md "| Started | $START_TS |"; md "| Host / user | $(hostname) / $(id -un) |"
  md "| Command line | \`$MIG_CLI\` |"; md "| Run folder | \`$RUN_DIR\` |"
  md "| Phases | ${PHASES[*]} |"; md "| Resume mode | $RESUME |"; md "| Keep going on errors | ${KEEP:-0} |"
  md "| Inputs given | legacy URL, legacy token, new URL, new token, sudo password - **values never recorded** |"
  md "| Redaction | tokens, sudo password$([ "${MIG_SHOW_URLS:-0}" = 1 ] || echo ', both URLs') replaced by placeholders |"
  md ""; md "## Setup"
  write_status
  run_cmd "sha256sum of the scripts used (audit trail)" bash -c "cd '$SCRIPT_DIR' && sha256sum run_migration.sh config.yaml migrate_gitlab.py migrate_metadata.rb validate_migration.py migrate_wikis.py fix_links.py fix_links_metadata.rb 2>&1"
  run_cmd "generate private run config from config.yaml (real URLs from the prompt, dry_run=$([ "$MODE" = dry ] && echo true || echo false))" make_config
  if [ "$PH_RC" -ne 0 ]; then
    md "**Stopping: setup failed.**"; FINAL_TEXT="SETUP FAILED"; FINAL_STATE="STOPPED"; finalize; return 1
  fi

  local i=0 total=${#PHASES[@]} p stopped=0 anyfail=0
  for p in "${PHASES[@]}"; do
    i=$((i + 1))
    run_phase "$p" "$i" "$total" || { stopped=1; break; }
  done
  for ((i=0; i<${#R_RES[@]}; i++)); do [ "${R_RES[$i]}" != OK ] && anyfail=1; done
  if [ $stopped -eq 1 ]; then FINAL_TEXT="STOPPED at ${R_NAME[$((${#R_NAME[@]}-1))]}"; FINAL_STATE="STOPPED"
  elif [ $anyfail -eq 1 ]; then FINAL_TEXT="FINISHED WITH ERRORS"; FINAL_STATE="FINISHED_WITH_ERRORS"
  else FINAL_TEXT="ALL PHASES OK"; FINAL_STATE="FINISHED_OK"; fi
  finalize
  [ "$FINAL_STATE" = FINISHED_OK ]
}

# ----------------------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------------------
if [ "${1:-}" = "_worker" ]; then worker_main; exit $?; fi

CMD="${1:-help}"; [ $# -gt 0 ] && shift
ORIG_ARGS="$*"
FG=0; RESUME=0; FROM=""; SKIP=""; KEEP=""
while [ $# -gt 0 ]; do
  case "$1" in
    --fg) FG=1;; --resume) RESUME=1;; --keep-going) KEEP=1;;
    --from) FROM="${2:-}"; shift;; --skip) SKIP="${2:-}"; shift;;
    -h|--help) usage; exit 0;;
    *) die "unknown option: $1 (see: $0 help)";;
  esac; shift
done

case "$CMD" in
  dry)    MODE=dry;  launch;;
  start)  MODE=real; launch;;
  status) cmd_status;;
  tail)   cmd_tail;;
  stop)   cmd_stop;;
  help|-h|--help) usage;;
  *) die "unknown command '$CMD' (use: dry | start | status | tail | stop)";;
esac
