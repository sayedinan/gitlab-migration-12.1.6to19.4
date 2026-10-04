# GitLab Migration Script — Part B (metadata: issues, MRs, notes)
#
# Run this ON THE NEW INSTANCE via:
#   sudo gitlab-rails runner migrate_metadata.rb
#
# It pulls issues/MRs/notes from the LEGACY instance's API, and recreates
# them here using ActiveRecord directly, so author + created_at survive
# exactly (confirmed necessary — the REST API cannot set both at once).
#
# Run Part A (migrate_gitlab.py) FIRST — this script assumes users, groups,
# projects, and repo history (with all branches) already exist on this instance.
#
# STATE FILE FORMAT CHANGED: issues/mrs entries are now {"iid" => N} instead of
# `true`, so a note failure can no longer duplicate the parent issue/MR on
# retry. Delete any /tmp/metadata_migration_state.json left over from a run
# with the old script version before using this one — do not mix the two.

require "net/http"
require "json"
require "uri"
require "yaml"

# ---- CONFIG: fill these in (or set via env vars) ----
LEGACY_URL   = ENV["GITLAB_LEGACY_URL"] || "CHANGE_ME"   # e.g. https://old-gitlab.example.com
LEGACY_TOKEN = ENV["GITLAB_LEGACY_TOKEN"] || "CHANGE_ME"
DRY_RUN      = ENV["DRY_RUN"] == "true"
STATE_FILE = "/tmp/metadata_migration_state.json"

# project_allowlist now comes from config.yaml's `project_allowlist` key -
# the SAME file migrate_gitlab.py and validate_migration.py read - so scope
# only ever needs to be set in one place. Path can be overridden with
# CONFIG_YAML_PATH if config.yaml isn't copied to /tmp alongside this script.
CONFIG_YAML_PATH = ENV["CONFIG_YAML_PATH"] || "/tmp/config.yaml"
PROJECT_ALLOWLIST =
  if File.exist?(CONFIG_YAML_PATH)
    (YAML.safe_load(File.read(CONFIG_YAML_PATH))["project_allowlist"] || [])
  else
    puts "[WARN] #{CONFIG_YAML_PATH} not found - PROJECT_ALLOWLIST defaulting to empty (all projects)." \
         " Copy config.yaml next to this script, or set CONFIG_YAML_PATH, to scope this run."
    []
  end
# --------------------------------

if LEGACY_URL == "CHANGE_ME" || LEGACY_TOKEN == "CHANGE_ME"
  abort "Set GITLAB_LEGACY_URL and GITLAB_LEGACY_TOKEN before running this script."
end

def log(msg, level = "INFO")
  puts "[#{Time.now.strftime('%Y-%m-%d %H:%M:%S')}] [#{level}] #{msg}"
end

def legacy_get(path, params = {})
  results = []
  page = 1
  loop do
    uri = URI("#{LEGACY_URL}/api/v4#{path}")
    query = params.merge(per_page: 100, page: page)
    uri.query = URI.encode_www_form(query)
    req = Net::HTTP::Get.new(uri)
    req["PRIVATE-TOKEN"] = LEGACY_TOKEN
    res = Net::HTTP.start(uri.hostname, uri.port, use_ssl: uri.scheme == "https") { |http| http.request(req) }
    break unless res.is_a?(Net::HTTPSuccess)
    batch = JSON.parse(res.body)
    break if batch.empty?
    results.concat(batch)
    break if batch.size < 100
    page += 1
  end
  results
end

def load_state
  File.exist?(STATE_FILE) ? JSON.parse(File.read(STATE_FILE)) : { "issues" => {}, "mrs" => {}, "notes" => {} }
end

def save_state(state)
  File.write(STATE_FILE, JSON.pretty_generate(state))
end

state = load_state

# Cache users to avoid repeat DB lookups
user_cache = {}
def find_user(username, cache)
  return cache[username] if cache.key?(username)
  u = User.find_by_username(username)
  cache[username] = u
  u
end

# ---- post-create enrichment (state timestamps + assignees) ----
# Called AFTER the issue/MR is created and recorded in the state file, inside its
# own rescue, so a failure here only logs a warning - it can never cause a
# duplicate issue/MR on re-run.
def enrich_issue(issue, li, cache)
  updates = {}
  if li["state"] == "closed"
    updates[:closed_at] = Time.parse(li["closed_at"]) if li["closed_at"]
    cb = li["closed_by"] && find_user(li.dig("closed_by", "username"), cache)
    updates[:closed_by_id] = cb.id if cb
  end
  issue.update_columns(updates) unless updates.empty?
  assignees = (li["assignees"] || []).map { |a| find_user(a["username"], cache) }.compact
  issue.assignees = assignees unless assignees.empty?
end

def enrich_mr(mr, lm, cache)
  attrs = {}
  if lm["state"] == "merged"
    attrs[:merged_at] = Time.parse(lm["merged_at"]) if lm["merged_at"]
    mb = lm["merged_by"] && find_user(lm.dig("merged_by", "username"), cache)
    attrs[:merged_by_id] = mb.id if mb
  elsif lm["state"] == "closed"
    attrs[:latest_closed_at] = Time.parse(lm["closed_at"]) if lm["closed_at"]
    cb = lm["closed_by"] && find_user(lm.dig("closed_by", "username"), cache)
    attrs[:latest_closed_by_id] = cb.id if cb
  end
  mr.metrics.update_columns(attrs) if attrs.any? && mr.metrics
  assignees = (lm["assignees"] || []).map { |a| find_user(a["username"], cache) }.compact
  mr.assignees = assignees unless assignees.empty?
end

legacy_projects = legacy_get("/projects", { membership: false })
legacy_projects.each do |lp|
  path = lp["path_with_namespace"]
  next if PROJECT_ALLOWLIST.any? && !PROJECT_ALLOWLIST.include?(path)

  project = Project.find_by_full_path(path)
  unless project
    log("Project #{path} not found on new instance — run Part A first, skipping", "WARN")
    next
  end

  log("=== Project: #{path} ===")

  # ---------------- ISSUES ----------------
  # NOTE: issue creation and note creation are tracked and rescued SEPARATELY.
  # This is the fix for the duplicate-issue bug: state["issues"][key] is saved
  # immediately after Issue.create! succeeds, BEFORE any notes are touched, so
  # a note failure can never cause a re-run to create the same issue twice.
  # A failing note is rescued and logged individually instead of crashing the
  # whole run or being silently skipped.
  legacy_issues = legacy_get("/projects/#{lp['id']}/issues", { scope: "all" })
  legacy_issues.each do |li|
    key = "#{path}##{li['iid']}"

    author = find_user(li.dig("author", "username"), user_cache)
    unless author
      log("Author #{li.dig('author', 'username')} not found for issue #{key}, skipping", "WARN")
      next
    end

    if DRY_RUN
      log("[DRY RUN] Would create/continue issue #{key}: #{li['title']}")
      next
    end

    existing = state["issues"][key]
    if existing
      issue = Issue.find_by(project: project, iid: existing["iid"])
      unless issue
        log("Issue #{key} was marked done but not found on new instance (iid #{existing['iid']}) —" \
            " skipping notes for it; investigate manually", "ERROR")
        next
      end
    else
      begin
        issue = Issue.create!(
          project: project,
          title: li["title"],
          description: li["description"] || "",
          author: author,
          state: li["state"] == "closed" ? "closed" : "opened",
          created_at: Time.parse(li["created_at"]),
          updated_at: Time.parse(li["updated_at"] || li["created_at"]),
        )
        log("Created issue #{key} -> new iid #{issue.iid}")
        state["issues"][key] = { "iid" => issue.iid }
        save_state(state)
        begin
          enrich_issue(issue, li, user_cache)
        rescue => e2
          log("Issue #{key} created, but closed-state/assignee enrichment failed: #{e2.message}", "WARN")
        end
      rescue => e
        log("Failed to create issue #{key}: #{e.message}", "ERROR")
        next
      end
    end

    # ---- notes on this issue ----
    legacy_notes = legacy_get("/projects/#{lp['id']}/issues/#{li['iid']}/notes")
    legacy_notes.each do |ln|
      next if ln["system"] # skip system-generated notes, only real comments
      note_author = find_user(ln.dig("author", "username"), user_cache) || author
      note_key = "#{key}:note:#{ln['id']}"
      next if state["notes"][note_key]
      if DRY_RUN
        log("[DRY RUN]   Would add note by #{note_author.username} to #{key}")
        next
      end
      begin
        Note.create!(
          noteable: issue,
          project: project,
          author: note_author,
          note: ln["body"],
          created_at: Time.parse(ln["created_at"]),
          updated_at: Time.parse(ln["updated_at"] || ln["created_at"]),
        )
        state["notes"][note_key] = true
        save_state(state)
      rescue => e
        log("Failed to create note #{note_key} on issue #{key}: #{e.message}" \
            " — issue itself is fine, this note will be retried on next run", "ERROR")
      end
    end
  end

  # ---------------- MERGE REQUESTS ----------------
  # Same fix as issues above: MR creation and note creation are tracked and
  # rescued separately, so a failing comment can never cause a retry to
  # create a second, duplicate MR.
  legacy_mrs = legacy_get("/projects/#{lp['id']}/merge_requests", { scope: "all" })
  legacy_mrs.each do |lm|
    key = "#{path}!#{lm['iid']}"

    author = find_user(lm.dig("author", "username"), user_cache)
    unless author
      log("Author #{lm.dig('author', 'username')} not found for MR #{key}, skipping", "WARN")
      next
    end

    branch_names = project.repository.branch_names
    unless branch_names.include?(lm["source_branch"]) && branch_names.include?(lm["target_branch"])
      log("MR #{key} references missing branch(es) (#{lm['source_branch']} -> #{lm['target_branch']}), skipping", "WARN")
      next
    end

    if DRY_RUN
      log("[DRY RUN] Would create/continue MR #{key}: #{lm['title']}")
      next
    end

    existing = state["mrs"][key]
    if existing
      mr = MergeRequest.find_by(target_project: project, iid: existing["iid"])
      unless mr
        log("MR #{key} was marked done but not found on new instance (iid #{existing['iid']}) —" \
            " skipping notes for it; investigate manually", "ERROR")
        next
      end
    else
      begin
        mr = MergeRequest.create!(
          target_project: project,
          source_project: project,
          target_branch: lm["target_branch"],
          source_branch: lm["source_branch"],
          title: lm["title"],
          description: lm["description"] || "",
          author: author,
          state: lm["state"] == "merged" ? "merged" : (lm["state"] == "closed" ? "closed" : "opened"),
          created_at: Time.parse(lm["created_at"]),
          updated_at: Time.parse(lm["updated_at"] || lm["created_at"]),
        )
        log("Created MR #{key} -> new iid #{mr.iid}")
        state["mrs"][key] = { "iid" => mr.iid }
        save_state(state)
        begin
          enrich_mr(mr, lm, user_cache)
        rescue => e2
          log("MR #{key} created, but merged/closed-state/assignee enrichment failed: #{e2.message}", "WARN")
        end
      rescue => e
        log("Failed to create MR #{key}: #{e.message}", "ERROR")
        next
      end
    end

    legacy_notes = legacy_get("/projects/#{lp['id']}/merge_requests/#{lm['iid']}/notes")
    legacy_notes.each do |ln|
      next if ln["system"]
      note_author = find_user(ln.dig("author", "username"), user_cache) || author
      note_key = "#{key}:note:#{ln['id']}"
      next if state["notes"][note_key]
      begin
        Note.create!(
          noteable: mr,
          project: project,
          author: note_author,
          note: ln["body"],
          created_at: Time.parse(ln["created_at"]),
          updated_at: Time.parse(ln["updated_at"] || ln["created_at"]),
        )
        state["notes"][note_key] = true
        save_state(state)
      rescue => e
        log("Failed to create note #{note_key} on MR #{key}: #{e.message}" \
            " — MR itself is fine, this note will be retried on next run", "ERROR")
      end
    end
  end
end

log("Done.")
