#!/usr/bin/env bash
# Daily SQLite backup for a Likelyread server, keeping the last 30 days.
# Safe while the server runs (uses SQLite's online backup API inside the container).
#
#   sudo bash deploy/backup.sh [--dir /opt/likelyread] [--keep 30]
#
# Cron, every night at 03:17:
#   17 3 * * * root bash /opt/likelyread/deploy/backup.sh --dir /opt/likelyread >> /var/log/likelyread-backup.log 2>&1
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KEEP=30
while [ $# -gt 0 ]; do
  case "$1" in
    --dir) DIR="$2"; shift 2 ;;
    --keep) KEEP="$2"; shift 2 ;;
    -h|--help) sed -n 2,9p "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

cd "$DIR/deploy"
mkdir -p data/backups
stamp="$(date -u +%Y%m%d-%H%M%S)"
docker compose exec -T server python - "$stamp" <<'PY'
import sqlite3, sys
src = sqlite3.connect("/data/likelyread.db")
dst = sqlite3.connect(f"/data/backups/likelyread-{sys.argv[1]}.db")
with dst:
    src.backup(dst)
dst.close()
src.close()
PY
gzip -f "data/backups/likelyread-$stamp.db"
find data/backups -name 'likelyread-*.db.gz' -mtime +"$KEEP" -delete
echo "$(date -u +%FT%TZ) backup ok: data/backups/likelyread-$stamp.db.gz ($(du -h "data/backups/likelyread-$stamp.db.gz" | cut -f1))"
