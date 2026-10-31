#!/usr/bin/env bash
_CS_DEFAULT_URL="https://raw.githubusercontent.com/hwajin-me/proxmox-reposilite/main"
_cs_boot="${COMMUNITY_SCRIPTS_CORE_DIR:-$(dirname "${BASH_SOURCE[0]}")/../../core}/core/build.func"
source "$_cs_boot" 2>/dev/null || source <(curl -fsSL "${COMMUNITY_SCRIPTS_CORE_URL:-https://raw.githubusercontent.com/community-scripts/core/main}/core/build.func")

# Copyright (c) 2026
# Author: hwajin-me
# License: MIT
# Source: https://reposilite.com/

APP="reposilite"
var_tags="${var_tags:-alpine;maven;repository}"
var_cpu="${var_cpu:-1}"
var_ram="${var_ram:-1024}"
var_disk="${var_disk:-8}"
var_os="${var_os:-alpine}"
var_version="${var_version:-3.24}"
var_unprivileged="${var_unprivileged:-1}"
var_nesting="${var_nesting:-0}"
# ARM64 has not been tested; Core asks before attempting it.
#var_arm64="${var_arm64:-no}"

header_info "$APP"
variables
color
catch_errors

function update_script() {
  header_info
  check_container_storage
  check_container_resources
  [[ -f /opt/reposilite/reposilite.jar && -f /etc/init.d/reposilite ]] || {
    msg_error "No Reposilite installation found"
    exit 1
  }
  command -v flock >/dev/null || $STD apk add --no-cache flock
  exec 9>/run/reposilite-update.lock
  flock -n 9 || { msg_error "Another Reposilite update is running"; exit 1; }
  source /etc/conf.d/reposilite
  if check_for_gh_release "reposilite" "dzikoysk/reposilite"; then
    RELEASE="${CHECK_UPDATE_RELEASE#v}"
    [[ "$RELEASE" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { msg_error "Invalid Reposilite release"; exit 1; }
    URL="https://maven.reposilite.com/releases/com/reposilite/reposilite/${RELEASE}/reposilite-${RELEASE}-all.jar"
    if ! curl_with_retry "$URL" /opt/reposilite/reposilite.jar.new ||
      ! CHECKSUM="$(curl_with_retry "${URL}.sha256")"; then
      rm -f /opt/reposilite/reposilite.jar.new
      msg_error "Reposilite download failed; the running service is unchanged"
      exit 1
    fi
    [[ "$CHECKSUM" =~ ^[[:xdigit:]]{64}$ ]] || {
      rm -f /opt/reposilite/reposilite.jar.new
      msg_error "Invalid Reposilite checksum"
      exit 1
    }
    msg_info "Verifying Reposilite download"
    if ! $STD sha256sum -c - <<<"$CHECKSUM  /opt/reposilite/reposilite.jar.new"; then
      rm -f /opt/reposilite/reposilite.jar.new
      msg_error "Reposilite checksum mismatch; the running service is unchanged"
      exit 1
    fi
    msg_ok "Verified Reposilite download"

    msg_info "Stopping Reposilite for a consistent backup"
    if ! $STD rc-service reposilite stop; then
      rm -f /opt/reposilite/reposilite.jar.new
      msg_error "Could not stop Reposilite; no application files were replaced"
      exit 1
    fi
    msg_ok "Stopped Reposilite"
    BACKUP_ROOT="/opt/reposilite-backups/$(date +%Y%m%d-%H%M%S)-$$"
    if ! install -d -m 700 "$BACKUP_ROOT"; then
      rm -f /opt/reposilite/reposilite.jar.new
      $STD rc-service reposilite start || true
      msg_error "Could not create the backup directory; the previous JAR is unchanged"
      exit 1
    fi
    BACKUP_DIR="$BACKUP_ROOT/application"
    if ! (create_backup /opt/reposilite/reposilite.jar /root/.reposilite); then
      rm -f /opt/reposilite/reposilite.jar.new
      $STD rc-service reposilite start || true
      msg_error "Application backup failed; the previous JAR is unchanged"
      exit 1
    fi
    BACKUP_DIR="$BACKUP_ROOT/state"
    STATE_PATHS=(/etc/conf.d/reposilite)
    for STATE_FILE in configuration.cdn reposilite.db reposilite.db-wal reposilite.db-shm; do
      [[ ! -f "${data_directory:-/var/lib/reposilite}/$STATE_FILE" ]] || STATE_PATHS+=("${data_directory:-/var/lib/reposilite}/$STATE_FILE")
    done
    if ! (create_backup "${STATE_PATHS[@]}"); then
      rm -f /opt/reposilite/reposilite.jar.new
      $STD rc-service reposilite start || true
      msg_error "State backup failed; the previous JAR is unchanged"
      exit 1
    fi
    msg_info "Updating Reposilite"
    READY=no
    if mv /opt/reposilite/reposilite.jar.new /opt/reposilite/reposilite.jar && $STD rc-service reposilite start; then
      for ((i=0; i<60; i++)); do
        if curl -fsS --max-time 2 "http://127.0.0.1:${http_port:-8080}/" >/dev/null 2>&1; then READY=yes; break; fi
        sleep 2
      done
    fi
    if [[ "$READY" != yes ]]; then
      $STD rc-service reposilite stop || true
      rm -f /opt/reposilite/reposilite.jar.new
      BACKUP_DIR="$BACKUP_ROOT/application"
      restore_backup
      msg_error "Reposilite failed to start. Previous JAR restored, service stopped. State backup: $BACKUP_ROOT/state. Inspect logs and database compatibility before restarting."
      exit 1
    fi
    cat >/root/.reposilite <<EOF
${RELEASE}
EOF
    msg_ok "Updated Reposilite to ${RELEASE}; backup: ${BACKUP_ROOT}"
  fi
  exit 0
}

start
if [[ "${METHOD:-}" == advanced ]] && MODE=advanced prompt_confirm "Customize Reposilite application settings?" "n" 60; then
  _settings=(
    'var_port|HTTP port|8080'
    'var_data_dir|Data directory (under /var/lib or /srv)|/var/lib/reposilite'
    'var_java_heap|Maximum Java heap in MiB|256'
    'var_admin_user|Manager token name|admin'
    'var_public_repositories|Public repositories, comma separated (- for none)|releases,snapshots'
    'var_private_repositories|Private repositories, comma separated (- for none)|private'
    'var_repository_quota|Storage quota per repository, percent|100'
    'var_redeployment|Allow overwriting artifacts (yes/no)|no'
    'var_preserve_snapshots|Preserve timestamped snapshots (yes/no)|no'
    'var_dashboard_title|Dashboard title|Reposilite Repository'
    'var_dashboard_description|Dashboard description|Public Maven repository hosted through Reposilite'
  )
  for _setting in "${_settings[@]}"; do
    IFS='|' read -r _key _label _default <<<"$_setting"
    [[ -n "${!_key:-}" ]] || printf -v "$_key" '%s' "$(MODE=advanced prompt_input "$_label" "$_default" 60)"
  done
fi
export var_port="${var_port:-8080}"
export var_data_dir="${var_data_dir:-/var/lib/reposilite}"
export var_java_heap="${var_java_heap:-256}"
export var_admin_user="${var_admin_user:-admin}"
export var_public_repositories="${var_public_repositories:-releases,snapshots}"
export var_private_repositories="${var_private_repositories:-private}"
export var_repository_quota="${var_repository_quota:-100}"
export var_redeployment="${var_redeployment:-no}"
export var_preserve_snapshots="${var_preserve_snapshots:-no}"
export var_dashboard_title="${var_dashboard_title:-Reposilite Repository}"
export var_dashboard_description="${var_dashboard_description:-Public Maven repository hosted through Reposilite}"
export var_repositories_json="${var_repositories_json:-}"
[[ "$var_public_repositories" != *$'\n'* && "$var_private_repositories" != *$'\n'* ]] || { msg_error "Repository lists must be on one line"; exit 1; }
var_public_repositories="$(printf '%s' "$var_public_repositories" | sed -E 's/^[[:space:]]*//;s/[[:space:]]*$//;s/[[:space:]]*,[[:space:]]*/,/g')"
var_private_repositories="$(printf '%s' "$var_private_repositories" | sed -E 's/^[[:space:]]*//;s/[[:space:]]*$//;s/[[:space:]]*,[[:space:]]*/,/g')"

[[ "$var_port" =~ ^[1-9][0-9]{0,4}$ && "$var_port" -le 65535 ]] || { msg_error "HTTP port must be 1-65535"; exit 1; }
[[ "$var_java_heap" =~ ^[1-9][0-9]{1,4}$ && "$var_java_heap" -ge 64 && "$var_java_heap" -le 65536 ]] || { msg_error "Java heap must be 64-65536 MiB"; exit 1; }
_container_ram="${RAM_SIZE:-${var_ram:-1024}}"
[[ "$_container_ram" =~ ^[1-9][0-9]{0,8}$ ]] || { msg_error "Invalid container RAM size"; exit 1; }
[[ $((var_java_heap + 128)) -le $_container_ram ]] || { msg_error "Leave at least 128 MiB of container RAM above the Java heap"; exit 1; }
[[ "$var_data_dir" =~ ^/(var/lib|srv)/[a-zA-Z0-9][a-zA-Z0-9_/-]*$ && "$var_data_dir" != *'//'* ]] || { msg_error "Data directory must be below /var/lib or /srv, using letters, numbers, /, _ and -"; exit 1; }
[[ "$var_admin_user" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$ ]] || { msg_error "Invalid initial manager token name"; exit 1; }
[[ "$var_repository_quota" =~ ^[1-9][0-9]{0,2}$ && "$var_repository_quota" -le 100 ]] || { msg_error "Repository quota must be 1-100 percent"; exit 1; }
[[ "$var_redeployment" =~ ^(yes|no)$ && "$var_preserve_snapshots" =~ ^(yes|no)$ ]] || { msg_error "Repository switches must be yes or no"; exit 1; }
ensure_dependencies jq
if [[ -n "$var_repositories_json" ]]; then
  jq -e 'type == "array" and length > 0 and all(.[];
    type == "object" and (.id | type == "string" and test("^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")) and
    (.visibility == "PUBLIC" or .visibility == "PRIVATE") and .storageProvider.type == "fs" and
    (.storageProvider.quota | type == "string" and length > 0) and
    (if (.storageProvider | has("mount")) then (.storageProvider.mount | type == "string") else true end) and
    (if has("redeployment") then (.redeployment | type == "boolean") else true end) and
    (if has("preserveSnapshots") then (.preserveSnapshots | type == "boolean") else true end) and
    (if has("proxied") then (.proxied | type == "array") else true end)) and
    ((map(.id) | unique | length) == length)' <<<"$var_repositories_json" >/dev/null || { msg_error "Invalid repository JSON: check unique IDs, visibility, fs quota/mount, boolean switches and proxy arrays"; exit 1; }
else
  _repository_ids=()
  for _list in "$var_public_repositories" "$var_private_repositories"; do
    [[ "$_list" == - ]] && continue
    [[ "$_list" != *, && "$_list" != ,* && "$_list" != *,,* ]] || { msg_error "Empty repository name in list"; exit 1; }
    IFS=, read -ra _ids <<<"$_list"
    for _id in "${_ids[@]}"; do
      [[ "$_id" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$ ]] || { msg_error "Invalid repository name: $_id"; exit 1; }
      _repository_ids+=("$_id")
    done
  done
  [[ ${#_repository_ids[@]} -gt 0 ]] || { msg_error "Configure at least one repository"; exit 1; }
  [[ $(printf '%s\n' "${_repository_ids[@]}" | sort -u | wc -l) -eq ${#_repository_ids[@]} ]] || { msg_error "Repository names must be unique across public and private lists"; exit 1; }
fi
build_container
description
msg_ok "Reposilite installation completed"
echo -e "${INFO}${YW}URL: http://${IP}:${var_port}${CL}"
echo -e "${INFO}${YW}Manager token: ${var_admin_user}. Read admin_secret in /etc/conf.d/reposilite inside the container.${CL}"
