#!/usr/bin/env bash
# Pre-push check: no secrets or personal data in what is (or is about to be) tracked by git.
#
#   1. gitleaks over the full history and the working tree (via Docker if the binary is not installed)
#   2. every tracked file grepped for local patterns that must never be published:
#        .pii-patterns        one fixed string per line (your domain, account id, email...), gitignored
#        .pii-secret-files    one path per line to files whose CONTENTS must never appear (API secret, tokens), gitignored
#      Matches are reported as file:line only; the secret itself is never printed.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
fail=0

if command -v gitleaks >/dev/null 2>&1; then
  gitleaks detect --source . --redact --no-banner || fail=1
elif command -v docker >/dev/null 2>&1; then
  docker run --rm -v "$PWD":/repo -w /repo zricethezav/gitleaks:v8.24.3 detect --source /repo --redact --no-banner || fail=1
else
  echo "prepush: gitleaks not available (install it or Docker); skipping the generic scan" >&2
fi

patterns=$(mktemp)
trap 'rm -f "$patterns"' EXIT
[ -f .pii-patterns ] && grep -v -e '^\s*$' -e '^#' .pii-patterns >>"$patterns" || true
if [ -f .pii-secret-files ]; then
  # the secret VALUES: a bare value file as is; a JSON file's string values under keys naming a token/secret/key
  python3 -I - .pii-secret-files >>"$patterns" <<'PY'
import json, os, sys
for raw in open(sys.argv[1], encoding="utf-8"):
    path = os.path.expanduser(raw.strip())
    if not path or path.startswith("#") or not os.access(path, os.R_OK):
        continue
    text = open(path, encoding="utf-8", errors="replace").read().strip()
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    values = []
    if isinstance(data, dict):
        values = [v for k, v in data.items() if isinstance(v, str) and any(w in k.lower() for w in ("token", "secret", "key"))]
    elif "\n" not in text:
        values = [text]
    for value in values:
        if len(value) >= 8:
            print(value)
PY
fi
if [ -s "$patterns" ]; then
  if git ls-files -z | xargs -0 grep -n -F -f "$patterns" -I -l 2>/dev/null; then
    echo "prepush: the files above contain a local PII/secret pattern (values not shown)" >&2
    fail=1
  fi
  if git log --all -p --format= | grep -q -F -f "$patterns"; then
    echo "prepush: git history contains a local PII/secret pattern (values not shown)" >&2
    fail=1
  fi
fi

[ "$fail" -eq 0 ] && echo "prepush: clean" || { echo "prepush: FAILED" >&2; exit 1; }
