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
# Env:     SC_NAV_DATA   data dir holding sc_nav.db (default: ../../poi)
#          SC_NAV_BACKUP_DIR   where to write copies (default: $SC_NAV_DATA/backups)
#          SC_NAV_BACKUP_KEEP  how many to retain (default: 14)
#
# --- Docker deployment (the live nav.bytecollective.io setup) ---------------
# The server runs in a container and the DB lives in a named volume, NOT in the
# repo's poi/ dir (those poi.json/containers.json are just the offline cache).
# The host path is the SAME file the container has open, so sqlite3 ".backup"
# from the host is still safe (WAL allows cross-process access). Point the
# script at the volume and write copies OUTSIDE it. Must run as root (the volume
# is root-owned). Find the volume with: docker volume ls | grep sc-nav
#
#   sudo SC_NAV_DATA=/var/lib/docker/volumes/<project>_sc-nav-data/_data \
#        SC_NAV_BACKUP_DIR=/var/backups/sc-nav \
#        server/deploy/backup_db.sh
#
# Nightly via root cron (`sudo crontab -e`):
#   0 4 * * * SC_NAV_DATA=/var/lib/docker/volumes/<project>_sc-nav-data/_data \
#     SC_NAV_BACKUP_DIR=/var/backups/sc-nav \
#     /path/to/server/deploy/backup_db.sh >> /var/log/sc-nav-backup.log 2>&1
#
# NOTE: the volume name embeds the compose project (usually the repo dir name),
# so renaming the project changes the path — the "no database" check below makes
# that loud in the log rather than silently backing up nothing.

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
