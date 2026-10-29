#!/bin/sh
set -e

# Apply migrations on start so deploys are just "pull + up". Only the app container does this
# (RUN_MIGRATIONS=0 for the worker), so two containers never migrate at once.
if [ "${RUN_MIGRATIONS:-1}" = "1" ]; then
    python manage.py migrate --noinput
fi

exec "$@"
