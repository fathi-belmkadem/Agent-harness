#!/bin/sh
# If AGENT_ACCESS_TOKEN_FILE points at a readable file (e.g. a mounted
# Docker/Compose secret), load its content into AGENT_ACCESS_TOKEN before
# starting the harness. Plain `-e AGENT_ACCESS_TOKEN=...` still works
# unchanged when no _FILE is set — config.py itself never changes, it
# still only ever reads AGENT_ACCESS_TOKEN from the environment.
set -eu

if [ -n "${AGENT_ACCESS_TOKEN_FILE:-}" ] && [ -f "$AGENT_ACCESS_TOKEN_FILE" ]; then
    AGENT_ACCESS_TOKEN="$(cat "$AGENT_ACCESS_TOKEN_FILE")"
    export AGENT_ACCESS_TOKEN
fi

# "$@" forwards whatever follows the service name in `docker compose run`,
# so --new-session, --prompt and --json work through Compose exactly as
# they do locally.
exec python -m harness.cli --config /app/config.yaml "$@"
