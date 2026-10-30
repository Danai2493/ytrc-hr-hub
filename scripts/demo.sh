#!/usr/bin/env bash
# Start the SAMPLE-data demo on port 8000. Loads the demo data the first time;
# pass --fresh to start again from clean demo data.
set -e
cd "$(dirname "$0")/.."
export DATABASE_URL=sqlite:///./data/demo.db DEMO_MODE=true AI_PROVIDER=mock STORAGE_DIR=./data/files
mkdir -p data
if [ "$1" = "--fresh" ]; then rm -f data/demo.db; fi
if [ ! -f data/demo.db ]; then
  echo "Loading SAMPLE demo data (about a minute)..."
  python -m app.cli migrate
  python -m app.cli seed-demo
fi
echo
echo "YTRC HR Hub demo is running. Password for every demo login: Demo-2026-hr"
echo "Logins: hr.manager, hr.officer, gm, prod.head, sup.dry, qa.head, it.admin (more in the README)"
echo
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips "*"
