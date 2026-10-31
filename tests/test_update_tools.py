"""Read-only contracts for the pinned official updaters and standalone adapter."""
import contextlib
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("update_apps_adapter", ROOT / "misc/update-apps-adapter.py")
ADAPTER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ADAPTER)
APPS_FIXTURE = os.environ.get("REPOSILITE_UPDATE_APPS_FIXTURE")
LXCS_FIXTURE = os.environ.get("REPOSILITE_UPDATE_LXCS_FIXTURE")
LXCS_SHA256 = "b448501017c3d18eb80adbd841e261772abfd865238bcaa0b3c41c8f5c1bf622"


def function_source(source, name):
    match = re.search(r"(?ms)^(?:function )?" + re.escape(name) + r"\(\) \{\n.*?^\}", source)
    if not match:
        raise AssertionError("Function not found: " + name)
    return match.group()


class AdapterSafetyTests(unittest.TestCase):
    def test_help_does_not_access_host_or_network(self):
        result = subprocess.run(["bash", str(ROOT / "tools/pve/update-apps.sh"), "--help"],
                                text=True, capture_output=True, env={"PATH": "/usr/bin:/bin"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Standalone Reposilite adapter", result.stdout)
        self.assertIn("--render", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_invalid_upstream_hash_is_rejected(self):
        with self.assertRaisesRegex(ADAPTER.AdapterError, "SHA256 mismatch"):
            ADAPTER.render_source(b"#!/bin/bash\necho changed\n")

    def test_wrapper_default_rejects_non_pve_before_downloading(self):
        result = subprocess.run(["/bin/bash", str(ROOT / "tools/pve/update-apps.sh")],
                                text=True, capture_output=True, env={"PATH": "/nonexistent"})
        self.assertEqual(result.returncode, 1)
        self.assertIn("Proxmox VE", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_direct_helper_execution_requires_pve_before_downloading(self):
        with patch.object(ADAPTER.os, "geteuid", return_value=1000), \
                patch.object(ADAPTER, "load_upstream") as download:
            with self.assertRaisesRegex(ADAPTER.AdapterError, "Proxmox VE host"):
                ADAPTER.main([])
        download.assert_not_called()

    def test_invalid_roots_are_rejected(self):
        for value in ("http://example.test/repo", "https://user:password@example.test/repo",
                      "https://example.test/repo?token=private", "https://example.test/repo#fragment",
                      "https://example.test/a/../repo", "https://example.test/a/%2e%2e/repo",
                      "https://example.test/$(false)", "https://example.test/repo\n"):
            with self.subTest(value=value), self.assertRaises(ADAPTER.AdapterError):
                ADAPTER.validate_root(value)

    def test_source_selection_preserves_mixed_fleet_and_legacy(self):
        script = ADAPTER.ADAPTER_FUNCTION + r'''
for slug in reposilite alpine-reposilite adguard alpine-openbao; do
  reposilite_adapter_script_url "$slug"
  printf '\n'
done
'''
        result = subprocess.run(["bash", "-c", script], text=True, capture_output=True,
                                env={**os.environ, "REPOSILITE_SCRIPTS_URL": "https://example.test/fork/branch/"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [
            "https://example.test/fork/branch/ct/reposilite.sh",
            "https://example.test/fork/branch/ct/reposilite.sh",
            ADAPTER.OFFICIAL_ROOT + "/ct/adguard.sh",
            ADAPTER.OFFICIAL_ROOT + "/ct/alpine-openbao.sh",
        ])


@unittest.skipUnless(APPS_FIXTURE, "Set REPOSILITE_UPDATE_APPS_FIXTURE to the pinned official script")
class OfficialAppsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = Path(APPS_FIXTURE).read_bytes()
        if hashlib.sha256(cls.payload).hexdigest() != ADAPTER.UPSTREAM_SHA256:
            raise AssertionError("Unexpected official update-apps fixture SHA256")
        cls.original = cls.payload.decode()
        cls.rendered = ADAPTER.render_source(cls.payload)

    def test_exactly_two_url_references_change_and_header_is_preserved(self):
        expected = self.original
        for old, new in ADAPTER.REPLACEMENTS.items():
            self.assertEqual(expected.count(old), 1)
            expected = expected.replace(old, new, 1)
        expected = expected.replace(ADAPTER.ANCHOR, ADAPTER.ADAPTER_FUNCTION + ADAPTER.ANCHOR, 1)
        self.assertEqual(self.rendered, expected)
        self.assertTrue(self.rendered.startswith(self.original.split(ADAPTER.ANCHOR, 1)[0]))

    def test_context_drift_is_rejected_independently_of_hash(self):
        for old, replacement in ((ADAPTER.ANCHOR, "\nchanged_anchor=1\n"),
                                 (next(iter(ADAPTER.REPLACEMENTS)), '"https://example.test/ct/${name}.sh"')):
            payload = self.original.replace(old, replacement, 1).encode()
            with self.subTest(old=old), patch.object(ADAPTER, "UPSTREAM_SHA256", hashlib.sha256(payload).hexdigest()):
                with self.assertRaisesRegex(ADAPTER.AdapterError, "context changed"):
                    ADAPTER.render_source(payload)

    def test_actual_upstream_lookup_functions_use_per_service_roots(self):
        declarations = ADAPTER.ADAPTER_FUNCTION + "\n" + "\n".join(
            function_source(self.rendered, name)
            for name in ("sanitize_service_name", "script_exists", "resolve_service_script"))
        with tempfile.TemporaryDirectory() as folder:
            requests = Path(folder) / "requests"
            script = declarations + r'''
curl() { printf '%s\n' "${@: -1}" >>"$REQUESTS"; }
for slug in reposilite alpine-reposilite adguard; do
  resolve_service_script "$slug"
  printf '\n'
done
'''
            result = subprocess.run(["bash", "-c", script], text=True, capture_output=True,
                                    env={**os.environ, "REQUESTS": str(requests),
                                         "REPOSILITE_SCRIPTS_URL": "https://example.test/standalone/main"})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), ["reposilite", "alpine-reposilite", "adguard"])
            self.assertEqual(requests.read_text().splitlines(), [
                "https://example.test/standalone/main/ct/reposilite.sh",
                "https://example.test/standalone/main/ct/reposilite.sh",
                ADAPTER.OFFICIAL_ROOT + "/ct/adguard.sh",
            ])

    def test_legacy_metadata_uses_canonical_release_parser(self):
        metadata_fetch = next(line for line in self.rendered.splitlines()
                              if line.startswith('  script=$(curl -fsSL '))
        script = ADAPTER.ADAPTER_FUNCTION + '\n' + function_source(self.rendered, "dry_run_container") + r'''
curl() {
  local url="${@: -1}"
  case "$url" in
    https://example.test/standalone/main/ct/reposilite.sh) cat "$CANONICAL_CT" ;;
    https://api.github.com/repos/dzikoysk/reposilite/releases/latest) printf '{"tag_name":"3.6.5"}\n' ;;
    *) printf 'Unexpected request\n' >&2; return 22 ;;
  esac
}
pct() { printf '3.6.4\n'; }
service=alpine-reposilite
''' + metadata_fetch + '\ndry_run_container 101 "$service"\nprintf \'RESULT %s\\n\' "$DRY_RUN_RESULT"\n'
        result = subprocess.run(["bash", "-c", script], text=True, capture_output=True,
                                env={**os.environ, "CANONICAL_CT": str(ROOT / "ct/reposilite.sh"),
                                     "REPOSILITE_SCRIPTS_URL": "https://example.test/standalone/main",
                                     "YW": "", "GN": "", "BL": "", "CL": ""})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Container 101 (alpine-reposilite): update available", result.stdout)
        self.assertIn("3.6.4 → 3.6.5", result.stdout)
        self.assertNotIn("skipping", result.stdout)

    def test_render_only_prints_source_and_never_executes(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(ADAPTER, "load_upstream", return_value=self.payload), \
                patch.object(ADAPTER, "verify_host") as host_check, \
                patch.object(ADAPTER.os, "execvpe") as execute, \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(ADAPTER.main(["--render"]), 0)
        execute.assert_not_called()
        host_check.assert_not_called()
        self.assertEqual(stdout.getvalue(), self.rendered)
        self.assertIn("Standalone Reposilite adapter", stderr.getvalue())

    def test_arguments_and_environment_survive_adapter(self):
        with patch.dict(os.environ, {"var_container": "101,102", "var_unattended": "yes",
                                    "PHS_SILENT": "1", "REPOSILITE_SCRIPTS_URL": ADAPTER.DEFAULT_ROOT}), \
                patch.object(ADAPTER, "load_upstream", return_value=self.payload), \
                patch.object(ADAPTER, "verify_host"), \
                patch.object(ADAPTER.os, "execvpe") as execute, contextlib.redirect_stderr(io.StringIO()):
            ADAPTER.main(["--export-config"])
        command, argv, environment = execute.call_args.args
        self.assertEqual(command, "bash")
        self.assertEqual(argv, ["bash", "-c", self.rendered, "update-apps-standalone", "--export-config"])
        self.assertEqual(environment["var_container"], "101,102")
        self.assertEqual(environment["var_unattended"], "yes")
        self.assertEqual(environment["PHS_SILENT"], "1")

    def test_actual_unattended_alpine_command_is_preserved(self):
        selection = self.rendered.split('UPDATE_CMD="update;"', 1)[1].split('\ncontainers_needing_reboot=()', 1)[0]
        branch = re.search(r'(?ms)^  case "\$os" in\n.*?^  esac', self.rendered).group()
        script = 'UPDATE_CMD="update;"' + selection + '\nos=alpine\ncontainer=101\n'
        setup = next(line for line in self.rendered.splitlines() if line.startswith('  SETUP_CMD='))
        script += setup + '\npct() { printf \'%s\\0\' "$@"; }\n' + branch
        result = subprocess.run(["bash", "-c", script], capture_output=True,
                                env={**os.environ, "UNATTENDED_UPDATE": "yes"})
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = result.stdout.decode().split("\0")[:-1]
        self.assertEqual(arguments[:5], ["exec", "101", "--", "ash", "-c"])
        self.assertIn("mkdir -p /tmp/.nc;", arguments[5])
        self.assertIn("export PATH=/tmp/.nc:$PATH; export TERM=dumb;", arguments[5])
        self.assertTrue(arguments[5].endswith("export PHS_SILENT=1;update;"))


@unittest.skipUnless(LXCS_FIXTURE, "Set REPOSILITE_UPDATE_LXCS_FIXTURE to the pinned official script")
class OfficialLxcsTests(unittest.TestCase):
    def test_actual_alpine_branch_updates_packages_only(self):
        payload = Path(LXCS_FIXTURE).read_bytes()
        self.assertEqual(hashlib.sha256(payload).hexdigest(), LXCS_SHA256)
        declaration = function_source(payload.decode(), "update_container")
        with tempfile.TemporaryDirectory() as folder:
            requests = Path(folder) / "requests"
            script = r'''
header_info() { :; }
pct() {
  printf '%s\0' "$@" >>"$REQUESTS"
  printf '\n' >>"$REQUESTS"
  if [[ "$1" == config ]]; then printf 'ostype: alpine\n';
  elif [[ "$*" == 'exec 101 hostname' ]]; then printf 'fixture\n'; fi
}
''' + declaration + '\nupdate_container 101\n'
            result = subprocess.run(["bash", "-c", script], text=True, capture_output=True,
                                    env={**os.environ, "REQUESTS": str(requests), "BL": "", "GN": "", "CL": "", "YW": ""})
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = [line.split("\0")[:-1] for line in requests.read_text().splitlines()]
            self.assertIn(["exec", "101", "--", "ash", "-c", "apk -U upgrade"], calls)
            self.assertFalse(any("update;" in argument or argument == "/usr/bin/update"
                                 for call in calls for argument in call))


if __name__ == "__main__":
    unittest.main()
