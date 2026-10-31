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

set +x
header_info "$APP"
variables
color
catch_errors

function update_alpine() {
  # Profiles and service configuration contain credentials; never trace them.
  set +x
  umask 077
  [[ -f /opt/reposilite/reposilite.jar && -f /etc/init.d/reposilite ]] || {
    msg_error "No Reposilite installation found"
    exit 1
  }
  command -v flock >/dev/null || $STD apk add --no-cache flock
  exec 9>/run/reposilite-update.lock
  flock -n 9 || {
    msg_error "Another Reposilite update is running"
    exit 1
  }
  source /etc/conf.d/reposilite
  chmod 700 "${data_directory:-/var/lib/reposilite}"
  [[ ! -d /var/log/reposilite ]] || chmod 700 /var/log/reposilite
  for state_file in reposilite.db reposilite.db-wal reposilite.db-shm reposilite.db-journal; do
    [[ ! -f "${data_directory:-/var/lib/reposilite}/$state_file" ]] || chmod 600 "${data_directory:-/var/lib/reposilite}/$state_file"
  done
  HEALTH_URL="http://127.0.0.1:${http_port:-8080}/"
  HEALTH_ARGS=(--noproxy '*' --connect-timeout 5 --max-time 30)
  if [[ "${tls_enabled:-no}" == yes ]]; then
    HEALTH_URL="https://${tls_host}:${tls_port}/"
    HEALTH_ARGS+=(--cacert /etc/reposilite/tls/certificate.pem --resolve "${tls_host}:${tls_port}:127.0.0.1")
  fi
  # Read curl credentials from stdin, including rotated secrets with quotes.
  UPDATE_SECRET="${admin_secret:-${bootstrap_secret:-}}"
  [[ -n "$UPDATE_SECRET" && "$UPDATE_SECRET" != *$'\n'* && "$UPDATE_SECRET" != *$'\r'* ]] || {
    msg_error "Reposilite update needs current manager credentials in /etc/conf.d/reposilite"
    exit 1
  }
  UPDATE_AUTH="user = $(printf '%s:%s' "${admin_user:-admin}" "$UPDATE_SECRET" | jq -Rs .)"
  unset UPDATE_SECRET
  if ! EXPECTED_REPOSITORIES="$(curl -fsS "${HEALTH_ARGS[@]}" --config - \
    "${HEALTH_URL%/}/api/settings/domain/maven" <<<"$UPDATE_AUTH" | jq -ce '.repositories | map(.id) | sort')" || [[ -z "$EXPECTED_REPOSITORIES" ]]; then
    msg_error "Reposilite manager authentication/settings check failed; update saved credentials before retrying"
    exit 1
  fi
  if check_for_gh_release "reposilite" "dzikoysk/reposilite"; then
    RELEASE="${CHECK_UPDATE_RELEASE#v}"
    [[ "$RELEASE" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
      msg_error "Invalid Reposilite release"
      exit 1
    }
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
      msg_error "Reposilite: Could not create the backup directory; the previous JAR is unchanged"
      exit 1
    fi
    BACKUP_DIR="$BACKUP_ROOT/application"
    if ! (create_backup /opt/reposilite/reposilite.jar /root/.reposilite /etc/init.d/reposilite); then
      rm -f /opt/reposilite/reposilite.jar.new
      $STD rc-service reposilite start || true
      msg_error "Reposilite: Application backup failed; the previous JAR is unchanged"
      exit 1
    fi
    BACKUP_DIR="$BACKUP_ROOT/state"
    STATE_PATHS=(/etc/conf.d/reposilite)
    [[ ! -d /etc/reposilite ]] || STATE_PATHS+=(/etc/reposilite)
    for STATE_FILE in configuration.cdn reposilite.db reposilite.db-wal reposilite.db-shm reposilite.db-journal; do
      [[ ! -f "${data_directory:-/var/lib/reposilite}/$STATE_FILE" ]] || STATE_PATHS+=("${data_directory:-/var/lib/reposilite}/$STATE_FILE")
    done
    if ! (create_backup "${STATE_PATHS[@]}"); then
      rm -f /opt/reposilite/reposilite.jar.new
      $STD rc-service reposilite start || true
      msg_error "Reposilite: State backup failed; the previous JAR is unchanged"
      exit 1
    fi
    msg_info "Updating Reposilite"
    READY=no
    if ! grep -q '^umask="0077"$' /etc/init.d/reposilite; then
      printf '\numask="0077"\n' >>/etc/init.d/reposilite
    fi
    HEALTH_DEADLINE=$((SECONDS + 120))
    if mv /opt/reposilite/reposilite.jar.new /opt/reposilite/reposilite.jar && $STD rc-service reposilite start; then
      for ((i = 0; i < 60; i++)); do
        [[ "$SECONDS" -lt "$HEALTH_DEADLINE" ]] || break
        if curl -fsS "${HEALTH_ARGS[@]}" --max-time 2 "$HEALTH_URL" >/dev/null 2>&1 &&
          ACTIVE_REPOSITORIES="$(curl -fsS "${HEALTH_ARGS[@]}" --max-time 2 --config - \
            "${HEALTH_URL%/}/api/maven/details" <<<"$UPDATE_AUTH" | jq -ce 'select(.type == "DIRECTORY") | [.files[].name] | sort')" &&
          [[ "$ACTIVE_REPOSITORIES" == "$EXPECTED_REPOSITORIES" ]]; then
          READY=yes
          while IFS= read -r REPOSITORY; do
            REPOSITORY_PATH="$(printf '%s' "$REPOSITORY" | jq -sRr @uri)"
            if [[ "$SECONDS" -ge "$HEALTH_DEADLINE" ]] ||
              ! REPOSITORY_DETAILS="$(curl -fsS "${HEALTH_ARGS[@]}" --max-time 2 --config - \
                "${HEALTH_URL%/}/api/maven/details/${REPOSITORY_PATH}" <<<"$UPDATE_AUTH")" ||
              ! jq -e '.type == "DIRECTORY"' <<<"$REPOSITORY_DETAILS" >/dev/null; then
              READY=no
              break
            fi
          done < <(jq -r '.[]' <<<"$EXPECTED_REPOSITORIES")
          [[ "$READY" != yes ]] || break
        fi
        sleep 2
      done
    fi
    if [[ "$READY" != yes ]]; then
      $STD rc-service reposilite stop || true
      rm -f /opt/reposilite/reposilite.jar.new
      BACKUP_DIR="$BACKUP_ROOT/application"
      restore_backup
      msg_error "Reposilite application/storage readiness failed. Previous JAR restored, service stopped. State backup: $BACKUP_ROOT/state. Inspect logs and database compatibility before restarting."
      exit 1
    fi
    cat >/root/.reposilite <<EOF
${RELEASE}
EOF
    msg_ok "Updated Reposilite to ${RELEASE}; backup: ${BACKUP_ROOT}"
  fi
  exit 0
}

function update_script() {
  header_info
  check_container_storage
  check_container_resources
  run_os_update
  exit $?
}

start
set +x
[[ "${var_os:-alpine}" == alpine ]] || {
  msg_error "Reposilite requires Alpine Linux"
  exit 1
}
_integrations_interactive=no
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
  if [[ -z "${var_integrations_file:-}" && (-z "${var_integrations_json:-}" || "$var_integrations_json" == '{}') ]] && MODE=advanced prompt_confirm "Configure database, external storage, LDAP and native TLS?" "n" 60; then
    _integrations_interactive=yes
  fi
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
export var_integrations_file="${var_integrations_file:-}"
export var_integrations_json="${var_integrations_json:-}"
[[ -n "$var_integrations_json" ]] || var_integrations_json='{}'
if [[ "$_integrations_interactive" == yes || -n "$var_integrations_file" || "$var_integrations_json" != '{}' || -n "$var_repositories_json" ]]; then
  ensure_dependencies python3
  _helper="${COMMUNITY_SCRIPTS_ROOT:-}/misc/reposilite-config.py"
  _helper_temp=""
  if [[ ! -f "$_helper" ]]; then
    _helper_temp="$(mktemp)"
    _helper="$_helper_temp"
    curl -fsSL "${COMMUNITY_SCRIPTS_URL:-$_CS_DEFAULT_URL}/misc/reposilite-config.py" -o "$_helper" || {
      rm -f "$_helper_temp"
      exit 1
    }
  fi
  _args=(validate)
  [[ "$_integrations_interactive" != yes ]] || _args=(interactive)
  [[ -z "${var_integrations_file:-}" ]] || _args+=(--file "$var_integrations_file")
  if ! var_integrations_json="$(python3 "$_helper" "${_args[@]}")"; then
    [[ -z "$_helper_temp" ]] || rm -f "$_helper_temp"
    exit 1
  fi
  [[ -z "$_helper_temp" ]] || rm -f "$_helper_temp"
fi
[[ "$var_public_repositories" != *$'\n'* && "$var_private_repositories" != *$'\n'* ]] || {
  msg_error "Reposilite: Repository lists must be on one line"
  exit 1
}
var_public_repositories="$(printf '%s' "$var_public_repositories" | sed -E 's/^[[:space:]]*//;s/[[:space:]]*$//;s/[[:space:]]*,[[:space:]]*/,/g')"
var_private_repositories="$(printf '%s' "$var_private_repositories" | sed -E 's/^[[:space:]]*//;s/[[:space:]]*$//;s/[[:space:]]*,[[:space:]]*/,/g')"

[[ "$var_port" =~ ^[1-9][0-9]{0,4}$ && "$var_port" -le 65535 ]] || {
  msg_error "Reposilite: HTTP port must be 1-65535"
  exit 1
}
[[ "$var_java_heap" =~ ^[1-9][0-9]{1,4}$ && "$var_java_heap" -ge 64 && "$var_java_heap" -le 65536 ]] || {
  msg_error "Reposilite: Java heap must be 64-65536 MiB"
  exit 1
}
_container_ram="${RAM_SIZE:-${var_ram:-1024}}"
[[ "$_container_ram" =~ ^[1-9][0-9]{0,8}$ ]] || {
  msg_error "Reposilite: Invalid container RAM size"
  exit 1
}
[[ $((var_java_heap + 128)) -le $_container_ram ]] || {
  msg_error "Reposilite: Leave at least 128 MiB of container RAM above the Java heap"
  exit 1
}
[[ "$var_data_dir" =~ ^/(var/lib|srv)/[a-zA-Z0-9][a-zA-Z0-9_/-]*$ && "$var_data_dir" != *'//'* ]] || {
  msg_error "Reposilite: Data directory must be below /var/lib or /srv, using letters, numbers, /, _ and -"
  exit 1
}
[[ "$var_admin_user" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$ ]] || {
  msg_error "Reposilite: Invalid initial manager token name"
  exit 1
}
[[ "$var_repository_quota" =~ ^[1-9][0-9]{0,2}$ && "$var_repository_quota" -le 100 ]] || {
  msg_error "Reposilite: Repository quota must be 1-100 percent"
  exit 1
}
[[ "$var_redeployment" =~ ^(yes|no)$ && "$var_preserve_snapshots" =~ ^(yes|no)$ ]] || {
  msg_error "Reposilite: Repository switches must be yes or no"
  exit 1
}
ensure_dependencies jq
if [[ "$(jq -r '.ldap.enabled // false' <<<"$var_integrations_json")" == true && "$var_admin_user" == admin ]]; then
  var_admin_user="$(python3 -c 'import secrets; print("local-manager-" + secrets.token_hex(6))')"
  msg_warn "Reposilite LDAP enabled; reserved local manager token: $var_admin_user"
fi
if [[ -z "$var_repositories_json" ]]; then
  _repository_ids=()
  for _list in "$var_public_repositories" "$var_private_repositories"; do
    [[ "$_list" == - ]] && continue
    [[ "$_list" != *, && "$_list" != ,* && "$_list" != *,,* ]] || {
      msg_error "Reposilite: Empty repository name in list"
      exit 1
    }
    IFS=, read -ra _ids <<<"$_list"
    for _id in "${_ids[@]}"; do
      [[ "$_id" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$ ]] || {
        msg_error "Reposilite: Invalid repository name: $_id"
        exit 1
      }
      _repository_ids+=("$_id")
    done
  done
  [[ ${#_repository_ids[@]} -gt 0 ]] || {
    msg_error "Reposilite: Configure at least one repository"
    exit 1
  }
  [[ $(printf '%s\n' "${_repository_ids[@]}" | sort -u | wc -l) -eq ${#_repository_ids[@]} ]] || {
    msg_error "Reposilite: Repository names must be unique across public and private lists"
    exit 1
  }
fi
build_container
description
msg_ok "Reposilite installation completed"
if [[ "$(jq -r '.tls.mode // "off"' <<<"$var_integrations_json")" != off ]]; then
  echo -e "${INFO}${YW}URL: $(jq -r '"https://" + .tls.hostname + ":" + ((.tls.port // 443)|tostring)' <<<"$var_integrations_json")${CL}"
else
  echo -e "${INFO}${YW}URL: http://${IP}:${var_port}${CL}"
fi
echo -e "${INFO}${YW}Manager token: ${var_admin_user}. Read admin_secret in /etc/conf.d/reposilite inside the container.${CL}"
