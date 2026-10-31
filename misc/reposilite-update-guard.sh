#!/bin/sh
# Copyright (c) 2026
# Author: hwajin-me
# License: MIT

# A failed or interrupted update must not boot an older JAR against migrated
# state. Only the live, exclusively locked updater may start a pending update.
marker=/etc/reposilite/update-in-progress
authorization=/run/reposilite-update.authorized
lock=/run/reposilite-update.lock

[ -e "$marker" ] || [ -L "$marker" ] || exit 0
case "${1:-start}" in
  stop | status | describe | depend | zap | help) exit 0 ;;
esac

if [ -f "$authorization" ] && [ ! -L "$authorization" ] &&
  [ "$(stat -c '%u:%a' "$authorization" 2>/dev/null)" = 0:600 ]; then
  updater_pid=
  IFS= read -r updater_pid <"$authorization"
  case "$updater_pid" in
    '' | 0* | *[!0-9]*) ;;
    *)
      if kill -0 "$updater_pid" 2>/dev/null; then
        # Conflict is specifically exit 1. A missing/broken flock is not proof
        # that the updater still owns its lock.
        if flock -n "$lock" /bin/true >/dev/null 2>&1; then
          :
        else
          [ "$?" -ne 1 ] || exit 0
        fi
      fi
      ;;
  esac
fi

printf '%s\n' 'Reposilite update recovery is required; inspect the saved state before starting.' >&2
exit 1
