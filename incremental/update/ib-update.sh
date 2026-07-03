#!/usr/bin/env bash
# ib-update.sh — the 2-minute inner loop (R4).
#
# Pushes Python code + package data + configs from this repo to a BOOTED
# Icebreaker system (UTM VM, QEMU guest, GCP VM) over ssh, restarts the
# services, and prints an ib-debug health snapshot.
#
# Usage:
#   bash incremental/update/ib-update.sh <host> [--port N] [--dry-run]
#
#   <host>     IP or hostname of the booted system (user: icebreaker)
#   --port N   ssh port (default 22; use 2299 for the qemu-gate guest)
#   --dry-run  show what would be copied, touch nothing
#
# Requires: ssh access as icebreaker (password 'icebreaker' — install
# sshpass, or set up a key with ssh-copy-id first).
#
# Only components present on the target are updated (V2 targets get
# controller only; V3+ gets terminal; V7+ gets gui). Detection is by
# what exists in the target venv.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
DB="${REPO_ROOT}/dual-brain"

HOST="${1:?usage: ib-update.sh <host> [--port N] [--dry-run]}"; shift
PORT=22
DRY=0
while [ $# -gt 0 ]; do
    case "$1" in
        --port) PORT="$2"; shift 2 ;;
        --dry-run) DRY=1; shift ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

USER=icebreaker
PASS=icebreaker
SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -p "$PORT")

if command -v sshpass >/dev/null; then
    SSH=(sshpass -p "$PASS" ssh "${SSH_OPTS[@]}" "${USER}@${HOST}")
    RSYNC_RSH="sshpass -p ${PASS} ssh ${SSH_OPTS[*]}"
else
    SSH=(ssh "${SSH_OPTS[@]}" "${USER}@${HOST}")
    RSYNC_RSH="ssh ${SSH_OPTS[*]}"
fi

info() { echo -e "\033[0;32m[ib-update]\033[0m $*"; }
die()  { echo -e "\033[0;31mFATAL:\033[0m $*" >&2; exit 1; }

# ── Discover target ─────────────────────────────────────────────────────
info "Probing ${HOST}:${PORT}..."
TARGET_V="$("${SSH[@]}" "cat /etc/icebreaker-version 2>/dev/null" || true)"
[ -n "$TARGET_V" ] || die "cannot reach target or /etc/icebreaker-version missing — is this a booted Icebreaker system?"
SP="$("${SSH[@]}" "/opt/icebreaker/venv/bin/python3 -c 'import controller,os;print(os.path.dirname(os.path.dirname(controller.__file__)))' 2>/dev/null" || true)"
info "Target: ${TARGET_V}, site-packages: ${SP:-<no venv — target is pre-V2, nothing to update>}"

if [ "$DRY" = "1" ]; then
    info "--dry-run: would sync these packages into ${SP:-<n/a>}:"
    for pkg in controller terminal gui gui_agent rpa_bridge; do
        [ -d "${DB}/${pkg}" ] && echo "  ${DB}/${pkg}/ → ${SP:-?}/${pkg}/"
    done
    exit 0
fi
[ -n "$SP" ] || die "no venv on target — ib-update applies to V2+ systems"

# ── Sync packages that exist on the target ──────────────────────────────
SYNCED=""
for pkg in controller terminal gui gui_agent rpa_bridge; do
    [ -d "${DB}/${pkg}" ] || continue
    if "${SSH[@]}" "test -d '${SP}/${pkg}'"; then
        info "Syncing ${pkg}/..."
        rsync -az --delete \
            --exclude='__pycache__' --exclude='*.pyc' --exclude='tests/' \
            -e "$RSYNC_RSH" \
            --rsync-path="sudo rsync" \
            "${DB}/${pkg}/" "${USER}@${HOST}:${SP}/${pkg}/"
        SYNCED="${SYNCED} ${pkg}"
    fi
done
[ -n "$SYNCED" ] || die "no matching packages found on target"

# ── Re-copy package data (PKG-1 — pip doesn't ship it, rsync --delete may drop it) ──
info "Restoring package data (PKG-1)..."
for d in schemas prompts grammars; do
    [ -d "${DB}/controller/${d}" ] && rsync -az -e "$RSYNC_RSH" --rsync-path="sudo rsync" \
        "${DB}/controller/${d}/" "${USER}@${HOST}:${SP}/controller/${d}/" || true
done
[ -f "${DB}/controller/catalogue.toml" ] && rsync -az -e "$RSYNC_RSH" --rsync-path="sudo rsync" \
    "${DB}/controller/catalogue.toml" "${USER}@${HOST}:${SP}/controller/catalogue.toml"

# ── ib-debug too ────────────────────────────────────────────────────────
rsync -az -e "$RSYNC_RSH" --rsync-path="sudo rsync" \
    "${DB}/scripts/ib_debug.py" "${USER}@${HOST}:/usr/local/bin/ib-debug" 2>/dev/null || true
"${SSH[@]}" "sudo chmod +x /usr/local/bin/ib-debug" || true

# ── Restart services ────────────────────────────────────────────────────
info "Restarting services..."
"${SSH[@]}" "sudo systemctl restart icebreaker-controller 2>/dev/null; sudo systemctl restart icebreaker-pbd 2>/dev/null; true"
sleep 3

# ── Health check ────────────────────────────────────────────────────────
info "Health snapshot:"
"${SSH[@]}" "ib-debug snapshot --no-color" || \
    die "ib-debug reported problems — target updated but unhealthy (synced:${SYNCED})"

info "Updated:${SYNCED} → ${HOST} (${TARGET_V})"
