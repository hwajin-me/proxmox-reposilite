"""Fault-injection tests for update ordering and recovery, with isolated paths."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SOURCE = (Path(__file__).resolve().parents[1] / "ct/reposilite.sh").read_text()
UPDATE = SOURCE.split("function update_script() {", 1)[1].split("\nstart\n", 1)[0]
UPDATE = "function update_script() {" + UPDATE
STUBS = r'''
header_info() { :; }
check_container_storage() { :; }
check_container_resources() { :; }
msg_info() { :; }
msg_ok() { :; }
msg_error() { echo "$*" >&2; }
flock() { [[ "${FAULT:-}" != locked ]]; }
check_for_gh_release() { CHECK_UPDATE_RELEASE=v3.6.5; return 0; }
curl_with_retry() {
  [[ "${FAULT:-}" != download ]] || return 1
  if [[ $# -gt 1 ]]; then printf new-jar >"$2"; else printf '%064d' 0; fi
}
sha256sum() { [[ "${FAULT:-}" != checksum ]]; }
sleep() { :; }
curl() {
  printf '%s\n' "$*" >>"$FIXTURE/health-requests"
  [[ $(cat "$FIXTURE/service") == running && "${FAULT:-}" != health ]]
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
    def run_update(self, fault="", tls=False):
        temporary = tempfile.TemporaryDirectory(prefix="reposilite-update-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for name in ["opt/reposilite", "etc/conf.d", "etc/init.d", "root", "data", "run"]:
            (root / name).mkdir(parents=True, exist_ok=True)
        (root / "opt/reposilite/reposilite.jar").write_text("old-jar")
        (root / "etc/init.d/reposilite").write_text("service")
        (root / "root/.reposilite").write_text("3.6.4\n")
        (root / "etc/conf.d/reposilite").write_text(f'data_directory="{root}/data"\nhttp_port=9093\n')
        if tls:
            (root / "etc/reposilite/tls").mkdir(parents=True)
            (root / "etc/reposilite/tls/private-key.pem").write_text("private-key-fixture")
            with (root / "etc/conf.d/reposilite").open("a") as config:
                config.write('tls_enabled=yes\ntls_host=repo.example.test\ntls_port=8443\n')
        (root / "data/reposilite.db").write_text("original-db")
        (root / "data/configuration.cdn").write_text("original-config")
        (root / "service").write_text("running")
        code = UPDATE
        for prefix in ["/opt/", "/etc/", "/root/", "/run/", "/var/lib/reposilite"]:
            code = code.replace(prefix, str(root) + ("/data" if prefix == "/var/lib/reposilite" else prefix))
        result = subprocess.run(
            ["bash", "-c", STUBS + "\n" + code + "\nupdate_script\n"],
            env={**os.environ, "FIXTURE": str(root), "FAULT": fault, "STD": ""},
            capture_output=True, text=True,
        )
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

    def test_preflight_failures_never_stop_running_service(self):
        for fault in ["locked", "download", "checksum", "stop"]:
            with self.subTest(fault=fault):
                root, result = self.run_update(fault)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual((root / "service").read_text().strip(), "running")
                self.assertEqual((root / "opt/reposilite/reposilite.jar").read_text(), "old-jar")
                self.assertFalse((root / "opt/reposilite/reposilite.jar.new").exists())

    def test_backup_failures_restart_the_unchanged_application(self):
        for fault in ["backup-root", "backup-application", "backup-state"]:
            with self.subTest(fault=fault):
                root, result = self.run_update(fault)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual((root / "service").read_text().strip(), "running")
                self.assertEqual((root / "opt/reposilite/reposilite.jar").read_text(), "old-jar")

    def test_start_and_health_failures_restore_jar_without_overwriting_migrated_state(self):
        for fault in ["start", "health"]:
            with self.subTest(fault=fault):
                root, result = self.run_update(fault)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual((root / "service").read_text().strip(), "stopped")
                self.assertEqual((root / "opt/reposilite/reposilite.jar").read_text(), "old-jar")
                self.assertEqual((root / "root/.reposilite").read_text(), "3.6.4\n")
                self.assertEqual((root / "data/reposilite.db").read_text().strip(), "migrated-db")
                saved_db = list((root / "opt/reposilite-backups").glob("*/state/files/**/reposilite.db"))
                self.assertEqual(saved_db[0].read_text(), "original-db")

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
        self.assertNotIn("--insecure", health)
        saved = list((root / "opt/reposilite-backups").glob("*/state/files/**/private-key.pem"))
        self.assertEqual(saved[0].read_text(), "private-key-fixture")


if __name__ == "__main__":
    unittest.main()
