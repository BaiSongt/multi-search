#!/bin/sh
set -eu
SEARXNG_SECRET="$(cat /run/secrets/searxng_secret)"
export SEARXNG_SECRET
exec /usr/local/searxng/entrypoint.sh
