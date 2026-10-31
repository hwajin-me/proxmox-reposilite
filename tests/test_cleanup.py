"""Exercise post-install cleanup with isolated files and a package-manager stub."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SOURCE = (Path(__file__).resolve().parents[1] / "install/reposilite-install.sh").read_text()
CLEANUP = "cleanup_reposilite_install_files() {" + SOURCE.split("cleanup_reposilite_install_files() {", 1)[1].split("\n}\n", 1)[0] + "\n}\n"
JAVA = "  # The source-launch JDBC preflight" + SOURCE.split("  # The source-launch JDBC preflight", 1)[1].split('  msg_info "Configuring Reposilite"', 1)[0]


class InstallationCleanupTests(unittest.TestCase):
    def test_only_setup_files_are_removed_and_repeated_cleanup_is_safe(self):
        with tempfile.TemporaryDirectory(prefix="reposilite-install-cleanup-") as folder:
            root = Path(folder)
            transient = ("opt/reposilite/reposilite.jar.sha256", "etc/reposilite/configure.py",
                         "etc/reposilite/runtime.conf", "etc/reposilite/database-check.json",
                         "etc/reposilite/DatabasePreflight.java", "etc/reposilite/storage.json",
                         "etc/reposilite/ldap.json")
            persistent = ("opt/reposilite/reposilite.jar", "root/.reposilite",
                          "etc/conf.d/reposilite", "etc/reposilite/configuration.cdn",
                          "etc/reposilite/database.env", "etc/reposilite/truststore.p12",
                          "etc/reposilite/ca-0.pem", "etc/reposilite/tls/certificate.pem",
                          "etc/reposilite/tls/private-key.pem", "etc/reposilite/update-guard.sh",
                          "etc/reposilite/update-in-progress", "etc/reposilite/operator-notes",
                          "var/lib/reposilite/reposilite.db", "var/lib/reposilite/artifact.jar",
                          "var/log/reposilite/console.log", "opt/reposilite-backups/state",
                          "run/reposilite-update.authorized", "run/reposilite-update.lock")
            for name in transient + persistent:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"preserve content")
                path.chmod(0o600)
            script = CLEANUP.replace("/opt/reposilite", str(root / "opt/reposilite")).replace(
                "/etc/reposilite", str(root / "etc/reposilite"))
            result = subprocess.run(["bash", "-ec", script + "\ncleanup_reposilite_install_files\ncleanup_reposilite_install_files"],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            for name in transient:
                self.assertFalse((root / name).exists(), name)
            for name in persistent:
                self.assertEqual((root / name).read_bytes(), b"preserve content", name)
                self.assertEqual((root / name).stat().st_mode & 0o777, 0o600, name)

    def test_removing_a_setup_symlink_does_not_remove_its_target(self):
        with tempfile.TemporaryDirectory(prefix="reposilite-install-symlink-") as folder:
            root = Path(folder)
            (root / "etc").mkdir()
            (root / "opt").mkdir()
            target = root / "keep.pem"
            target.write_text("runtime certificate")
            link = root / "etc/configure.py"
            link.symlink_to(target)
            script = CLEANUP.replace("/etc/reposilite", str(root / "etc")).replace("/opt/reposilite", str(root / "opt"))
            result = subprocess.run(["bash", "-ec", script + "\ncleanup_reposilite_install_files"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(link.is_symlink())
            self.assertEqual(target.read_text(), "runtime certificate")

    def run_java_cleanup(self, fault=""):
        with tempfile.TemporaryDirectory(prefix="reposilite-java-cleanup-") as folder:
            calls = Path(folder) / "calls"
            script = r'''
set -eo pipefail
STD=""
msg_info() { :; }
msg_ok() { :; }
apk() {
  printf '%s\n' "$*" >>"$CALLS"
  if [[ "$1" == "$FAULT" ]]; then return 42; fi
  return 0
}
''' + JAVA
            result = subprocess.run(["bash", "-c", script], env={**os.environ, "CALLS": str(calls), "FAULT": fault},
                                    capture_output=True, text=True)
            return result, calls.read_text().splitlines()

    def test_headless_runtime_is_pinned_before_removing_the_jdk(self):
        result, calls = self.run_java_cleanup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, ["add --no-cache openjdk21-jre-headless", "del openjdk21-jdk"])
        self.assertLess(SOURCE.index('msg_ok "Verified empty Reposilite external database"'), SOURCE.index(JAVA))
        self.assertLess(SOURCE.index(JAVA), SOURCE.index("rc-service reposilite start"))

    def test_package_failure_stops_without_removing_an_unprotected_runtime(self):
        for fault, expected in (("add", ["add --no-cache openjdk21-jre-headless"]),
                                ("del", ["add --no-cache openjdk21-jre-headless", "del openjdk21-jdk"])):
            with self.subTest(fault=fault):
                result, calls = self.run_java_cleanup(fault)
                self.assertEqual(result.returncode, 42, result.stderr)
                self.assertEqual(calls, expected)


if __name__ == "__main__":
    unittest.main()
