# fix_links_metadata.rb
#
# Rewrites legacy GitLab URLs inside issue descriptions, MR descriptions,
# comments (notes) and milestone descriptions on the NEW GitLab.
# Uses update_columns, so updated_at (and everything else) is left untouched,
# then refreshes the cached rendered HTML.
#
# DRY RUN by default. Set APPLY=1 to write changes.
# Run on the new GitLab server (script must be readable by the 'git' user, so use /tmp):
#
#   cp fix_links_metadata.rb /tmp/ && chmod 644 /tmp/fix_links_metadata.rb
#   sudo LEGACY_BASES="http://180.210.129.233:8090,http://180.210.129.233" \
#        NEW_BASE="http://192.16.0.65" \
#        gitlab-rails runner /tmp/fix_links_metadata.rb          # dry run
#   sudo LEGACY_BASES="..." NEW_BASE="..." APPLY=1 \
#        gitlab-rails runner /tmp/fix_links_metadata.rb          # for real
#
# Run AFTER validate_migration.py passes.

old_bases = ENV.fetch('LEGACY_BASES') { abort 'ABORT: LEGACY_BASES not set' }
              .split(',').map { |s| s.strip.chomp('/') }.reject(&:empty?)
new_base  = ENV.fetch('NEW_BASE') { abort 'ABORT: NEW_BASE not set' }.chomp('/')
apply     = ENV['APPLY'] == '1'

abort 'ABORT: LEGACY_BASES is empty' if old_bases.empty?

# longest first, so the ":8090" variant is replaced before the bare host
old_bases.sort_by! { |b| -b.length }
patterns = old_bases.map { |b| "%#{b}%" }

puts "Mode: #{apply ? 'APPLY' : 'DRY RUN'}"
puts "Replacing #{old_bases.inspect} -> #{new_base}"
puts

TARGETS = [
  [Issue,        :description],
  [MergeRequest, :description],
  [Note,         :note],
  [Milestone,    :description]
].freeze

grand_total = 0

TARGETS.each do |klass, field|
  scope = klass.where("#{klass.table_name}.#{field} LIKE ANY (ARRAY[?])", patterns)
  changed = 0
  failed  = 0

  scope.find_each do |rec|
    old_text = rec[field]
    next if old_text.blank?

    new_text = old_text.dup
    old_bases.each { |b| new_text = new_text.gsub(b, new_base) }
    next if new_text == old_text

    changed += 1
    next unless apply

    begin
      rec.update_columns(field => new_text)
      begin
        rec.refresh_markdown_cache! if rec.respond_to?(:refresh_markdown_cache!)
      rescue StandardError
        # fall back: mark cache stale so GitLab re-renders on next view
        if klass.column_names.include?('cached_markdown_version')
          rec.update_columns(cached_markdown_version: nil)
        end
      end
    rescue StandardError => e
      failed += 1
      puts "  FAILED #{klass.name} id=#{rec.id}: #{e.class}: #{e.message}"
    end
  end

  puts format('%-14s %-12s %d record(s) %s%s',
              klass.name, field, changed,
              apply ? 'updated' : 'would change',
              failed.positive? ? " (#{failed} failed)" : '')
  grand_total += changed
end

puts
puts "Total records #{apply ? 'updated' : 'that would change'}: #{grand_total}"
puts 'Dry run only. Re-run with APPLY=1 to write changes.' unless apply
