# Contributing

Thanks for helping. This server can place real orders on real brokerage accounts. Changes are judged first by
whether they keep it safe, then by everything else.

## Ground rules

- **No secrets, ever.** No API keys, secrets, access tokens, request tokens, bearer tokens, account IDs or real
  portfolio data in code, tests, fixtures, docs, issues or commit messages. Fixtures use obviously fake values.
  Run `scripts/prepush_check.sh` before you push.
- **Tests never touch the network.** Kite is faked in-process (`tests/conftest.py::FakeKite`), and a guard fails any
  test that tries to reach a non-loopback address. Never run tests against a live account.
- **Every write goes through the safety pipeline.** A tool that changes the account builds a `WriteRequest` and calls
  `rt.pipeline.run(...)`. It never calls the HTTP client's write path directly. `tests/test_invariants.py` checks
  that every write tool is a dry run by default and sends nothing.
- **Writes are never retried automatically.** If you add a write endpoint, add it to `ENDPOINTS` in
  `src/kite_connect_mcp/client/http.py` with `kind="write"`.
- Keep the defaults safe: dry run, low caps, loopback-only HTTP.

## Development setup

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy
```

## Adding a tool

1. Pick the module in `src/kite_connect_mcp/tools/` for its toolset (or add a toolset to `config.TOOLSETS`).
2. Decorate an async function with `@reg.tool(name, title=..., toolset=..., kind=...)`. Use `kind="read"` for reads,
   `"calc"` for computing POSTs, `"write"` for anything that changes the account, and `"local"` for server-local
   state.
3. Type every argument as `Annotated[..., Field(description=...)]`. The description is what the model reads.
4. Write tools take `dry_run: bool = True` and `confirmation_token: str | None = None`, and return
   `await rt.pipeline.run(WriteRequest(...), dry_run=..., confirmation_token=...)`.
5. Add tests with `FakeKite`, covering the request that is sent (method, path, body) and the errors.
   Write tools also need an entry in `tests/test_invariants.py`.
6. Regenerate the tool reference: `kite-connect-mcp tools --markdown > docs/TOOLS.md`.

## Commits and pull requests

- Small, focused commits with a message that says why, not just what.
- Describe how you tested the change. For anything that touches orders, say how you verified it against the
  [Kite Connect docs](https://kite.trade/docs/connect/v3/).
- CI must be green: lint, type check, tests on Python 3.11 to 3.13, and a secret scan.

By contributing you agree that your contributions are licensed under the MIT licence of this project.
