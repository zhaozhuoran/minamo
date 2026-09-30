#!/bin/sh
# Minamo startup entry point.
#
# Loads environment variables from a local .env file (if present) and launches
# the Minamo server. If .env is missing, a warning is printed and the built-in
# defaults are used.
#
# Usage:
#   ./run.sh                 # load .env (or warn) and start the server
#   ./run.sh --debug         # start the server with debug logging
#   ./run.sh --port 9000     # extra args are forwarded to `python -m minamo`

set -e

if [ -f .env ]; then
    echo "Loading environment from .env"
    set -a
    . ./.env
    set +a
else
    echo "WARNING: .env file not found in $(pwd). Using built-in defaults." >&2
fi

exec python -m minamo "$@"
