# willhaben Agent: FastAPI dashboard + headed Chromium (Xvfb/noVNC for the one-time login) + Claude Code CLI
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    DISPLAY=:99 \
    DATA_DIR=/data \
    PATH=/opt/venv/bin:/home/pwuser/.local/bin:$PATH \
    CLAUDE_BIN=/home/pwuser/.local/bin/claude \
    DISABLE_AUTOUPDATER=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends xvfb x11vnc novnc websockify supervisor curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /usr/local/bin/uv

# Claude Code CLI, version pinned on purpose: `claude -p` must keep reading CLAUDE_CODE_OAUTH_TOKEN
# (the docs announce --bare, which ignores it, as a future default). Check before bumping.
ARG CLAUDE_CODE_VERSION=2.1.286
USER pwuser
RUN curl -fsSL https://claude.ai/install.sh | bash -s ${CLAUDE_CODE_VERSION} && claude --version
USER root

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev --no-hashes -o /tmp/requirements.txt \
 && uv venv /opt/venv \
 && uv pip install --python /opt/venv/bin/python -r /tmp/requirements.txt

COPY app ./app
COPY docker/supervisord.conf /etc/supervisor/conf.d/willhaben.conf
COPY docker/entrypoint.sh docker/start-vnc.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/start-vnc.sh

EXPOSE 8000 6080
VOLUME ["/data"]
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s CMD curl -fs http://127.0.0.1:8000/healthz || exit 1
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["/usr/bin/supervisord", "-n", "-c", "/etc/supervisor/supervisord.conf"]
