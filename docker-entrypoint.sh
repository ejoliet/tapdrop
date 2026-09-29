#!/bin/sh
# Cloud Run and Hugging Face Spaces tell a container which port to listen on
# through $PORT, which tapdrop itself does not read: every tapdrop setting comes
# from TAPDROP_*. Translating it here keeps that rule intact and keeps the image
# deployable on both hosts without a --port flag at deploy time.
set -e

if [ -n "$PORT" ]; then
    export TAPDROP_PORT="$PORT"
fi

exec tapdrop "$@"
