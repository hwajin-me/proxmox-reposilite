"""Exercise pre-creation validation without creating a container."""
import json
import os
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
CT = (ROOT / "ct/reposilite.sh").read_text()
SETTINGS = CT.split("\nstart\n", 1)[1].split("\nbuild_container\n", 1)[0]
HARNESS = """
msg_error() { echo "$*" >&2; }
ensure_dependencies() { command -v jq >/dev/null; }
prompt_confirm() { return 0; }
prompt_input() { printf '%s' "${2}"; }
""" + SETTINGS + "\nenv -0\n"


class SettingsTests(unittest.TestCase):
    def run_settings(self, **values):
        environment = {k: v for k, v in os.environ.items() if not k.startswith("var_")}
        environment.update(values)
        result = subprocess.run(["bash", "-c", HARNESS], env=environment, capture_output=True)
        exported = {}
        if result.returncode == 0:
            exported = dict(item.decode().split("=", 1) for item in result.stdout.split(b"\0") if b"=" in item)
        return result, exported

    def test_defaults_match_catalog(self):
        result, values = self.run_settings()
        self.assertEqual(result.returncode, 0, result.stderr)
        catalog = json.loads((ROOT / "json/reposilite.json").read_text())
        for field in catalog["app_vars"]:
            self.assertIn(field["name"], values)
            if "default" in field:
                self.assertEqual(values[field["name"]], str(field["default"]))

    def test_custom_values_and_shell_metacharacters_are_preserved(self):
        result, values = self.run_settings(
            METHOD="advanced", var_port="9091", var_data_dir="/srv/maven",
            var_admin_user="maintainer", var_dashboard_title='Team "Maven" $(false)',
            var_public_repositories="stable,nightly", var_private_repositories="internal",
            var_repository_quota="80", var_redeployment="yes", var_preserve_snapshots="yes",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(values["var_port"], "9091")
        self.assertEqual(values["var_dashboard_title"], 'Team "Maven" $(false)')
        self.assertEqual(values["var_private_repositories"], "internal")

    def test_advanced_prompt_only_fills_missing_values(self):
        result, values = self.run_settings(METHOD="advanced", var_port="9092")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(values["var_port"], "9092")
        self.assertEqual(values["var_public_repositories"], "releases,snapshots")

    def test_invalid_values_fail_before_creation(self):
        for field, value in [
            ("var_port", "0"), ("var_port", "65536"), ("var_port", "8080;false"),
            ("var_java_heap", "32"), ("var_java_heap", "99999"),
            ("var_data_dir", "/opt/reposilite"), ("var_data_dir", "/srv/../etc"),
            ("var_data_dir", "/srv/a;false"), ("var_admin_user", "admin:secret"),
            ("var_repository_quota", "101"), ("var_repository_quota", "0"),
            ("var_redeployment", "true"), ("var_preserve_snapshots", "maybe"),
            ("var_public_repositories", "public,,other"), ("var_public_repositories", "releases,"),
            ("var_public_repositories", "public/../../etc"),
            ("var_public_repositories", "private"),
        ]:
            with self.subTest(field=field, value=value):
                result, _ = self.run_settings(**{field: value})
                self.assertNotEqual(result.returncode, 0)

    def test_at_least_one_repository_and_no_duplicates(self):
        for public, private in [("-", "-"), ("a,a", "-"), ("a", "a")]:
            result, _ = self.run_settings(var_public_repositories=public, var_private_repositories=private)
            self.assertNotEqual(result.returncode, 0)
        result, _ = self.run_settings(var_public_repositories="-", var_private_repositories="internal")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_json_override_accepts_per_repository_settings(self):
        custom = [{"id": "maven", "visibility": "PRIVATE", "storageProvider": {"type": "fs", "quota": "25%", "mount": ""}, "redeployment": True, "proxied": []}]
        result, values = self.run_settings(var_repositories_json=json.dumps(custom))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(values["var_repositories_json"]), custom)

    def test_invalid_json_overrides_are_rejected(self):
        repository = {"id": "maven", "visibility": "PUBLIC", "storageProvider": {"type": "fs"}}
        for value in ["{", "{}", "[]", '[{"id":"bad"}]', json.dumps([repository, repository]), json.dumps([{**repository, "id": "../etc"}])]:
            with self.subTest(value=value):
                result, _ = self.run_settings(var_repositories_json=value)
                self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
