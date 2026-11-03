# Changelog

## Unreleased

- Core: settings from the environment (with `*_FILE` secrets), the Kite HTTP client with an endpoint allow-list,
  error mapping and credential scrubbing, Kite-aware rate limiting, and a token store compatible with existing
  JSON token files (managed and external auth modes).
- Safety: dry-run-by-default writes, two-step confirmation tokens, per-order / daily value and count caps,
  allow-lists, kill switch, read-only mode, JSON-lines audit log with fsynced intents, no automatic write retries.
- Tools: profile and margins; orders (place, modify, cancel, order book, history, trades); safety status, audit log,
  kill switch.
- Docs: the fork-or-build decision (docs/DECISION.md).
