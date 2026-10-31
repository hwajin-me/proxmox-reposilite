#!/usr/bin/env bash
# Copyright (c) 2026
# Author: hwajin-me
# License: MIT

set -euo pipefail

if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
  cat <<'EOF'
Standalone Reposilite adapter for Community Scripts update-apps.

Usage: update-apps.sh [--help | --render | --export-config]

  --help           Show this help without accessing Proxmox or the network.
  --render         Print the verified, adapted upstream script; do not execute it.
  --export-config  Pass through to the official script (requires a PVE root shell).

Normal execution keeps the official options, prompts and environment variables:
  var_container, var_backup, var_backup_storage, var_unattended,
  var_skip_confirm, var_auto_reboot, var_continue_on_error, var_dry_run, var_tags.

REPOSILITE_SCRIPTS_URL selects the HTTPS script root for reposilite and
alpine-reposilite only. Other applications retain the official source root.
The default is https://raw.githubusercontent.com/hwajin-me/proxmox-reposilite/main.
COMMUNITY_SCRIPTS_ROOT can select a local checkout containing this adapter's
Python helper; COMMUNITY_SCRIPTS_URL selects its HTTPS download root otherwise.
No schedule is created. The original upstream source is pinned and SHA256 checked.
EOF
  exit 0
fi

if [[ "${1:-}" == --render ]]; then
  [[ $# == 1 ]] || {
    echo "--render takes no additional arguments" >&2
    exit 2
  }
else
  [[ "$EUID" -eq 0 ]] || {
    echo "Run this standalone adapter as root on Proxmox VE" >&2
    exit 1
  }
  if ! command -v pveversion >/dev/null || ! command -v pct >/dev/null; then
    echo "This standalone adapter must run on a Proxmox VE host" >&2
    exit 1
  fi
fi

command -v python3 >/dev/null || {
  echo "python3 is required for the standalone adapter" >&2
  exit 1
}
_adapter_root="${COMMUNITY_SCRIPTS_ROOT:-}"
if [[ -z "$_adapter_root" && -f "${BASH_SOURCE[0]:-}" ]]; then
  _adapter_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
fi
_adapter_helper=""
[[ -z "$_adapter_root" ]] || _adapter_helper="${_adapter_root}/misc/update-apps-adapter.py"
_adapter_temp=""
trap '[[ -z "$_adapter_temp" ]] || rm -f -- "$_adapter_temp"' EXIT
if [[ -z "$_adapter_helper" || ! -f "$_adapter_helper" ]]; then
  _adapter_url="${COMMUNITY_SCRIPTS_URL:-${REPOSILITE_SCRIPTS_URL:-https://raw.githubusercontent.com/hwajin-me/proxmox-reposilite/main}}"
  python3 - "$_adapter_url" <<'PY'
import sys
from urllib.parse import unquote, urlsplit
try:
    value = sys.argv[1]
    parsed = urlsplit(value)
    decoded = unquote(value)
    assert parsed.scheme == "https" and parsed.hostname and parsed.path.strip("/")
    assert not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
    assert parsed.port is None or 1 <= parsed.port <= 65535
    assert not any(ord(c) <= 32 or c in "\"'`$\\" for c in decoded)
    assert not any(part in (".", "..") for part in unquote(parsed.path).split("/"))
except (AssertionError, ValueError):
    print("Invalid HTTPS adapter source root", file=sys.stderr)
    sys.exit(2)
PY
  _adapter_temp="$(mktemp)"
  _adapter_helper="$_adapter_temp"
  curl --proto '=https' --proto-redir '=https' -fsSL --retry 3 --connect-timeout 10 --max-time 60 \
    "${_adapter_url%/}/misc/update-apps-adapter.py" -o "$_adapter_helper"
fi
python3 "$_adapter_helper" "$@"
