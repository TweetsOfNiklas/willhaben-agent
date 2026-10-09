#!/bin/sh
# Full test suite + lint inside the app image (the checked-in .venv is a Windows venv).
# Usage: scripts/test.sh [pytest args]   – run from the repo root or any worktree.
set -e
cd "$(dirname "$0")/.."
# the app image: compose tags it ghcr.io/lumascet/willhaben-agent:latest (older builds: willhaben-agent-willhaben-agent)
IMAGE=${WH_TEST_IMAGE:-ghcr.io/lumascet/willhaben-agent:latest}
docker image inspect "$IMAGE" >/dev/null 2>&1 || IMAGE=willhaben-agent-willhaben-agent
exec docker run --rm --entrypoint sh -v "$PWD":/src -w /src \
  -e UV_PROJECT_ENVIRONMENT=/tmp/venv -e UV_CACHE_DIR=/tmp/uvc \
  "$IMAGE" -c "uv sync --frozen -q && uv run --no-sync pytest -q -p no:cacheprovider $* && uv tool run -q ruff check app tests --select F,E9,B --ignore B008 --no-cache"
