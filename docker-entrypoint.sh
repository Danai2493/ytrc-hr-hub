#!/bin/sh
# Bring the database up to date before the app starts. Safe to run every time.
set -e
python -m app.cli migrate
python -m app.cli seed-reference
if [ "$DEMO_MODE" = "true" ]; then
  python -m app.cli seed-demo || true
fi
exec "$@"
