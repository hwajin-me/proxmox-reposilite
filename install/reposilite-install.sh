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
  $STD apk add --no-cache tzdata flock
  msg_ok "Installed Reposilite dependencies"

  # GitHub releases link to Maven-hosted JARs and have no GitHub binary assets.
  # fetch_and_deploy_from_url extracts ZIPs, which would unpack a runnable JAR.
  RELEASE="${APPLICATION_VERSION:-$(get_latest_github_release "dzikoysk/reposilite")}"
  [[ "$RELEASE" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { msg_error "Invalid Reposilite release"; exit 1; }
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
java_opts="-Xms32m -Xmx${var_java_heap}m"
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
command="/usr/bin/java"
command_args="${java_opts} -jar /opt/reposilite/reposilite.jar --working-directory=${data_directory:-/var/lib/reposilite} --hostname=0.0.0.0 --port=${http_port:-8080}"
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
    if curl -fsS --max-time 2 "http://127.0.0.1:${var_port}/" >/dev/null 2>&1; then READY=yes; break; fi
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
    curl -fsS --config "$AUTH_FILE" "http://127.0.0.1:${var_port}/api/settings/domain/maven" -o "$BOOTSTRAP_DIR/maven-settings.json" &&
    jq --arg public "$var_public_repositories" --arg private "$var_private_repositories" \
    --arg quota "${var_repository_quota}%" --argjson redeploy "$([[ "$var_redeployment" == yes ]] && echo true || echo false)" \
    --argjson snapshots "$([[ "$var_preserve_snapshots" == yes ]] && echo true || echo false)" \
    --argjson custom "${var_repositories_json:-null}" '
      .repositories = (if $custom != null then $custom else
        [({names: $public, visibility: "PUBLIC"}, {names: $private, visibility: "PRIVATE"}) |
          select(.names != "-") | .visibility as $visibility | .names | split(",")[] |
          {id: ., visibility: $visibility, storageProvider: {type: "fs", quota: $quota, mount: ""},
           redeployment: $redeploy, preserveSnapshots: $snapshots, proxied: []}]
        end)' "$BOOTSTRAP_DIR/maven-settings.json" >"$BOOTSTRAP_DIR/maven-initial.json" &&
    curl -fsS --config "$AUTH_FILE" -X PUT -H 'Content-Type: application/json' \
    --data-binary @"$BOOTSTRAP_DIR/maven-initial.json" "http://127.0.0.1:${var_port}/api/settings/domain/maven" >/dev/null &&
    curl -fsS --config "$AUTH_FILE" "http://127.0.0.1:${var_port}/api/settings/domain/frontend" -o "$BOOTSTRAP_DIR/frontend-settings.json" &&
    jq --arg title "$var_dashboard_title" --arg description "$var_dashboard_description" \
    '.title = $title | .description = $description' "$BOOTSTRAP_DIR/frontend-settings.json" >"$BOOTSTRAP_DIR/frontend-initial.json" &&
    curl -fsS --config "$AUTH_FILE" -X PUT -H 'Content-Type: application/json' \
    --data-binary @"$BOOTSTRAP_DIR/frontend-initial.json" "http://127.0.0.1:${var_port}/api/settings/domain/frontend" >/dev/null &&
    curl -fsS --config "$AUTH_FILE" -X POST --data-binary @"$BOOTSTRAP_DIR/create-manager" \
      "http://127.0.0.1:${var_port}/api/console/execute" >/dev/null &&
    curl -fsS --config "$BOOTSTRAP_DIR/admin-auth" "http://127.0.0.1:${var_port}/api/auth/me" -o "$BOOTSTRAP_DIR/manager.json" &&
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
    if curl -fsS --max-time 2 --config "$BOOTSTRAP_DIR/admin-auth" \
      "http://127.0.0.1:${var_port}/api/auth/me" >/dev/null 2>&1; then READY=yes; break; fi
    sleep 2
  done
  rm -rf "$BOOTSTRAP_DIR"
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
