#!/bin/sh
set -e

# Apply migrations on every start so deploys are just "pull + up".
python manage.py migrate --noinput

exec "$@"
