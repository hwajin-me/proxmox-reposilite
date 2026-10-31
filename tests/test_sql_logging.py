"""Optional logging regression using the official Reposilite 3.6.5 all-JAR.

Set REPOSILITE_TEST_JAR and optionally REPOSILITE_TEST_JAVA (a full JDK 21+).
The Java probe and all logger files stay in a private temporary /tmp directory.
"""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
import uuid


JAR = os.environ.get("REPOSILITE_TEST_JAR")
if JAR:
    JAR = str(Path(JAR).resolve())
JAVA = shutil.which(os.environ.get("REPOSILITE_TEST_JAVA") or "java")
FILTER = "-Dtinylog.level@org.jetbrains.exposed=warn"
# tinylog applies package thresholds to the emitting call site, rather than
# merely the SLF4J logger's name. Match the real Exposed SQL caller's package.
PROBE = r'''
package org.jetbrains.exposed.v1.core;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

class SqlLoggingProbe {
    public static void main(String[] args) {
        Logger sql = LoggerFactory.getLogger(
            "org.jetbrains.exposed.v1.core.Slf4jSqlDebugLogger");
        sql.debug(args[0]);
        sql.warn(args[1]);
    }
}
'''


@unittest.skipUnless(JAR and JAVA, "Set REPOSILITE_TEST_JAR and provide a full JDK 21+")
class SqlLoggingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not Path(JAR).is_file():
            raise unittest.SkipTest("Reposilite all-JAR fixture is unavailable")
        cls.environment = {key: value for key, value in os.environ.items()
                           if key not in ("JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS")}
        try:
            modules = subprocess.run(
                [JAVA, "--list-modules"], env=cls.environment,
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise unittest.SkipTest("A working full JDK 21+ is required") from None
        compiler = re.search(r"(?m)^jdk\.compiler@(\d+)", modules.stdout)
        if modules.returncode or not compiler or int(compiler.group(1)) < 21:
            raise unittest.SkipTest("Java source-file launch requires a full JDK 21+")

    def run_probe(self, filtered):
        with tempfile.TemporaryDirectory(prefix="reposilite-sql-logging-", dir="/tmp") as temporary:
            root = Path(temporary)
            source = root / "SqlLoggingProbe.java"
            source.write_text(PROBE)
            source.chmod(0o600)
            suffix = uuid.uuid4().hex
            sentinels = tuple(label + suffix for label in ("SQL_DEBUG_FIXTURE_", "SQL_WARN_FIXTURE_"))
            result = subprocess.run(
                [JAVA, *([FILTER] if filtered else []), "-cp", JAR, str(source), *sentinels],
                cwd=root, env=self.environment, capture_output=True, text=True,
                timeout=30, umask=0o077,
            )
            self.assertEqual(result.returncode, 0, "Java logging probe failed")
            # The bundled configuration writes to both Journalist and files.
            # Do not replace its writers, or merely asserting console silence
            # could miss the original SQL disclosure in the rolling log.
            output = result.stdout + result.stderr
            for path in root.rglob("*"):
                if path.is_file() and path.suffix in (".log", ".txt"):
                    output += path.read_text()
            return sentinels, output

    def test_upstream_default_emits_exposed_debug_and_warning(self):
        (debug, warning), output = self.run_probe(filtered=False)
        self.assertIn(debug, output)
        self.assertIn(warning, output)

    def test_exposed_override_suppresses_debug_and_preserves_warning(self):
        (debug, warning), output = self.run_probe(filtered=True)
        self.assertNotIn(debug, output)
        self.assertIn(warning, output)


if __name__ == "__main__":
    unittest.main()
