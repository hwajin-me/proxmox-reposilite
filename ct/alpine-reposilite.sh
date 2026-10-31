#!/usr/bin/env bash
# Compatibility entrypoint for containers installed before the slug was renamed.
set -euo pipefail
SCRIPT="$(curl -fsSL "${COMMUNITY_SCRIPTS_URL:-https://raw.githubusercontent.com/hwajin-me/proxmox-reposilite/main}/ct/reposilite.sh")"
exec bash -c "$SCRIPT"
