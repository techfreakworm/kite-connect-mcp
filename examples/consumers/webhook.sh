#!/bin/sh
# Example consumer: POST each event as JSON to WEBHOOK_URL (list WEBHOOK_URL in the consumer's env_pass).
# Contract (docs/EVENTS.md): the event arrives on stdin; exit 0 = handled, 75 = retry later, anything else = permanent.
# A 4xx answer will not change by itself (exit 1); no answer, a timeout or a 5xx is worth retrying (exit 75).
# The URL reaches curl as a config on file descriptor 3, not as an argument: other users can see a command's
# arguments (ps), and a webhook URL often holds a secret. For the same reason curl runs silent (its messages name
# parts of the URL) and the script prints messages of its own: stderr reaches the log and other consumers' events.
set -u
[ -n "${WEBHOOK_URL:-}" ] || { echo "webhook.sh: set WEBHOOK_URL" >&2; exit 1; }
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 -H 'Content-Type: application/json' --data-binary @- \
  -K /dev/fd/3 3<<EOF
url = "$WEBHOOK_URL"
EOF
) || { rc=$?; echo "webhook.sh: no answer from WEBHOOK_URL (curl exit code $rc)" >&2; exit 75; }
case "$code" in
  2??) exit 0 ;;
  4??) echo "webhook.sh: HTTP $code" >&2; exit 1 ;;
  *) echo "webhook.sh: HTTP $code" >&2; exit 75 ;;
esac
