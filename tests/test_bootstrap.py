"""Ensure rejected API calls cannot leave installer credentials on disk."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SOURCE = (Path(__file__).resolve().parents[1] / "install/reposilite-install.sh").read_text()
SEED = SOURCE.split("  # Seed editable settings once", 1)[1].split('  msg_ok "Configured Reposilite"', 1)[0]
SEED = SEED.split("\n", 1)[1]
STUBS = r'''
msg_error() { echo "$*" >&2; }
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
  [[ "${FAULT:-}" != "$count" ]] || return 1
  [[ "${FAULT:-}" != verify || "$count" -lt 7 ]] || return 1
  output=""
  while [[ $# -gt 0 ]]; do
    if [[ "$1" == -o ]]; then output="$2"; shift; fi
    shift
  done
  if [[ -n "$output" ]]; then
    case "$count" in
      1) echo '{"repositories":[]}' >"$output" ;;
      3) echo '{"title":"Original","description":"Original"}' >"$output" ;;
      6) echo '{"permissions":[{"identifier":"access-token:manager"}]}' >"$output" ;;
    esac
  fi
}
'''


class BootstrapTests(unittest.TestCase):
    def run_seed(self, fault):
        temporary = tempfile.TemporaryDirectory(prefix="reposilite-bootstrap-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        private = root / "private"
        private.mkdir(mode=0o700)
        for name in ["bootstrap-auth", "admin-auth", "create-manager"]:
            (private / name).write_text("sensitive fixture")
        (root / "calls").write_text("0")
        (root / "config").write_text('bootstrap_secret="temporary"\nadmin_secret="persistent"\n')
        seed = SEED.replace("/etc/conf.d/reposilite", str(root / "config"))
        result = subprocess.run(["bash", "-c", STUBS + "\n" + seed], capture_output=True, text=True, env={
            **os.environ, "FAULT": fault, "FIXTURE": str(root), "STD": "",
            "BOOTSTRAP_DIR": str(private), "AUTH_FILE": str(private / "bootstrap-auth"),
            "var_port": "9093", "var_public_repositories": "stable", "var_private_repositories": "internal",
            "var_repository_quota": "75", "var_redeployment": "no", "var_preserve_snapshots": "no",
            "var_repositories_json": "", "var_dashboard_title": "Team Maven", "var_dashboard_description": "Team",
        })
        return root, result

    def test_every_failed_api_step_removes_private_files_and_stops_service(self):
        for count in range(1, 7):
            with self.subTest(step=count):
                root, result = self.run_seed(str(count))
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((root / "private").exists())
                self.assertIn("stop", (root / "service-events").read_text())
                self.assertIn('bootstrap_secret="temporary"', (root / "config").read_text())
                self.assertNotIn("sensitive fixture", result.stdout + result.stderr)

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


if __name__ == "__main__":
    unittest.main()
