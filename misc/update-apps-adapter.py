#!/usr/bin/env python3
"""Adapt two source lookups in a verified official updater, without replacing it."""
import hashlib
import os
import shutil
import subprocess
import sys
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen


UPSTREAM_COMMIT = "9b65262e4241e9c797e8113169c62a8ff8a659c3"
UPSTREAM_SHA256 = "f619846d1b0c4faeb7129294973fe3bc411ac983b68a610b585a056a782c1e4a"
UPSTREAM_URL = f"https://raw.githubusercontent.com/community-scripts/ProxmoxVE/{UPSTREAM_COMMIT}/tools/pve/update-apps.sh"
OFFICIAL_ROOT = "https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main"
DEFAULT_ROOT = "https://raw.githubusercontent.com/hwajin-me/proxmox-reposilite/main"
ANCHOR = '\n_CS_CORE_URL="${COMMUNITY_SCRIPTS_CORE_URL:-https://raw.githubusercontent.com/community-scripts/core/main}"\n'
ADAPTER_FUNCTION = r'''
# Standalone Reposilite adapter: only these two application slugs use another root.
reposilite_adapter_script_url() {
  local slug="$1" root
  case "$slug" in
    reposilite) root="${REPOSILITE_SCRIPTS_URL:-https://raw.githubusercontent.com/hwajin-me/proxmox-reposilite/main}" ;;
    alpine-reposilite)
      root="${REPOSILITE_SCRIPTS_URL:-https://raw.githubusercontent.com/hwajin-me/proxmox-reposilite/main}"
      slug=reposilite
      ;;
    *) root="https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main" ;;
  esac
  printf '%s/ct/%s.sh' "${root%/}" "$slug"
}
'''
REPLACEMENTS = {
    '"' + OFFICIAL_ROOT + '/ct/${name}.sh"': '"$(reposilite_adapter_script_url "$name")"',
    '"' + OFFICIAL_ROOT + '/ct/${service}.sh"': '"$(reposilite_adapter_script_url "$service")"',
}


class AdapterError(ValueError):
    pass


def validate_root(value):
    try:
        parsed = urlsplit(value)
        decoded = unquote(value)
        valid = (
            parsed.scheme == "https" and parsed.hostname and parsed.path.strip("/")
            and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
            and (parsed.port is None or 1 <= parsed.port <= 65535)
            and not any(ord(c) <= 32 or c in "\"'`$\\" for c in decoded)
            and not any(part in (".", "..") for part in unquote(parsed.path).split("/"))
        )
    except ValueError:
        valid = False
    if not valid:
        raise AdapterError("Invalid HTTPS Reposilite scripts root")
    return value.rstrip("/")


def load_upstream():
    request = Request(UPSTREAM_URL, headers={"User-Agent": "Reposilite-standalone-update-adapter"})
    with urlopen(request, timeout=30) as response:
        return response.read(1024 * 1024 + 1)


def render_source(payload):
    if hashlib.sha256(payload).hexdigest() != UPSTREAM_SHA256:
        raise AdapterError("Official update-apps SHA256 mismatch; refusing changed upstream source")
    try:
        source = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise AdapterError("Official update-apps source is not UTF-8") from error
    if source.count(OFFICIAL_ROOT + "/ct/") != 2 or source.count(ANCHOR) != 1:
        raise AdapterError("Official update-apps source context changed")
    if "reposilite_adapter_script_url" in source:
        raise AdapterError("Official update-apps source already contains the adapter function")
    for old, new in REPLACEMENTS.items():
        if source.count(old) != 1:
            raise AdapterError("Official update-apps URL context changed")
        source = source.replace(old, new, 1)
    source = source.replace(ANCHOR, ADAPTER_FUNCTION + ANCHOR, 1)
    result = subprocess.run(["bash", "-n"], input=source, text=True, capture_output=True)
    if result.returncode:
        raise AdapterError("Adapted update-apps script failed Bash syntax validation")
    return source


def verify_host():
    if os.geteuid() != 0 or not shutil.which("pveversion") or not shutil.which("pct"):
        raise AdapterError("Run this standalone adapter as root on a Proxmox VE host")


def main(arguments=None):
    arguments = sys.argv[1:] if arguments is None else arguments
    if arguments[:1] == ["--render"] and len(arguments) != 1:
        raise AdapterError("--render takes no additional arguments")
    if arguments != ["--render"]:
        verify_host()
    root = validate_root(os.environ.get("REPOSILITE_SCRIPTS_URL", DEFAULT_ROOT))
    source = render_source(load_upstream())
    print(f"Standalone Reposilite adapter for verified official update-apps ({UPSTREAM_COMMIT}).", file=sys.stderr)
    if arguments == ["--render"]:
        sys.stdout.write(source)
        return 0
    environment = dict(os.environ, REPOSILITE_SCRIPTS_URL=root)
    os.execvpe("bash", ["bash", "-c", source, "update-apps-standalone", *arguments], environment)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (AdapterError, OSError) as error:
        print(f"Standalone update-apps adapter: {error}", file=sys.stderr)
        sys.exit(1)
