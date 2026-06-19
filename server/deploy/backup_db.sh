#!/usr/bin/env bash
# Online backup of the SC Nav SQLite database (user contributions are
# irreplaceable — this is the Phase 4 "EBS snapshot" item, adapted for the
# home-server / Cloudflare-Tunnel deployment that doesn't run on EC2).
#
# Uses sqlite3 ".backup", which is safe against a live server (WAL mode): it
# takes a consistent copy without stopping uvicorn. Keeps the most recent
# $KEEP backups and prunes older ones.
#
# Usage:   backup_db.sh
# Cron:    0 4 * * *  /opt/sc-nav/server/deploy/backup_db.sh   # nightly 04:00
# Env:     SC_NAV_DATA   data dir holding sc_nav.db (default: ../../poi)
#          SC_NAV_BACKUP_DIR   where to write copies (default: $SC_NAV_DATA/backups)
#          SC_NAV_BACKUP_KEEP  how many to retain (default: 14)

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
data_dir="${SC_NAV_DATA:-$here/../../poi}"
db="$data_dir/sc_nav.db"
backup_dir="${SC_NAV_BACKUP_DIR:-$data_dir/backups}"
keep="${SC_NAV_BACKUP_KEEP:-14}"

if [[ ! -f "$db" ]]; then
    echo "[backup] no database at $db" >&2
    exit 1
fi

mkdir -p "$backup_dir"
stamp="$(date +%Y%m%d-%H%M%S)"
dest="$backup_dir/sc_nav-$stamp.db"

sqlite3 "$db" ".backup '$dest'"
gzip -f "$dest"
echo "[backup] wrote $dest.gz"

# Prune all but the newest $keep archives.
ls -1t "$backup_dir"/sc_nav-*.db.gz 2>/dev/null | tail -n "+$((keep + 1))" | while read -r old; do
    rm -f -- "$old"
    echo "[backup] pruned $old"
done
