"""Ensure rejected API calls cannot leave installer credentials on disk."""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

SOURCE = (Path(__file__).resolve().parents[1] / "install/reposilite-install.sh").read_text()
SEED = SOURCE.split("  # Seed editable settings once", 1)[1].split("\n}\n", 1)[0]
CLEANUP = "cleanup_reposilite_install_files() {" + SOURCE.split("cleanup_reposilite_install_files() {", 1)[1].split("\n}\n", 1)[0] + "\n}\n"
SEED = SEED.split("\n", 1)[1]
STUBS = r'''
CURL_LOCAL=(--noproxy '*' --connect-timeout 5 --max-time 30)
msg_error() { echo "$*" >&2; }
msg_info() { :; }
msg_ok() { :; }
sleep() { :; }
sed() {
  if [[ $(uname -s) == Darwin && "$1" == -i ]]; then
    shift
    command sed -i '' "$@"
  else
    command sed "$@"
  fi
}
rc-service() {
  echo "$2" >>"$FIXTURE/service-events"
  [[ "${FAULT:-}" != restart || "$2" != restart ]]
}
curl() {
  count=$(cat "$FIXTURE/calls")
  count=$((count + 1))
  echo "$count" >"$FIXTURE/calls"
  printf '%s %s\n' "$count" "$*" >>"$FIXTURE/requests"
  [[ "${FAULT:-}" != "$count" ]] || return 1
  [[ "${FAULT:-}" != verify || "$count" -lt 9 ]] || return 1
  output=""
  while [[ $# -gt 0 ]]; do
    if [[ "$1" == -o ]]; then output="$2"; shift; fi
    shift
  done
  if [[ -n "$output" ]]; then
    case "$count" in
      1) echo '{"repositories":[]}' >"$output" ;;
      3) echo '{"title":"Original","description":"Original"}' >"$output" ;;
      5) echo '{"ldap":{"enabled":false}}' >"$output" ;;
      8) echo '{"permissions":[{"identifier":"access-token:manager"}]}' >"$output" ;;
      10)
        case "${FAULT:-}" in
          missing-provider) echo '{"type":"DIRECTORY","files":[{"name":"stable"}]}' >"$output" ;;
          invalid-provider) echo '{"type":"FILE","files":[{"name":"stable"},{"name":"internal"}]}' >"$output" ;;
          *) echo '{"type":"DIRECTORY","files":[{"name":"stable"},{"name":"internal"}]}' >"$output" ;;
        esac
        ;;
      11|12) echo '{"type":"DIRECTORY","files":[]}' >"$output" ;;
    esac
  fi
}
'''


class BootstrapTests(unittest.TestCase):
    def test_service_filters_all_exposed_sql_packages_before_jar(self):
        init = SOURCE.split("cat >/etc/init.d/reposilite <<'EOF'", 1)[1].split("\nEOF", 1)[0]
        assignment = next(line for line in init.splitlines() if line.startswith('command_args='))
        # Existing user JVM arguments may set a less restrictive threshold;
        # the installer property must be the final JVM option before -jar.
        result = subprocess.run(["sh", "-c",
                                 'java_opts="-Xmx256m -Dtinylog.level@org.jetbrains.exposed=debug"; '
                                 + assignment + '; printf "%s" "$command_args"'],
                                capture_output=True, text=True, check=True)
        arguments = shlex.split(result.stdout)
        jar = arguments.index("-jar")
        self.assertEqual(arguments[jar - 1], "-Dtinylog.level@org.jetbrains.exposed=warn")
        self.assertIn("--local-configuration=/etc/reposilite/configuration.cdn", arguments[jar + 1:])

    def run_seed(self, fault=""):
        temporary = tempfile.TemporaryDirectory(prefix="reposilite-bootstrap-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        private = root / "private"
        private.mkdir(mode=0o700)
        for name in ["bootstrap-auth", "admin-auth", "create-manager"]:
            (private / name).write_text("sensitive fixture")
        (root / "calls").write_text("0")
        (root / "application").mkdir()
        (root / "application/reposilite.jar.sha256").write_text("verified checksum")
        (root / "configure.py").write_text("installer helper")
        (root / "runtime.conf").write_text("installer variables")
        (root / "storage.json").write_text('{"provider":{"type":"fs","mount":"","quota":"100%"},"root":""}')
        (root / "ldap.json").write_text('{}')
        (root / "config").write_text('bootstrap_secret="temporary"\nadmin_secret="persistent"\n')
        seed = (CLEANUP + "\n" + SEED).replace("/etc/conf.d/reposilite", str(root / "config")).replace("/etc/reposilite", str(root)).replace("/opt/reposilite", str(root / "application"))
        result = subprocess.run(["bash", "-c", STUBS + "\n" + seed], capture_output=True, text=True, env={
            **os.environ, "FAULT": fault, "FIXTURE": str(root), "STD": "",
            "BOOTSTRAP_DIR": str(private), "AUTH_FILE": str(private / "bootstrap-auth"),
            "var_port": "9093", "var_public_repositories": "stable", "var_private_repositories": "internal",
            "var_repository_quota": "75", "var_redeployment": "no", "var_preserve_snapshots": "no",
            "var_repositories_json": "", "var_dashboard_title": "Team Maven", "var_dashboard_description": "Team",
        })
        for leftover in ("configure.py", "runtime.conf", "application/reposilite.jar.sha256"):
            self.assertEqual((root / leftover).exists(), result.returncode != 0)
        self.assertNotIn("sensitive fixture", result.stdout + result.stderr)
        if (root / "requests").exists():
            self.assertNotIn("sensitive fixture", (root / "requests").read_text())
        return root, result

    def test_every_failed_api_step_removes_private_files_and_stops_service(self):
        for count in range(1, 9):
            with self.subTest(step=count):
                root, result = self.run_seed(str(count))
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((root / "private").exists())
                self.assertIn("stop", (root / "service-events").read_text())
                self.assertIn('bootstrap_secret="temporary"', (root / "config").read_text())

    def test_restart_failure_removes_private_files_after_verified_token(self):
        root, result = self.run_seed("restart")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((root / "private").exists())
        self.assertIn('bootstrap_secret=""', (root / "config").read_text())
        self.assertIn("restart", (root / "service-events").read_text())

    def test_post_restart_verification_failure_cleans_up_and_stops(self):
        root, result = self.run_seed("verify")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((root / "private").exists())
        self.assertEqual((root / "service-events").read_text().splitlines(), ["restart", "stop"])

    def test_provider_request_and_missing_provider_failures_stop_after_restart(self):
        for fault in ["10", "missing-provider", "invalid-provider"]:
            with self.subTest(fault=fault):
                root, result = self.run_seed(fault)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((root / "private").exists())
                self.assertFalse((root / "storage.json").exists())
                self.assertFalse((root / "ldap.json").exists())
                self.assertEqual((root / "calls").read_text().strip(), "10")
                self.assertEqual((root / "service-events").read_text().splitlines(), ["restart", "stop"])
                self.assertIn('bootstrap_secret=""', (root / "config").read_text())

    def test_success_verifies_active_providers_before_private_cleanup(self):
        root, result = self.run_seed()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((root / "calls").read_text().strip(), "12")
        self.assertEqual((root / "service-events").read_text().splitlines(), ["restart"])
        self.assertFalse((root / "private").exists())
        self.assertFalse((root / "storage.json").exists())
        self.assertFalse((root / "ldap.json").exists())
        self.assertIn('bootstrap_secret=""', (root / "config").read_text())
        self.assertIn('admin_secret="persistent"', (root / "config").read_text())
        requests = (root / "requests").read_text().splitlines()
        self.assertTrue(all("--noproxy *" in request for request in requests))
        self.assertTrue(all("--connect-timeout 5" in request and "--max-time" in request for request in requests))
        self.assertIn("/api/maven/details", requests[-1])
        self.assertIn(str(root / "private/admin-auth"), requests[-1])


    def test_each_repository_storage_probe_must_succeed(self):
        for count in (11, 12):
            with self.subTest(step=count):
                root, result = self.run_seed(str(count))
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((root / "private").exists())
                self.assertEqual((root / "service-events").read_text().splitlines(), ["restart", "stop"])


if __name__ == "__main__":
    unittest.main()
