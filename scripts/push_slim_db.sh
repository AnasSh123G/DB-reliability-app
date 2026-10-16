#!/usr/bin/env bash
# Copy the deployed database to the home server and restart the app.
# Usage: scripts/push_slim_db.sh [path/to/slim.sqlite]   (asks for the server password once)
set -euo pipefail

DB="${1:-$HOME/dev/DB-reliability-app/data/deploy/slim.sqlite}"
HOST="${DBREL_HOST:-anas@192.168.0.213}"
DATA=/srv/bahnpredict/data
COMPOSE="${DBREL_COMPOSE:-/srv/bahnpredict/repo/deploy/compose.yaml}"

[ -f "$DB" ] || { echo "no such file: $DB" >&2; exit 1; }

# Fold the write-ahead log into the main file so one file is the whole database.
sqlite3 "$DB" 'PRAGMA wal_checkpoint(TRUNCATE);'
[ "$(sqlite3 "$DB" 'PRAGMA quick_check;')" = ok ] || { echo "quick_check failed" >&2; exit 1; }

# One ssh session: upload next to the target, verify, swap atomically, restart.
ssh "$HOST" "cat > $DATA/dbrel.sqlite.new \
  && [ \"\$(sqlite3 $DATA/dbrel.sqlite.new 'PRAGMA quick_check;')\" = ok ] \
  && mv $DATA/dbrel.sqlite.new $DATA/dbrel.sqlite \
  && rm -f $DATA/dbrel.sqlite-wal $DATA/dbrel.sqlite-shm \
  && if [ -f $COMPOSE ]; then docker compose -f $COMPOSE restart app; fi" < "$DB"
echo "done: $(du -h "$DB" | cut -f1) copied to $HOST:$DATA/dbrel.sqlite"
