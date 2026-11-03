# Security policy

## Reporting a vulnerability

Please **do not open a public issue** for a security problem. Use GitHub's private vulnerability reporting instead:
the repository's **Security** tab, then **Report a vulnerability**. Include the steps to reproduce, the impact, and
the affected version or commit. You should get a reply within a week.

Examples of problems we treat as vulnerabilities:

- any way to send a write (order, GTT, alert, conversion, MF order) without the safety pipeline's approval, or to
  get around read-only mode, the caps, the allow-lists, the kill switch or the confirmation token
- credentials (API key or secret, access token, request token, bearer token) reaching logs, tool output, error
  messages or files other than the token store
- bypassing the HTTP bearer authentication, or abusing the login callback or postback endpoint
- this server ending or replacing a session it does not own (`external` auth mode)

## Operating it safely

- Keep `KITE_MCP_TRADING_MODE=dry_run` unless you mean to trade. Keep the caps low.
- Expose the HTTP transport only behind TLS (a reverse proxy) and a long random bearer token. Bind to 127.0.0.1.
- Store secrets in files with mode 600 (`KITE_API_SECRET_FILE`, `KITE_MCP_BEARER_TOKEN_FILE`), not in shell history.
- Rotate the API secret in the Kite developer console if you think it leaked. Rotating invalidates every token.
