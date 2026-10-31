"""Fault-injection tests for update ordering and recovery, with isolated paths."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

SOURCE = (Path(__file__).resolve().parents[1] / "ct/reposilite.sh").read_text()
UPDATE = SOURCE.split("function update_alpine() {", 1)[1].split("\nstart\n", 1)[0]
UPDATE = "function update_alpine() {" + UPDATE
MANAGER_SECRET = "test-only-manager-secret"
STUBS = r'''
set -eo pipefail
header_info() { :; }
check_container_storage() { :; }
check_container_resources() { :; }
run_os_update() { update_alpine; }
msg_info() { :; }
msg_ok() { :; }
msg_error() { echo "$*" >&2; }
flock() { [[ "${FAULT:-}" != locked ]]; }
check_for_gh_release() {
  case "${FAULT:-}" in
    no-update) return 1 ;;
    release-dns) return 6 ;;
    release-api) return 22 ;;
    release-empty) return 250 ;;
  esac
  CHECK_UPDATE_RELEASE=v3.6.5
  return 0
}
curl_with_retry() {
  [[ "${FAULT:-}" != download ]] || return 1
  if [[ $# -gt 1 ]]; then printf new-jar >"$2"; else printf '%064d' 0; fi
}
sha256sum() { [[ "${FAULT:-}" != checksum ]]; }
sleep() { :; }
curl() {
  printf '%s\n' "$*" >>"$FIXTURE/health-requests"
  local output="" url="" config="" auth="" body=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -o|--output) output="$2"; shift ;;
      --config) config="$2"; shift ;;
      http://*|https://*) url="$1" ;;
    esac
    shift
  done
  if [[ "$url" == */api/* ]]; then
    [[ "$config" == - ]] || return 1
    auth=$(cat)
    printf '%s\n' "$auth" >>"$FIXTURE/stdin-auth"
    [[ "$auth" == "user = $(printf '%s' "$EXPECTED_AUTH" | jq -Rs .)" ]] || return 1
    [[ "${FAULT:-}" != credentials ]] || return 1
  fi
  [[ $(cat "$FIXTURE/service") == running ]] || return 1
  case "$url" in
    */api/settings/domain/maven)
      [[ "${FAULT:-}" != settings ]] || return 1
      case "${FAULT:-}" in
        settings-empty) return 0 ;;
        settings-json) body='{"repositories":null}' ;;
        settings-missing-id) body='{"repositories":[{}]}' ;;
        settings-duplicate-id) body='{"repositories":[{"id":"private"},{"id":"private"}]}' ;;
        *) body='{"repositories":[{"id":"private"}]}' ;;
      esac
      ;;
    */api/maven/details)
      [[ "${FAULT:-}" != providers ]] || return 1
      case "${FAULT:-}" in
        missing-provider) body='{"type":"DIRECTORY","files":[]}' ;;
        invalid-provider) body='{"type":"FILE","files":[{"name":"private"}]}' ;;
        *) body='{"type":"DIRECTORY","files":[{"name":"private"}]}' ;;
      esac
      ;;
    */api/maven/details/*)
      [[ "${FAULT:-}" != unreachable-storage ]] || return 1
      body='{"type":"DIRECTORY","files":[]}'
      ;;
    *) [[ "${FAULT:-}" != health ]] || return 1 ;;
  esac
  if [[ -n "$output" ]]; then
    printf '%s' "$body" >"$output"
  else
    printf '%s' "$body"
  fi
}
rc-service() {
  echo "$2" >>"$FIXTURE/events"
  if [[ "$2" == stop ]]; then
    [[ "${FAULT:-}" != stop ]] || return 1
    echo stopped >"$FIXTURE/service"
  elif [[ "$2" == start ]]; then
    if [[ $(cat "$FIXTURE/opt/reposilite/reposilite.jar") == new-jar ]]; then
      echo migrated-db >"$FIXTURE/data/reposilite.db"
      [[ "${FAULT:-}" != start ]] || return 1
    fi
    echo running >"$FIXTURE/service"
  fi
}
create_backup() {
  [[ $(cat "$FIXTURE/service") == stopped ]] || exit 42
  [[ "${FAULT:-}" != "backup-${BACKUP_DIR##*/}" ]] || exit 1
  mkdir -p "$BACKUP_DIR"
  for file in "$@"; do
    mkdir -p "$BACKUP_DIR/files$(dirname "$file")"
    cp -Rp "$file" "$BACKUP_DIR/files$file"
    printf '%s\n' "$file" >>"$BACKUP_DIR/manifest"
  done
}
restore_backup() {
  while IFS= read -r file; do cp -p "$BACKUP_DIR/files$file" "$file"; done <"$BACKUP_DIR/manifest"
  rm -rf "$BACKUP_DIR"
}
mv() {
  [[ "${FAULT:-}" != replace ]] || return 1
  command mv "$@"
}
install() {
  [[ "${FAULT:-}" != backup-root ]] || return 1
  command install "$@"
}
'''


class UpdateTests(unittest.TestCase):
    def run_update(self, fault="", tls=False, legacy=False, manager_secret=MANAGER_SECRET, trace=False):
        temporary = tempfile.TemporaryDirectory(prefix="reposilite-update-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for name in ["opt/reposilite", "etc/conf.d", "etc/init.d", "root", "data", "run", "var/log/reposilite"]:
            (root / name).mkdir(parents=True, exist_ok=True)
        (root / "data").chmod(0o755)
        (root / "var/log/reposilite").chmod(0o755)
        (root / "opt/reposilite/reposilite.jar").write_text("old-jar")
        (root / "etc/init.d/reposilite").write_text("service")
        (root / "root/.reposilite").write_text("3.6.4\n")
        config = f'data_directory="{root}/data"\nhttp_port=9093\nadmin_user="fixture-manager"\n'
        secret = "legacy-fixture-secret" if legacy else manager_secret
        if fault != "missing-credentials":
            config += f'{"bootstrap_secret" if legacy else "admin_secret"}={shlex.quote(secret)}\n'
        (root / "etc/conf.d/reposilite").write_text(config)
        if tls:
            (root / "etc/reposilite/tls").mkdir(parents=True)
            (root / "etc/reposilite/tls/private-key.pem").write_text("private-key-fixture")
            with (root / "etc/conf.d/reposilite").open("a") as config:
                config.write('tls_enabled=yes\ntls_host=repo.example.test\ntls_port=8443\n')
        for suffix in ["", "-wal", "-shm", "-journal"]:
            state = root / f"data/reposilite.db{suffix}"
            state.write_text("original-db" + suffix)
            state.chmod(0o644)
        (root / "data/configuration.cdn").write_text("original-config")
        (root / "service").write_text("running")
        code = UPDATE
        for prefix in ["/opt/", "/etc/", "/root/", "/run/", "/var/log/", "/var/lib/reposilite"]:
            code = code.replace(prefix, str(root) + ("/data" if prefix == "/var/lib/reposilite" else prefix))
        result = subprocess.run(
            ["bash", *(["-x"] if trace else []), "-c", STUBS + "\n" + code + "\nupdate_script\n"],
            env={**os.environ, "FIXTURE": str(root), "FAULT": fault, "STD": "",
                 "EXPECTED_AUTH": f"fixture-manager:{secret}"},
            capture_output=True, text=True,
        )
        self.assertNotIn(secret, result.stdout + result.stderr)
        if (root / "health-requests").exists():
            self.assertNotIn(secret, (root / "health-requests").read_text())
        return root, result

    def test_success_backs_up_consistent_database_and_records_version(self):
        root, result = self.run_update()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((root / "service").read_text().strip(), "running")
        self.assertEqual((root / "opt/reposilite/reposilite.jar").read_text(), "new-jar")
        self.assertEqual((root / "root/.reposilite").read_text(), "3.6.5\n")
        saved_db = list((root / "opt/reposilite-backups").glob("*/state/files/**/reposilite.db"))
        self.assertEqual(len(saved_db), 1)
        self.assertEqual(saved_db[0].read_text(), "original-db")
        saved_journal = list((root / "opt/reposilite-backups").glob("*/state/files/**/reposilite.db-journal"))
        self.assertEqual(saved_journal[0].read_text(), "original-db-journal")
        saved_init = list((root / "opt/reposilite-backups").glob("*/application/files/**/init.d/reposilite"))
        self.assertEqual(saved_init[0].read_text(), "service")
        self.assertIn('umask="0077"', (root / "etc/init.d/reposilite").read_text())
        self.assertEqual((root / "data").stat().st_mode & 0o777, 0o700)
        self.assertEqual((root / "var/log/reposilite").stat().st_mode & 0o777, 0o700)
        for suffix in ["", "-wal", "-shm", "-journal"]:
            self.assertEqual((root / f"data/reposilite.db{suffix}").stat().st_mode & 0o777, 0o600)
        requests = (root / "health-requests").read_text().splitlines()
        self.assertTrue(all("--noproxy *" in request for request in requests))
        self.assertTrue(all("--config -" in request for request in requests if "/api/" in request))
        self.assertIn("/api/settings/domain/maven", requests[0])
        self.assertIn("/api/maven/details", requests[-1])
        self.assertIn(f"fixture-manager:{MANAGER_SECRET}", (root / "stdin-auth").read_text())

    def test_preflight_failures_never_stop_running_service(self):
        for fault in ["locked", "missing-credentials", "credentials", "settings", "settings-empty",
                      "settings-json", "settings-missing-id", "settings-duplicate-id", "download", "checksum", "stop"]:
            with self.subTest(fault=fault):
                root, result = self.run_update(fault)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual((root / "service").read_text().strip(), "running")
                self.assertEqual((root / "opt/reposilite/reposilite.jar").read_text(), "old-jar")
                self.assertFalse((root / "opt/reposilite/reposilite.jar.new").exists())
                if fault in ["missing-credentials", "credentials", "settings", "settings-empty", "settings-json", "settings-missing-id", "settings-duplicate-id"]:
                    self.assertFalse((root / "events").exists())

    def test_release_lookup_errors_remain_errors_without_stopping_service(self):
        for fault, status in [("no-update", 0), ("release-dns", 6), ("release-api", 22), ("release-empty", 250)]:
            with self.subTest(fault=fault):
                root, result = self.run_update(fault)
                self.assertEqual(result.returncode, status, result.stderr)
                self.assertEqual((root / "service").read_text().strip(), "running")
                self.assertFalse((root / "events").exists())
                self.assertEqual((root / "opt/reposilite/reposilite.jar").read_text(), "old-jar")

    def test_backup_failures_restart_the_unchanged_application(self):
        for fault in ["backup-root", "backup-application", "backup-state"]:
            with self.subTest(fault=fault):
                root, result = self.run_update(fault)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual((root / "service").read_text().strip(), "running")
                self.assertEqual((root / "opt/reposilite/reposilite.jar").read_text(), "old-jar")

    def test_start_and_health_failures_restore_jar_without_overwriting_migrated_state(self):
        for fault in ["start", "health", "providers", "missing-provider", "invalid-provider", "unreachable-storage"]:
            with self.subTest(fault=fault):
                root, result = self.run_update(fault)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual((root / "service").read_text().strip(), "stopped")
                self.assertEqual((root / "opt/reposilite/reposilite.jar").read_text(), "old-jar")
                self.assertEqual((root / "root/.reposilite").read_text(), "3.6.4\n")
                self.assertEqual((root / "data/reposilite.db").read_text().strip(), "migrated-db")
                saved_db = list((root / "opt/reposilite-backups").glob("*/state/files/**/reposilite.db"))
                self.assertEqual(saved_db[0].read_text(), "original-db")
                self.assertEqual((root / "etc/init.d/reposilite").read_text(), "service")

    def test_file_replacement_failure_cleans_download_and_preserves_old_jar(self):
        root, result = self.run_update("replace")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((root / "service").read_text().strip(), "stopped")
        self.assertEqual((root / "opt/reposilite/reposilite.jar").read_text(), "old-jar")
        self.assertFalse((root / "opt/reposilite/reposilite.jar.new").exists())

    def test_native_tls_health_checks_and_certificate_backup(self):
        root, result = self.run_update(tls=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        health = (root / "health-requests").read_text()
        self.assertIn("https://repo.example.test:8443/", health)
        self.assertIn("--resolve repo.example.test:8443:127.0.0.1", health)
        self.assertIn("--cacert", health)
        self.assertIn("--noproxy *", health)
        self.assertNotIn("--insecure", health)
        saved = list((root / "opt/reposilite-backups").glob("*/state/files/**/private-key.pem"))
        self.assertEqual(saved[0].read_text(), "private-key-fixture")

    def test_legacy_bootstrap_credentials_remain_usable_for_update(self):
        root, result = self.run_update(legacy=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("fixture-manager:legacy-fixture-secret", (root / "stdin-auth").read_text())

    def test_rotated_quoted_secret_uses_stdin_and_is_not_traced(self):
        secret = 'rotated "quote" \\ backslash $value'
        root, result = self.run_update(manager_secret=secret, trace=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        encoded_auth = (root / "stdin-auth").read_text().splitlines()[0].removeprefix("user = ")
        self.assertEqual(json.loads(encoded_auth), f"fixture-manager:{secret}")


if __name__ == "__main__":
    unittest.main()
