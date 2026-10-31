"""Validate secret handling, native configuration and connection boundaries."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("config", ROOT / "misc/reposilite-config.py")
CONFIG = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONFIG)

DB = {"type": "mariadb", "host": "db.example.com", "name": "reposilite", "user": "maven", "password": 'a $special&?"password', "parameters": {"sslMode": "verify-full"}}
S3 = {"type": "s3", "bucketName": "maven-artifacts", "region": "auto", "accessKey": "access", "secretKey": "secret", "signer": "LEGACY_V4"}
LDAP = {"enabled": True, "hostname": "ldap.example.com", "ssl": True, "baseDn": "dc=example,dc=com", "searchUserDn": "cn=reader,dc=example,dc=com", "searchUserPassword": 'secret "with spaces"'}


class IntegrationTests(unittest.TestCase):
    def root(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        return Path(temp.name)

    def test_database_password_and_driver_parameters_round_trip(self):
        root = self.root()
        CONFIG.render({"database": DB}, root)
        config = root / "etc/reposilite/configuration.cdn"
        jdbc = json.loads(config.read_text().split(": ", 1)[1])
        query = parse_qs(urlsplit(jdbc.removeprefix("jdbc:")).query)
        self.assertEqual(query["sslMode"], ["verify-full"])
        self.assertEqual(query["credentialType"], ["ENV"])
        self.assertNotIn("password", query)
        credentials = root / "etc/reposilite/database.env"
        result = subprocess.run(["bash", "-c", 'source "$1"; printf "%s" "$MARIADB_PWD"', "test", str(credentials)], capture_output=True, text=True)
        self.assertEqual(result.stdout, DB["password"])
        self.assertEqual(credentials.stat().st_mode & 0o777, 0o600)
        self.assertEqual(config.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(DB["password"], (root / "etc/reposilite/runtime.conf").read_text())

    def test_other_jdbc_drivers_encode_credentials(self):
        for dialect in ("mysql", "postgresql"):
            root = self.root()
            CONFIG.render({"database": {**DB, "type": dialect}}, root)
            jdbc = json.loads((root / "etc/reposilite/configuration.cdn").read_text().split(": ", 1)[1])
            query = parse_qs(urlsplit(jdbc.removeprefix("jdbc:")).query)
            self.assertEqual(query["password"], [DB["password"]])

    def test_ldap_and_s3_use_native_field_names_and_isolate_repositories(self):
        root = self.root()
        CONFIG.render({"storage": S3, "ldap": LDAP}, root)
        folder = root / "etc/reposilite"
        provider = json.loads((folder / "storage.json").read_text())["provider"]
        self.assertEqual(provider["type"], "s3")
        self.assertTrue(provider["sharedBucket"])
        ldap = json.loads((folder / "ldap.json").read_text())
        self.assertEqual(ldap["searchUserPassword"], LDAP["searchUserPassword"])
        self.assertEqual(ldap["port"], 636)
        self.assertEqual(ldap["userType"], "TEMPORARY")
        self.assertEqual(folder.stat().st_mode & 0o777, 0o700)

    def test_bad_profile_types_and_injected_paths_fail(self):
        for profile in [
            {"oidc": {}}, {"proxy": {}}, {"database": {"type": "oracle"}},
            {"database": {**DB, "port": "3306"}}, {"database": {**DB, "host": "db;touch /tmp/no"}},
            {"database": {**DB, "parameters": {"password": "override"}}},
            {"database": {"type": "sqlite", "name": "../../etc/passwd"}},
            {"storage": {**S3, "pathStyle": "yes"}},
            {"storage": {**S3, "endpoint": "https://user:secret@s3.example.com"}},
            {"storage": {**S3, "prefix": "../other"}},
            {"storage": {"type": "fs", "root": "/etc"}},
            {"storage": {"type": "fs", "root": "/srv/a/../b"}},
            {"ldap": {**LDAP, "ssl": "false"}}, {"ldap": {**LDAP, "userType": "ADMIN"}},
            {"tls": {"mode": "provided", "hostname": "repo.example.com"}},
            {"tls": {"mode": "self-signed", "hostname": "repo.example.com", "port": 8080}},
        ]:
            with self.subTest(profile=profile), self.assertRaises(ValueError):
                CONFIG.validate(profile)

    def test_absent_external_mount_fails_instead_of_using_root_disk(self):
        with patch.object(CONFIG.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "mountpoint")):
            with self.assertRaises(subprocess.CalledProcessError):
                CONFIG.render({"storage": {"type": "fs", "root": "/mnt/artifacts"}}, self.root())

    def test_self_signed_native_tls_and_wrong_hostname_rejection(self):
        root = self.root()
        CONFIG.render({"tls": {"mode": "self-signed", "hostname": "repo.example.com"}}, root)
        folder = root / "etc/reposilite"
        self.assertIn("enforceSsl: true", (folder / "configuration.cdn").read_text())
        profile = {"tls": {"mode": "provided", "hostname": "wrong.example.com", "certificate": (folder / "tls/certificate.pem").read_text(), "privateKey": (folder / "tls/private-key.pem").read_text()}}
        with self.assertRaisesRegex(ValueError, "hostname"):
            CONFIG.render(profile, self.root())
        profile["tls"]["hostname"] = "repo.example.com"
        CONFIG.render(profile, self.root())

    def test_profile_permissions_and_errors_do_not_disclose_credentials(self):
        root = self.root()
        file = root / "profile.json"
        file.write_text(json.dumps({"database": DB}))
        file.chmod(0o644)
        result = subprocess.run(["python3", str(ROOT / "misc/reposilite-config.py"), "validate", "--file", str(file)], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(DB["password"], result.stdout + result.stderr)
        file.chmod(0o600)
        result = subprocess.run(["python3", str(ROOT / "misc/reposilite-config.py"), "validate", "--file", str(file)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
