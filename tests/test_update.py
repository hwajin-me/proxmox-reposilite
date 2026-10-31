"""Fault-injection tests for update ordering and recovery, with isolated paths."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

SOURCE = (Path(__file__).resolve().parents[1] / "ct/reposilite.sh").read_text()
GUARD_SOURCE = Path(__file__).resolve().parents[1] / "misc/reposilite-update-guard.sh"
JVM_ARGUMENTS = 'command_args="${java_opts} -jar /opt/reposilite/reposilite.jar --port=8080"\n'
CUSTOM_HOOKS = "# Custom service content\nstart_pre() { :; }\n"
INIT_CONTENT = "#!/sbin/openrc-run\n" + JVM_ARGUMENTS + CUSTOM_HOOKS
SQL_LOGGING_OPTION = "-Dtinylog.level@org.jetbrains.exposed=warn"
WRITER_OPTIONS = ("-Dtinylog.writerFile.level=info", "-Dtinylog.writerJournalist.level=info")
LOGGING_OPTIONS = " ".join((SQL_LOGGING_OPTION, *WRITER_OPTIONS))
UPDATE = SOURCE.split("function update_alpine() {", 1)[1].split("\nstart\n", 1)[0]
UPDATE = "function update_alpine() {" + UPDATE
MANAGER_SECRET = "test-only-manager-secret"
STUBS = r'''
set -eo pipefail
UPDATE_TEST_PID=$$
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
  printf '%s\n' "$1" >>"$FIXTURE/download-urls"
  [[ "${FAULT:-}" != download ]] || return 1
  if [[ "$1" == */misc/reposilite-update-guard.sh ]]; then cp "$GUARD_SOURCE" "$2"; return; fi
  if [[ $# -gt 1 ]]; then printf new-jar >"$2"; else printf '%064d' 0; fi
}
sha256sum() { [[ "${FAULT:-}" != checksum ]]; }
sync() { printf '%s\n' "$*" >"$FIXTURE/marker-synced"; }
sleep() { SECONDS=$((SECONDS + ${1:-0})); }
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
    *)
      [[ "${FAULT:-}" != health ]] || return 1
      [[ "${FAULT:-}" != budget ]] || SECONDS=$((SECONDS + 115))
      ;;
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
    [[ -f "$FIXTURE/marker-synced" && -f "$FIXTURE/etc/reposilite/update-in-progress" ]] || return 1
    [[ "${FAULT:-}" != stop ]] || return 1
    echo stopped >"$FIXTURE/service"
  elif [[ "$2" == start ]]; then
    if [[ $(cat "$FIXTURE/opt/reposilite/reposilite.jar") == new-jar ]]; then
      echo migrated-db >"$FIXTURE/data/reposilite.db"
      if [[ "${FAULT:-}" == killed-after-start ]]; then
        echo running >"$FIXTURE/service"
        kill -KILL "$UPDATE_TEST_PID"
      fi
      [[ "${FAULT:-}" != start ]] || return 1
    fi
    echo running >"$FIXTURE/service"
  fi
}
create_backup() {
  [[ $(cat "$FIXTURE/service") == stopped ]] || exit 42
  if [[ "${FAULT:-}" == killed ]]; then kill -KILL "$UPDATE_TEST_PID"; exit 0; fi
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
  [[ "${FAULT:-}" != replace || "$1" != */reposilite.jar.new ]] || return 1
  command mv "$@"
}
install() {
  [[ "${FAULT:-}" != backup-root ]] || return 1
  command install "$@"
}
'''


class UpdateTests(unittest.TestCase):
    def run_update(self, fault="", tls=False, legacy=False, manager_secret=MANAGER_SECRET, trace=False,
                   readiness_timeout=None, storage_timeout=None, custom_init=INIT_CONTENT, pending=False, managed_tls=False):
        temporary = tempfile.TemporaryDirectory(prefix="reposilite-update-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for name in ["opt/reposilite", "etc/conf.d", "etc/init.d", "root", "data", "run", "var/log/reposilite"]:
            (root / name).mkdir(parents=True, exist_ok=True)
        (root / "data").chmod(0o755)
        (root / "var/log/reposilite").chmod(0o755)
        (root / "opt/reposilite/reposilite.jar").write_text("old-jar")
        (root / "etc/init.d/reposilite").write_text(custom_init.replace("@FIXTURE@", str(root)))
        (root / "etc/init.d/reposilite").chmod(0o751)
        (root / "root/.reposilite").write_text("3.6.4\n")
        config = f'data_directory="{root}/data"\nhttp_port=9093\nadmin_user="fixture-manager"\n'
        secret = "legacy-fixture-secret" if legacy else manager_secret
        if fault != "missing-credentials":
            config += f'{"bootstrap_secret" if legacy else "admin_secret"}={shlex.quote(secret)}\n'
        if readiness_timeout is not None:
            config += f'update_readiness_timeout={shlex.quote(readiness_timeout)}\n'
        if storage_timeout is not None:
            config += f'update_storage_timeout={shlex.quote(storage_timeout)}\n'
        (root / "etc/conf.d/reposilite").write_text(config)
        if pending:
            (root / "etc/reposilite").mkdir()
            (root / "etc/reposilite/update-in-progress").write_text("existing recovery marker")
        if tls:
            (root / "etc/reposilite/tls").mkdir(parents=True)
            (root / "etc/reposilite/tls/private-key.pem").write_text("private-key-fixture")
            if managed_tls:
                managed = root / "etc/certmate/tls/reposilite"
                generation = managed / "releases/baseline"
                generation.mkdir(parents=True)
                (generation / "privkey.pem").write_text("managed-private-key")
                (generation / "privkey.pem").chmod(0o600)
                (generation / "fullchain.pem").write_text("managed-certificate")
                (managed / "current").symlink_to("releases/baseline")
                (root / "etc/reposilite/tls/private-key.pem").unlink()
                (root / "etc/reposilite/tls/private-key.pem").symlink_to(managed / "current/privkey.pem")
                (root / "etc/reposilite/tls/certificate.pem").symlink_to(managed / "current/fullchain.pem")
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
                 "EXPECTED_AUTH": f"fixture-manager:{secret}", "GUARD_SOURCE": str(GUARD_SOURCE),
                 "PHS_SILENT": "1"},
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
        self.assertIn("# BEGIN REPOSILITE UPDATE GUARD", saved_init[0].read_text())
        self.assertIn(CUSTOM_HOOKS, saved_init[0].read_text())
        self.assertIn(LOGGING_OPTIONS + " -jar", saved_init[0].read_text())
        self.assertIn('umask="0077"', (root / "etc/init.d/reposilite").read_text())
        self.assertEqual((root / "etc/init.d/reposilite").stat().st_mode & 0o777, 0o751)
        self.assertFalse((root / "etc/reposilite/update-in-progress").exists())
        self.assertFalse((root / "run/reposilite-update.authorized").exists())
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
        for fault, status in [("release-dns", 6), ("release-api", 22), ("release-empty", 250)]:
            with self.subTest(fault=fault):
                root, result = self.run_update(fault)
                self.assertEqual(result.returncode, status, result.stderr)
                self.assertEqual((root / "service").read_text().strip(), "running")
                self.assertFalse((root / "events").exists())
                self.assertEqual((root / "opt/reposilite/reposilite.jar").read_text(), "old-jar")

    def test_current_version_without_sql_protection_uses_complete_redeploy(self):
        root, result = self.run_update("no-update")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((root / "events").read_text().splitlines(), ["stop", "start"])
        self.assertEqual((root / "root/.reposilite").read_text(), "3.6.4\n")
        self.assertIn("/3.6.4/reposilite-3.6.4-all.jar", (root / "download-urls").read_text())
        self.assertEqual(len(list((root / "opt/reposilite-backups").glob("*/state/files/**/reposilite.db"))), 1)
        self.assertIn(LOGGING_OPTIONS + " -jar", (root / "etc/init.d/reposilite").read_text())
        self.assertFalse((root / "etc/reposilite/update-in-progress").exists())

    def test_current_protected_version_is_a_noop(self):
        protected = INIT_CONTENT.replace(" -jar ", " " + LOGGING_OPTIONS + " -jar ")
        root, result = self.run_update("no-update", custom_init=protected)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((root / "events").exists())
        self.assertFalse((root / "download-urls").exists())
        self.assertEqual((root / "etc/init.d/reposilite").read_text(), protected)

    def test_current_sql_only_or_partially_filtered_version_is_repaired(self):
        for old_options in [SQL_LOGGING_OPTION,
                            SQL_LOGGING_OPTION + " " + WRITER_OPTIONS[0],
                            SQL_LOGGING_OPTION + " " + WRITER_OPTIONS[1]]:
            with self.subTest(old_options=old_options):
                original = INIT_CONTENT.replace(" -jar ", " " + old_options + " -jar ")
                root, result = self.run_update("no-update", custom_init=original)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((root / "events").read_text().splitlines(), ["stop", "start"])
                self.assertEqual((root / "root/.reposilite").read_text(), "3.6.4\n")
                self.assertIn(LOGGING_OPTIONS + " -jar", (root / "etc/init.d/reposilite").read_text())
                self.assertFalse((root / "etc/reposilite/update-in-progress").exists())

    def test_sql_protection_overrides_older_debug_option_and_preserves_hooks(self):
        old_options = ("-Dtinylog.level@org.jetbrains.exposed=debug -Dtinylog.writerFile.level=debug "
                       "-Dtinylog.writerJournalist.level=debug")
        original = INIT_CONTENT.replace(" -jar ", " " + old_options + " -jar ")
        root, result = self.run_update(custom_init=original)
        self.assertEqual(result.returncode, 0, result.stderr)
        updated = (root / "etc/init.d/reposilite").read_text()
        self.assertIn(CUSTOM_HOOKS, updated)
        self.assertIn(old_options + " " + LOGGING_OPTIONS + " -jar", updated)
        for option in (SQL_LOGGING_OPTION, *WRITER_OPTIONS):
            self.assertEqual(updated.count(option), 1)
        self.assertEqual((root / "etc/init.d/reposilite").stat().st_mode & 0o777, 0o751)

    def test_native_installer_guard_is_preserved_during_sql_repair(self):
        installer = (Path(__file__).resolve().parents[1] / "install/reposilite-install.sh").read_text()
        header = installer.split("cat >/etc/init.d/reposilite <<'EOF'\n", 1)[1].split('description=', 1)[0]
        header = header.replace("/etc/", "@FIXTURE@/etc/")
        root, result = self.run_update(custom_init=header + JVM_ARGUMENTS + CUSTOM_HOOKS)
        self.assertEqual(result.returncode, 0, result.stderr)
        updated = (root / "etc/init.d/reposilite").read_text()
        self.assertTrue(updated.startswith(header.replace("@FIXTURE@", str(root))))
        self.assertEqual(updated.count("# BEGIN REPOSILITE UPDATE GUARD"), 1)
        self.assertIn(CUSTOM_HOOKS, updated)
        self.assertIn(LOGGING_OPTIONS + " -jar", updated)

    def test_logging_option_in_a_comment_does_not_suppress_current_version_repair(self):
        original = INIT_CONTENT.replace('--port=8080"', '--port=8080" # ' + LOGGING_OPTIONS + ' -jar note')
        root, result = self.run_update("no-update", custom_init=original)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((root / "events").read_text().splitlines(), ["stop", "start"])
        self.assertIn('${java_opts} ' + LOGGING_OPTIONS + ' -jar', (root / "etc/init.d/reposilite").read_text())

    def test_nonstandard_jvm_arguments_fail_before_service_or_init_changes(self):
        multiline = 'command_args="${java_opts} ' + "\\\n" + '-jar /opt/reposilite/reposilite.jar"\n'
        for args in ['command_args="@/etc/reposilite/java.args"\n', multiline,
                     'command_args="@/etc/reposilite/java.args" # -jar unused\n']:
            with self.subTest(args=args):
                original = "#!/sbin/openrc-run\n" + args + CUSTOM_HOOKS
                root, result = self.run_update(custom_init=original)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((root / "events").exists())
                self.assertEqual((root / "etc/init.d/reposilite").read_text(), original)
                self.assertFalse((root / "etc/reposilite/update-in-progress").exists())

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
                restored_init = (root / "etc/init.d/reposilite").read_text()
                self.assertIn(CUSTOM_HOOKS, restored_init)
                self.assertIn(LOGGING_OPTIONS + " -jar", restored_init)
                self.assertEqual(restored_init.count("# BEGIN REPOSILITE UPDATE GUARD"), 1)
                self.assertTrue((root / "etc/reposilite/update-in-progress").exists())
                self.assertFalse((root / "run/reposilite-update.authorized").exists())

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

    def test_managed_tls_backup_is_readable_after_the_original_generation_is_gone(self):
        root, result = self.run_update(tls=True, managed_tls=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = list((root / "opt/reposilite-backups").glob("*/state/files/**/certmate/tls/reposilite"))
        self.assertEqual(len(saved), 1)
        self.assertTrue((saved[0] / "current").is_symlink())
        shutil.rmtree(root / "etc/certmate/tls/reposilite")
        self.assertEqual((saved[0] / "current/privkey.pem").read_text(), "managed-private-key")
        self.assertEqual((saved[0] / "current/fullchain.pem").read_text(), "managed-certificate")
        self.assertEqual((saved[0] / "current/privkey.pem").stat().st_mode & 0o777, 0o600)

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

    def test_pending_recovery_prevents_even_a_silent_update(self):
        root, result = self.run_update(pending=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((root / "events").exists())
        self.assertFalse((root / "health-requests").exists())
        self.assertEqual((root / "etc/reposilite/update-in-progress").read_text(), "existing recovery marker")

    def test_nonstandard_init_is_rejected_before_stopping_service(self):
        original = "#!/bin/sh\ncustom_start() { :; }\n"
        root, result = self.run_update(custom_init=original)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((root / "events").exists())
        self.assertEqual((root / "etc/init.d/reposilite").read_text(), original)
        self.assertFalse((root / "etc/reposilite/update-in-progress").exists())
        self.assertFalse((root / "opt/reposilite/reposilite.jar.new").exists())

    def test_killed_update_keeps_persistent_boot_guard(self):
        for fault in ["killed", "killed-after-start"]:
            with self.subTest(fault=fault):
                root, result = self.run_update(fault)
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue((root / "etc/reposilite/update-in-progress").exists())
                self.assertIn("# BEGIN REPOSILITE UPDATE GUARD", (root / "etc/init.d/reposilite").read_text())
                self.assertEqual((root / "root/.reposilite").read_text(), "3.6.4\n")
                pid = int((root / "run/reposilite-update.authorized").read_text())
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)

    def test_custom_guard_reference_does_not_suppress_guard_injection(self):
        original = '#!/sbin/openrc-run\n' + JVM_ARGUMENTS + '# Note: # BEGIN REPOSILITE UPDATE GUARD\ncustom_hook() { :; }\n'
        root, result = self.run_update("health", custom_init=original)
        self.assertNotEqual(result.returncode, 0)
        restored = (root / "etc/init.d/reposilite").read_text()
        self.assertIn('/etc/reposilite/update-guard.sh', restored)
        self.assertIn('# Note: # BEGIN REPOSILITE UPDATE GUARD\ncustom_hook() { :; }\n', restored)
        self.assertTrue((root / "etc/reposilite/update-in-progress").exists())

    def test_invalid_timeout_values_fail_before_service_changes(self):
        for options in [
            {"readiness_timeout": "29"}, {"readiness_timeout": "3601"},
            {"readiness_timeout": "030"}, {"readiness_timeout": "false"},
            {"storage_timeout": "0"}, {"storage_timeout": "121"},
            {"storage_timeout": "1;false"},
        ]:
            with self.subTest(options=options):
                root, result = self.run_update(**options)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((root / "events").exists())
                self.assertFalse((root / "etc/reposilite/update-in-progress").exists())

    def test_storage_probe_uses_configured_budget_and_remaining_deadline(self):
        for fault, options, maximum in [
            ("", {}, 30), ("", {"storage_timeout": "5"}, 5),
            ("budget", {}, 5),
            ("", {"readiness_timeout": "30", "storage_timeout": "120"}, 30),
        ]:
            with self.subTest(fault=fault, options=options):
                root, result = self.run_update(fault, **options)
                self.assertEqual(result.returncode, 0, result.stderr)
                requests = (root / "health-requests").read_text().splitlines()
                storage = shlex.split(next(line for line in requests if "/api/maven/details/private" in line))
                budget = int(storage[max(i for i, value in enumerate(storage) if value == "--max-time") + 1])
                self.assertGreaterEqual(budget, 1)
                self.assertLessEqual(budget, maximum)
                if not fault and not options:
                    self.assertEqual(budget, 30)


if __name__ == "__main__":
    unittest.main()
