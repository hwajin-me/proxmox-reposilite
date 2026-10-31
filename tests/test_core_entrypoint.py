"""Exercise the real Core-generated entrypoint/helper without a host or TTY."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

CORE = os.environ.get("REPOSILITE_TEST_CORE_ENTRYPOINT")
HELPER = os.environ.get("REPOSILITE_TEST_CORE_UPDATE")
SCRIPT_ROOT = "https://scripts.example.test/distribution/main"
CORE_ROOT = "https://core.example.test/engine"
CURL = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
url = next((a for a in args if a.startswith("https://")), "")
with open(os.environ["FIXTURE_REQUESTS"], "a") as log:
    log.write(url + "\n")
if url.endswith("/misc/update.sh"):
    if os.environ.get("MISSING_HELPER") == "1":
        sys.exit(22)
    sys.stdout.write(pathlib.Path(os.environ["FIXTURE_HELPER"]).read_text())
elif "/api/update-info?" in url:
    print(json.dumps({"found": False, "state": "unknown"}))
elif url.endswith("/ct/reposilite.sh"):
    if os.environ.get("MISSING_APP") == "1":
        sys.exit(22)
    if "-o" not in args:
        print('printf "APP_CALLED:%s:%s\\n" "${PHS_SILENT:-}" "$COMMUNITY_SCRIPTS_URL"')
        print('exit "${APP_EXIT:-0}"')
else:
    sys.exit(22)
'''


@unittest.skipUnless(CORE and HELPER, "Provide pinned Core entrypoint/helper fixtures")
class CoreEntrypointTests(unittest.TestCase):
    def run_entrypoint(self, name="reposilite", **options):
        temporary = tempfile.TemporaryDirectory(prefix="reposilite-core-entrypoint-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        binary = root / "bin"
        binary.mkdir(mode=0o700)
        (binary / "curl").write_text(CURL)
        (binary / "curl").chmod(0o700)
        source = Path(CORE).read_text()
        generator = "write_update_entrypoint() {" + source.split("write_update_entrypoint() {", 1)[1].split("\n}\n", 1)[0] + "\n}\n"
        generator = generator.replace("/usr/bin/update", str(binary / "update")).replace("/usr/bin/.update", str(binary / ".update"))
        setup = 'set -eo pipefail\n_cs_live_base() { printf "%s" "$1"; }\n'
        requests = root / "requests"
        environment = {**os.environ, "PATH": str(binary) + os.pathsep + os.environ["PATH"],
                       "FIXTURE_REQUESTS": str(requests), "FIXTURE_HELPER": str(Path(HELPER).resolve()),
                       "COMMUNITY_SCRIPTS_CORE_URL": CORE_ROOT,
                       "COMMUNITY_SCRIPTS_WEBSITE_URL": "https://website.example.test",
                       "PHS_SILENT": "1", "TERM": "dumb", **options}
        generated = subprocess.run(
            ["bash", "-c", setup + generator + '\nwrite_update_entrypoint reposilite "$1" "$2"', "test", name, SCRIPT_ROOT],
            env=environment, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(generated.returncode, 0, generated.stderr)
        result = subprocess.run([str(binary / "update")], env=environment, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=10)
        return result, requests.read_text().splitlines()

    def test_unknown_catalog_slug_preserves_the_script_root_and_silent_mode(self):
        result, requests = self.run_entrypoint()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"APP_CALLED:1:{SCRIPT_ROOT}", result.stdout)
        self.assertIn(CORE_ROOT + "/misc/update.sh", requests)
        self.assertIn(SCRIPT_ROOT + "/ct/reposilite.sh", requests)
        self.assertFalse(any("/ProxmoxVE/" in url for url in requests))

    def test_legacy_resolution_keeps_application_failure_status(self):
        result, requests = self.run_entrypoint("alpine-reposilite", APP_EXIT="6")
        self.assertEqual(result.returncode, 6, result.stderr)
        self.assertIn(f"APP_CALLED:1:{SCRIPT_ROOT}", result.stdout)
        self.assertIn(SCRIPT_ROOT + "/ct/alpine-reposilite.sh", requests)

    def test_missing_app_fails_and_unavailable_helper_uses_the_recorded_root(self):
        result, _ = self.run_entrypoint(MISSING_APP="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("APP_CALLED", result.stdout)
        result, requests = self.run_entrypoint(MISSING_HELPER="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"APP_CALLED:1:{SCRIPT_ROOT}", result.stdout)
        self.assertIn(SCRIPT_ROOT + "/ct/reposilite.sh", requests)


if __name__ == "__main__":
    unittest.main()
