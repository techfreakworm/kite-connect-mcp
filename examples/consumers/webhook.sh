#!/bin/sh
# Example consumer: POST each event as JSON to WEBHOOK_URL (list WEBHOOK_URL in the consumer's env_pass).
# Contract (docs/EVENTS.md): the event arrives on stdin; exit 0 = handled, 75 = retry later, anything else = permanent.
# No answer, a timeout, a 5xx, a 408 (timed out) or a 429 (rate limited) is worth retrying (exit 75). Any other 4xx,
# a bad WEBHOOK_URL or a missing curl will not change by itself (exit 1).
# The URL reaches curl as a config on file descriptor 3, not as an argument: other users can see a command's
# arguments (ps), and a webhook URL often holds a secret. For the same reason curl runs silent (its messages name
# parts of the URL) and the script prints messages of its own: stderr reaches the log and other consumers' events.
# -q, which must come first, makes curl skip ~/.curlrc: a "verbose" there would print the URL.
set -u
[ -n "${WEBHOOK_URL:-}" ] || { echo "webhook.sh: set WEBHOOK_URL" >&2; exit 1; }
command -v curl >/dev/null 2>&1 || { echo "webhook.sh: curl is not installed" >&2; exit 1; }
code=$(curl -q -s -o /dev/null -w '%{http_code}' --max-time 10 -H 'Content-Type: application/json' --data-binary @- \
  -K /dev/fd/3 3<<EOF
url = "$WEBHOOK_URL"
EOF
) || {
  rc=$?
  case "$rc" in
    1|3) echo "webhook.sh: check WEBHOOK_URL (curl exit code $rc)" >&2; exit 1 ;;  # an unknown scheme, a malformed URL
    *) echo "webhook.sh: no answer from WEBHOOK_URL (curl exit code $rc)" >&2; exit 75 ;;
  esac
}
case "$code" in
  2??) exit 0 ;;
  408|429) echo "webhook.sh: HTTP $code" >&2; exit 75 ;;
  4??) echo "webhook.sh: HTTP $code" >&2; exit 1 ;;
  *) echo "webhook.sh: HTTP $code" >&2; exit 75 ;;
esac
