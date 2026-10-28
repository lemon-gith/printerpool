# Shared by the cron job scripts (sourced, not run). Loads deploy/jobs.env and reports each run to an
# optional monitoring URL, so a job that fails, or silently stops running, gets noticed.
#
# Uses the Healthchecks.io convention (also supported by Uptime Kuma push monitors, Cronitor, etc.):
#   <url>/start when the job starts, <url> on success, <url>/fail on failure.
# Usage, in a script with `set -eu`:   monitor_job "${SOME_PING_URL:-}"

[ -f deploy/jobs.env ] && . deploy/jobs.env

_ping() {
    # Never let monitoring itself break a job: a short timeout, a few retries, and only a warning on failure.
    curl -fsS -m 10 --retry 3 -o /dev/null "$1" 2>/dev/null || echo "warning: couldn't reach monitoring URL" >&2
}

monitor_job() {
    _monitor_url=${1%/}
    [ -n "$_monitor_url" ] || return 0
    _ping "$_monitor_url/start"
    # Runs when the script exits, however it exits: success pings the URL, anything else pings /fail.
    trap '_status=$?; if [ "$_status" -eq 0 ]; then _ping "$_monitor_url"; else _ping "$_monitor_url/fail"; fi; exit "$_status"' EXIT
}
