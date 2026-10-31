"""Optional logging regression using the official Reposilite 3.6.5 all-JAR.

Set REPOSILITE_TEST_JAR and optionally REPOSILITE_TEST_JAVA (a full JDK 21+).
The Java probe and all logger files stay in a private temporary /tmp directory.
"""
import gzip
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
WRITER_FILTERS = (
    "-Dtinylog.writerFile.level=info",
    "-Dtinylog.writerJournalist.level=info",
)
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
APPLICATION_PROBE = r'''
package com.reposilite.configuration;

import com.reposilite.journalist.Channel;
import com.reposilite.journalist.backend.PrintStreamLogger;
import com.reposilite.journalist.tinylog.TinyLogLogger;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

class ConfigurationLoggingProbe {
    public static void main(String[] args) {
        // Attach the bundled Journalist console consumer, without replacing
        // tinylog's writers or adding a second severity threshold.
        TinyLogLogger console = new TinyLogLogger(
            Channel.ALL, new PrintStreamLogger(System.out, System.err));
        Logger configuration = LoggerFactory.getLogger(
            "com.reposilite.configuration.SharedConfigurationFacade");
        configuration.debug(args[0]);
        configuration.info(args[1]);
        configuration.warn(args[2]);
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

    def run_java_probe(self, name, probe, labels, filters):
        with tempfile.TemporaryDirectory(prefix="reposilite-sql-logging-", dir="/tmp") as temporary:
            root = Path(temporary)
            source = root / (name + ".java")
            source.write_text(probe)
            source.chmod(0o600)
            suffix = uuid.uuid4().hex
            sentinels = tuple(label + suffix for label in labels)
            result = subprocess.run(
                [JAVA, *filters, "-cp", JAR, str(source), *sentinels],
                cwd=root, env=self.environment, capture_output=True, text=True,
                timeout=30, umask=0o077,
            )
            self.assertEqual(result.returncode, 0, "Java logging probe failed")
            # The bundled configuration writes to both Journalist and files.
            # Do not replace its writers, or merely asserting console silence
            # could miss a disclosure in the rolling log. Read the actual
            # rolling file, without counting its latest.log alias twice.
            rolling_files = sorted(
                path for path in (root / "logs").glob("*")
                if path.name.endswith((".txt", ".txt.gz"))
            )
            self.assertTrue(rolling_files, "Bundled rolling writer produced no log file")
            rolling_output = ""
            for path in rolling_files:
                if path.suffix == ".gz":
                    with gzip.open(path, "rt") as logfile:
                        rolling_output += logfile.read()
                else:
                    rolling_output += path.read_text()
            outputs = {
                "consoleJournalist": result.stdout + result.stderr,
                "rollingFile": rolling_output,
            }
            return sentinels, outputs

    def run_probe(self, filtered):
        sentinels, outputs = self.run_java_probe(
            "SqlLoggingProbe", PROBE,
            ("SQL_DEBUG_FIXTURE_", "SQL_WARN_FIXTURE_"),
            (FILTER,) if filtered else (),
        )
        return sentinels, "".join(outputs.values())

    def assert_application_levels(self, filters, expected_counts):
        sentinels, outputs = self.run_java_probe(
            "ConfigurationLoggingProbe", APPLICATION_PROBE,
            ("APP_DEBUG_FIXTURE_", "APP_INFO_FIXTURE_", "APP_WARN_FIXTURE_"),
            filters,
        )
        for writer, output in outputs.items():
            with self.subTest(writer=writer):
                self.assertEqual(
                    tuple(output.count(sentinel) for sentinel in sentinels),
                    expected_counts,
                    "Application DEBUG/INFO/WARN counts differ for " + writer,
                )

    def test_upstream_default_emits_exposed_debug_and_warning(self):
        (debug, warning), output = self.run_probe(filtered=False)
        self.assertIn(debug, output)
        self.assertIn(warning, output)

    def test_exposed_override_suppresses_debug_and_preserves_warning(self):
        (debug, warning), output = self.run_probe(filtered=True)
        self.assertNotIn(debug, output)
        self.assertIn(warning, output)

    def test_upstream_default_emits_application_debug_info_warning_in_each_writer(self):
        self.assert_application_levels((), (1, 1, 1))

    def test_exposed_only_override_still_emits_application_debug_in_each_writer(self):
        self.assert_application_levels((FILTER,), (1, 1, 1))

    def test_all_overrides_suppress_application_debug_and_preserve_info_warning_in_each_writer(self):
        self.assert_application_levels((FILTER, *WRITER_FILTERS), (0, 1, 1))


if __name__ == "__main__":
    unittest.main()
