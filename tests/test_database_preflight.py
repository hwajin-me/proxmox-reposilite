"""Optional JDBC checks using the official application JAR, without a server.

Set REPOSILITE_TEST_JAR to a downloaded Reposilite all-JAR to run these checks.
REPOSILITE_TEST_JAVA can select a full JDK's java executable.
"""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "misc/DatabasePreflight.java"
INSTALLER = (SOURCE.parents[1] / "install/reposilite-install.sh").read_text()
PREFLIGHT = '  if [[ "$database_type" != sqlite ]]; then' + INSTALLER.split(
    '  if [[ "$database_type" != sqlite ]]; then', 1
)[1].split('  msg_info "Configuring Reposilite"', 1)[0]
JAR = os.environ.get("REPOSILITE_TEST_JAR")
if JAR:
    JAR = str(Path(JAR).resolve())
JAVA = os.environ.get("REPOSILITE_TEST_JAVA") or shutil.which("java")


class InstallerPreflightTests(unittest.TestCase):
    def test_driver_output_is_suppressed_and_exit_status_is_preserved(self):
        with tempfile.TemporaryDirectory(prefix="reposilite-driver-output-") as temporary:
            root = Path(temporary)
            (root / "database.env").write_text("")
            stubs = r'''
set -eo pipefail
database_type=mariadb
integration_java_opts=''
download_file() { :; }
msg_info() { :; }
msg_ok() { :; }
msg_error() { echo "$*" >&2; }
timeout() { shift; "$@"; }
java() {
  echo PRIVATE_DRIVER_CONNECTION_INFORMATION
  echo PRIVATE_DRIVER_CONNECTION_INFORMATION >&2
  return "$JDBC_EXIT"
}
'''
            script = stubs + PREFLIGHT.replace("/etc/reposilite", str(root))
            for status in (0, 1):
                with self.subTest(status=status):
                    result = subprocess.run(
                        ["bash", "-c", script], capture_output=True, text=True,
                        env={**os.environ, "JDBC_EXIT": str(status)},
                    )
                    self.assertEqual(result.returncode, status)
                    self.assertNotIn("PRIVATE_DRIVER", result.stdout + result.stderr)
                    if status:
                        self.assertIn("requires an empty, reachable external database", result.stderr)


@unittest.skipUnless(JAR and JAVA, "Set REPOSILITE_TEST_JAR and provide a full JDK to test JDBC preflight")
class DatabasePreflightTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="reposilite-database-preflight-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / "application.db"
        with sqlite3.connect(self.database):
            pass
        self.database.chmod(0o600)

    def run_check(self, url=None, driver="org.sqlite.JDBC", content=None):
        configuration = self.root / "database-check.json"
        configuration.write_text(
            content if content is not None else json.dumps({
                "url": url or f"jdbc:sqlite:{self.database}",
                "driver": driver,
            })
        )
        configuration.chmod(0o600)
        return subprocess.run(
            [JAVA, "--enable-native-access=ALL-UNNAMED", "-cp", JAR, str(SOURCE), str(configuration)],
            capture_output=True,
            text=True,
            timeout=45,
            cwd=self.root,
        )

    def test_empty_database_is_accepted_without_creating_tables(self):
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout + result.stderr, "")
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(connection.execute("SELECT name FROM sqlite_master").fetchall(), [])

    def test_existing_table_is_refused_without_changing_schema_or_data(self):
        with sqlite3.connect(self.database) as connection:
            connection.execute("CREATE TABLE private_existing_data (value TEXT)")
            connection.execute("INSERT INTO private_existing_data VALUES ('preserve-me')")
        result = self.run_check()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("Database preflight failed", result.stderr)
        self.assertNotIn("private_existing_data", result.stderr)
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(
                connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall(),
                [("private_existing_data",)],
            )
            self.assertEqual(
                connection.execute("SELECT value FROM private_existing_data").fetchall(),
                [("preserve-me",)],
            )

    def test_invalid_connection_does_not_disclose_url_or_exception(self):
        secret = "PRIVATE_CONNECTION_PASSWORD"
        url = f"jdbc:unsupported://private-host/database?password={secret}"
        result = self.run_check(url=url)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertNotIn(secret, result.stderr)
        self.assertNotIn(url, result.stderr)
        self.assertNotIn("SQLException", result.stderr)
        self.assertNotIn("at DatabasePreflight", result.stderr)

    def test_existing_view_is_refused_without_modifying_it(self):
        with sqlite3.connect(self.database) as connection:
            connection.execute("CREATE VIEW private_existing_view AS SELECT 'preserve-me' AS value")
        result = self.run_check()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("Database preflight failed", result.stderr)
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(connection.execute("SELECT value FROM private_existing_view").fetchall(), [("preserve-me",)])

    def test_invalid_configuration_does_not_disclose_parser_input(self):
        secret = "PRIVATE_MALFORMED_CONFIGURATION"
        result = self.run_check(content='{"url": "' + secret)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertNotIn(secret, result.stderr)
        self.assertNotIn("Exception", result.stderr)


if __name__ == "__main__":
    unittest.main()
