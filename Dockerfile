# kite-connect-mcp: a self-hostable MCP server for the Zerodha Kite Connect v3 API.
#
#   docker build -t kite-connect-mcp:local .
#   docker run --rm -e KITE_API_KEY=your_api_key -e KITE_MCP_BEARER_TOKEN="$(openssl rand -hex 32)" \
#       kite-connect-mcp:local check-config
#
# Two stages: the first builds the wheel, the second installs only that wheel into a clean image and runs it as an
# unprivileged user. See docs/SELF_HOSTING.md for volumes, secrets and the reverse proxy.

FROM python:3.12-slim AS build
WORKDIR /src
# hatchling reads the version from src/, the readme from README.md and the licence file from LICENSE
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip wheel --no-cache-dir --no-deps --wheel-dir /wheels .

FROM python:3.12-slim AS runtime
LABEL org.opencontainers.image.title="kite-connect-mcp" \
      org.opencontainers.image.description="Self-hostable MCP server for the Zerodha Kite Connect v3 API" \
      org.opencontainers.image.source="https://github.com/techfreakworm/kite-connect-mcp" \
      org.opencontainers.image.licenses="MIT" \
      io.modelcontextprotocol.server.name="io.github.techfreakworm/kite-connect-mcp"

# uid/gid 10001, no login shell. The state directory is created here, owned by that user and private (0700), so a
# fresh named volume mounted over /var/lib/kite-connect-mcp inherits the right owner. A bind mount does not: chown it
# on the host.
RUN groupadd --gid 10001 kite \
 && useradd --uid 10001 --gid 10001 --home-dir /var/lib/kite-connect-mcp --no-create-home \
      --shell /usr/sbin/nologin kite \
 && install -d -o 10001 -g 10001 -m 0700 \
      /var/lib/kite-connect-mcp /var/lib/kite-connect-mcp/state /var/lib/kite-connect-mcp/cache

COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir --disable-pip-version-check --root-user-action=ignore /wheels/*.whl \
 && rm -rf /wheels

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    KITE_MCP_TRANSPORT=http \
    KITE_MCP_HOST=0.0.0.0 \
    KITE_MCP_STATE_DIR=/var/lib/kite-connect-mcp/state \
    KITE_MCP_CACHE_DIR=/var/lib/kite-connect-mcp/cache

# Everything the server writes (token, audit log, kill switch, instrument cache) lives under this volume.
VOLUME /var/lib/kite-connect-mcp
WORKDIR /var/lib/kite-connect-mcp
USER 10001:10001

# KITE_MCP_HOST=0.0.0.0 makes the server refuse to start without KITE_MCP_BEARER_TOKEN (or _FILE): that is intended.
# Publish the port on loopback only (-p 127.0.0.1:8765:8765) and put a TLS reverse proxy in front.
EXPOSE 8765

# Ask /health on the loopback address. The empty ProxyHandler keeps a HTTPS_PROXY set for Kite traffic from catching it.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import os, urllib.request as u; u.build_opener(u.ProxyHandler({})).open('http://127.0.0.1:%s/health' % os.environ.get('KITE_MCP_PORT', '8765'), timeout=3)"]

ENTRYPOINT ["kite-connect-mcp"]
CMD ["serve"]
