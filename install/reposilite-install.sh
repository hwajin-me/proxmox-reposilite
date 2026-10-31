#!/usr/bin/env bash
# Copyright (c) 2026
# Author: hwajin-me
# License: MIT
# Source: https://reposilite.com/guide/general

source /dev/stdin <<<"$FUNCTIONS_FILE_PATH"
color
catch_errors
setting_up_container
network_check
update_os

setup_alpine() {
  JAVA_VERSION="21" setup_java
  msg_info "Installing Reposilite dependencies"
  $STD apk add --no-cache tzdata flock python3 openssl util-linux-misc
  msg_ok "Installed Reposilite dependencies"

  # GitHub releases link to Maven-hosted JARs and have no GitHub binary assets.
  # fetch_and_deploy_from_url extracts ZIPs, which would unpack a runnable JAR.
  RELEASE="${APPLICATION_VERSION:-$(get_latest_github_release "dzikoysk/reposilite")}"
  [[ "$RELEASE" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { msg_error "Invalid Reposilite release"; exit 1; }
  if jq -e '.storage.type == "s3"' <<<"${var_integrations_json:-null}" >/dev/null ||
    jq -e 'any(.[]; .storageProvider.type == "s3")' <<<"${var_repositories_json:-[]}" >/dev/null; then
    IFS=. read -r _major _minor _patch <<<"$RELEASE"
    (( _major * 1000000 + _minor * 1000 + _patch >= 3006005 )) || { msg_error "S3 profiles require Reposilite 3.6.5 or newer"; exit 1; }
  fi
  mkdir -p /opt/reposilite "$var_data_dir" /var/log/reposilite
  URL="https://maven.reposilite.com/releases/com/reposilite/reposilite/${RELEASE}/reposilite-${RELEASE}-all.jar"
  download_file "$URL" /opt/reposilite/reposilite.jar
  download_file "${URL}.sha256" /opt/reposilite/reposilite.jar.sha256
  CHECKSUM="$(cat /opt/reposilite/reposilite.jar.sha256)"
  [[ "$CHECKSUM" =~ ^[[:xdigit:]]{64}$ ]] || { msg_error "Invalid Reposilite checksum"; exit 1; }
  msg_info "Verifying Reposilite download"
  $STD sha256sum -c - <<<"$CHECKSUM  /opt/reposilite/reposilite.jar"
  msg_ok "Verified Reposilite download"
  cat >/root/.reposilite <<EOF
${RELEASE}
EOF

  msg_info "Configuring Reposilite"
  install -d -m 700 /etc/reposilite
  download_file "${COMMUNITY_SCRIPTS_URL:-https://raw.githubusercontent.com/hwajin-me/proxmox-reposilite/main}/misc/reposilite-config.py" /etc/reposilite/configure.py
  if ! python3 /etc/reposilite/configure.py render; then
    msg_error "Could not render native integrations; Reposilite has not been started"
    exit 1
  fi
  source /etc/reposilite/runtime.conf
  API_BASE="http://127.0.0.1:${var_port}"
  CURL_LOCAL=(--noproxy '*')
  if [[ "$tls_enabled" == yes ]]; then
    API_BASE="https://${tls_host}:${tls_port}"
    CURL_LOCAL+=(--cacert /etc/reposilite/tls/certificate.pem --resolve "${tls_host}:${tls_port}:127.0.0.1")
  fi
  BOOTSTRAP_SECRET="$(random_password 48)"
  BOOTSTRAP_USER="setup-$(random_password 12)"
  ADMIN_SECRET="$(random_password 48)"
  umask 077
  cat >/etc/conf.d/reposilite <<EOF
# Persistent manager token (secret is saved for first login).
admin_user="${var_admin_user}"
admin_secret="${ADMIN_SECRET}"
bootstrap_user="${BOOTSTRAP_USER}"
bootstrap_secret="${BOOTSTRAP_SECRET}"
http_port="${var_port}"
data_directory="${var_data_dir}"
java_opts="-Xms32m -Xmx${var_java_heap}m ${integration_java_opts}"
tls_enabled="${tls_enabled}"
tls_host="${tls_host}"
tls_port="${tls_port}"
database_type="${database_type}"
storage_root="${storage_root}"
EOF
  chmod 600 /etc/conf.d/reposilite
  BOOTSTRAP_DIR="$(mktemp -d /run/reposilite-bootstrap.XXXXXX)"
  AUTH_FILE="$BOOTSTRAP_DIR/bootstrap-auth"
  cat >"$AUTH_FILE" <<EOF
user = "${BOOTSTRAP_USER}:${BOOTSTRAP_SECRET}"
EOF
  cat >"$BOOTSTRAP_DIR/admin-auth" <<EOF
user = "${var_admin_user}:${ADMIN_SECRET}"
EOF
  cat >"$BOOTSTRAP_DIR/create-manager" <<EOF
token-generate --secret=${ADMIN_SECRET} --silent ${var_admin_user} m
EOF
  unset BOOTSTRAP_SECRET BOOTSTRAP_USER ADMIN_SECRET
  umask 022
  cat >/etc/init.d/reposilite <<'EOF'
#!/sbin/openrc-run
description="Reposilite Maven repository manager"
[ ! -f /etc/reposilite/database.env ] || . /etc/reposilite/database.env
command="/usr/bin/java"
command_args="${java_opts} -jar /opt/reposilite/reposilite.jar --working-directory=${data_directory:-/var/lib/reposilite} --local-configuration=/etc/reposilite/configuration.cdn --hostname=0.0.0.0 --port=${http_port:-8080}"
if [ -n "${bootstrap_secret}" ]; then
  command_args="${command_args} --token=${bootstrap_user:-${admin_user:-admin}}:${bootstrap_secret}"
fi
command_background="yes"
directory="${data_directory:-/var/lib/reposilite}"
pidfile="/run/reposilite.pid"
output_log="/var/log/reposilite/console.log"
error_log="/var/log/reposilite/error.log"
retry="TERM/30/KILL/5"
depend() {
  need net
  after firewall
}
start_pre() {
  if [ -n "${storage_root}" ] && ! mountpoint -q "${storage_root}"; then
    eerror "External storage is not mounted: ${storage_root}"
    return 1
  fi
}
EOF
  chmod 755 /etc/init.d/reposilite
  $STD rc-update add reposilite default
  if ! $STD rc-service reposilite start; then
    rm -rf "$BOOTSTRAP_DIR"
    msg_error "Reposilite service could not start; inspect /var/log/reposilite"
    exit 1
  fi
  READY=no
  for ((i=0; i<60; i++)); do
    if curl -fsS "${CURL_LOCAL[@]}" --max-time 2 "${API_BASE}/" >/dev/null 2>&1; then READY=yes; break; fi
    sleep 2
  done
  if [[ "$READY" != yes ]]; then
    rm -rf "$BOOTSTRAP_DIR"
    $STD rc-service reposilite stop || true
    msg_error "Reposilite failed to start; inspect /var/log/reposilite"
    exit 1
  fi
  # Seed editable settings once and replace the temporary startup manager.
  umask 077
  SEED_OK=no
  if (
    curl -fsS "${CURL_LOCAL[@]}" --config "$AUTH_FILE" "${API_BASE}/api/settings/domain/maven" -o "$BOOTSTRAP_DIR/maven-settings.json" &&
    jq --arg public "$var_public_repositories" --arg private "$var_private_repositories" \
    --arg quota "${var_repository_quota}%" --argjson redeploy "$([[ "$var_redeployment" == yes ]] && echo true || echo false)" \
    --argjson snapshots "$([[ "$var_preserve_snapshots" == yes ]] && echo true || echo false)" \
    --slurpfile storage /etc/reposilite/storage.json \
    --argjson custom "${var_repositories_json:-null}" '
      .repositories = (if $custom != null then $custom else
        [({names: $public, visibility: "PUBLIC"}, {names: $private, visibility: "PRIVATE"}) |
          select(.names != "-") | .visibility as $visibility | .names | split(",")[] |
          {id: ., visibility: $visibility, storageProvider: ($storage[0].provider | if .type == "fs" then .quota = $quota | .mount = (if $storage[0].root == "" then "" else $storage[0].root + "/" end) else . end),
           redeployment: $redeploy, preserveSnapshots: $snapshots, proxied: []}]
        end) |
      if $custom == null and $storage[0].root != "" then
        .repositories |= map(.storageProvider.mount += .id)
      else . end'  "$BOOTSTRAP_DIR/maven-settings.json" >"$BOOTSTRAP_DIR/maven-initial.json" &&
    curl -fsS "${CURL_LOCAL[@]}" --config "$AUTH_FILE" -X PUT -H 'Content-Type: application/json' \
    --data-binary @"$BOOTSTRAP_DIR/maven-initial.json" "${API_BASE}/api/settings/domain/maven" >/dev/null &&
    curl -fsS "${CURL_LOCAL[@]}" --config "$AUTH_FILE" "${API_BASE}/api/settings/domain/frontend" -o "$BOOTSTRAP_DIR/frontend-settings.json" &&
    jq --arg title "$var_dashboard_title" --arg description "$var_dashboard_description" \
    '.title = $title | .description = $description' "$BOOTSTRAP_DIR/frontend-settings.json" >"$BOOTSTRAP_DIR/frontend-initial.json" &&
    curl -fsS "${CURL_LOCAL[@]}" --config "$AUTH_FILE" -X PUT -H 'Content-Type: application/json' \
    --data-binary @"$BOOTSTRAP_DIR/frontend-initial.json" "${API_BASE}/api/settings/domain/frontend" >/dev/null &&
    curl -fsS "${CURL_LOCAL[@]}" --config "$AUTH_FILE" "${API_BASE}/api/settings/domain/authentication" -o "$BOOTSTRAP_DIR/authentication.json" &&
    jq --slurpfile ldap /etc/reposilite/ldap.json \
      'if $ldap[0].enabled == true then .ldap = $ldap[0] else . end' \
      "$BOOTSTRAP_DIR/authentication.json" >"$BOOTSTRAP_DIR/authentication-initial.json" &&
    curl -fsS "${CURL_LOCAL[@]}" --config "$AUTH_FILE" -X PUT -H 'Content-Type: application/json' \
      --data-binary @"$BOOTSTRAP_DIR/authentication-initial.json" "${API_BASE}/api/settings/domain/authentication" >/dev/null &&
    curl -fsS "${CURL_LOCAL[@]}" --config "$AUTH_FILE" -X POST --data-binary @"$BOOTSTRAP_DIR/create-manager" \
      "${API_BASE}/api/console/execute" >/dev/null &&
    curl -fsS "${CURL_LOCAL[@]}" --config "$BOOTSTRAP_DIR/admin-auth" "${API_BASE}/api/auth/me" -o "$BOOTSTRAP_DIR/manager.json" &&
    jq -e '.permissions | any(.identifier == "access-token:manager")' "$BOOTSTRAP_DIR/manager.json" >/dev/null
  ); then
    SEED_OK=yes
  fi
  if [[ "$SEED_OK" != yes ]]; then
    rm -rf "$BOOTSTRAP_DIR"
    $STD rc-service reposilite stop || true
    msg_error "Reposilite initial configuration failed; service stopped. Inspect /var/log/reposilite."
    exit 1
  fi
  # The permanent token is verified before removing the temporary startup token.
  sed -i 's/^bootstrap_secret=.*/bootstrap_secret=""/' /etc/conf.d/reposilite
  if ! $STD rc-service reposilite restart; then
    rm -rf "$BOOTSTRAP_DIR"
    msg_error "Reposilite restart failed; the persistent token is in /etc/conf.d/reposilite"
    exit 1
  fi
  READY=no
  for ((i=0; i<60; i++)); do
    if curl -fsS "${CURL_LOCAL[@]}" --max-time 2 --config "$BOOTSTRAP_DIR/admin-auth" \
      "${API_BASE}/api/auth/me" >/dev/null 2>&1; then READY=yes; break; fi
    sleep 2
  done
  rm -rf "$BOOTSTRAP_DIR"
  rm -f /etc/reposilite/storage.json /etc/reposilite/ldap.json
  unset var_integrations_json
  umask 022
  if [[ "$READY" != yes ]]; then
    $STD rc-service reposilite stop || true
    msg_error "Reposilite persistent-token verification failed after restart"
    exit 1
  fi
  msg_ok "Configured Reposilite"
}

run_os_setup
motd_ssh
customize
cleanup_lxc
