"""Recovery guard checks with every service path isolated in a temporary tree."""
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "misc/reposilite-update-guard.sh"
BLOCKED_MESSAGE = "Reposilite update recovery is required"


class GuardFixture:
    def make_fixture(self, stub_commands=True):
        temporary = tempfile.TemporaryDirectory(prefix="reposilite-recovery-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.configuration = self.root / "etc/reposilite"
        self.configuration.mkdir(parents=True)
        self.run = self.root / "run"
        self.run.mkdir()
        self.marker = self.configuration / "update-in-progress"
        self.authorization = self.run / "reposilite-update.authorized"
        self.lock = self.run / "reposilite-update.lock"
        self.guard = self.root / "guard.sh"
        source = SOURCE.read_text()
        self.assertIn("/etc/reposilite/update-in-progress", source)
        self.assertIn("/run/reposilite-update.authorized", source)
        self.assertIn("/run/reposilite-update.lock", source)
        self.guard.write_text(source.replace("/etc/reposilite", str(self.configuration)).replace(
            "/run/reposilite-update", str(self.run / "reposilite-update")
        ))
        self.guard.chmod(0o755)
        self.environment = {**os.environ, "LC_ALL": "C"}
        if stub_commands:
            commands = self.root / "bin"
            commands.mkdir()
            self.environment.update({
                "PATH": str(commands) + os.pathsep + os.defpath,
                "TEST_AUTHORIZATION_OWNER": "0",
                "TEST_FLOCK_STATUS": "1",
            })
            # macOS stat uses different flags. This shim preserves real file
            # modes while allowing the root-owner policy to be tested anywhere.
            (commands / "stat").write_text(
                "#!/bin/sh\nexec " + shlex.quote(sys.executable) + " -c " + shlex.quote(
                    "import os, sys\n"
                    "arguments = sys.argv[1:]\n"
                    "if len(arguments) != 3 or arguments[0] != '-c':\n"
                    "    sys.exit(2)\n"
                    "metadata = os.stat(arguments[2])\n"
                    "print(arguments[1].replace('%u', os.environ['TEST_AUTHORIZATION_OWNER'])"
                    ".replace('%a', format(metadata.st_mode & 0o7777, 'o')))\n"
                ) + ' "$@"\n'
            )
            (commands / "flock").write_text(
                '#!/bin/sh\n'
                '[ "$#" -eq 3 ] && [ "$1" = -n ] && '
                '[ "$2" = "$TEST_LOCK" ] && [ "$3" = /bin/true ] || exit 2\n'
                'exit "$TEST_FLOCK_STATUS"\n'
            )
            for command in commands.iterdir():
                command.chmod(0o755)
            self.environment["TEST_LOCK"] = str(self.lock)

    def pending_update(self, pid=None):
        self.marker.write_text("pending\n")
        self.authorization.write_text(str(os.getpid() if pid is None else pid) + "\n")
        self.authorization.chmod(0o600)
        self.lock.touch()

    def check_guard(self, action=None):
        return subprocess.run(
            ["/bin/sh", str(self.guard), *([] if action is None else [action])],
            env=self.environment, capture_output=True, text=True, timeout=5,
        )

    def assert_allowed(self, action=None):
        result = self.check_guard(action)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout + result.stderr, "")

    def assert_blocked(self, action=None):
        result = self.check_guard(action)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn(BLOCKED_MESSAGE, result.stderr)


class RecoveryGuardTests(GuardFixture, unittest.TestCase):
    def setUp(self):
        self.make_fixture()

    def test_absent_marker_allows_normal_service_commands(self):
        for action in (None, "start", "restart", "stop", "status", "custom-action"):
            with self.subTest(action=action):
                self.assert_allowed(action)

    def test_pending_marker_blocks_automatic_start_and_unknown_actions(self):
        self.marker.touch()
        for action in (None, "start", "restart", "reload", "custom-action"):
            with self.subTest(action=action):
                self.assert_blocked(action)

    def test_pending_marker_allows_recovery_and_inspection_commands(self):
        self.marker.touch()
        for action in ("stop", "status", "describe", "depend", "zap", "help"):
            with self.subTest(action=action):
                self.assert_allowed(action)

    def test_live_authorized_updater_with_held_lock_can_start(self):
        self.pending_update()
        for action in (None, "start", "restart"):
            with self.subTest(action=action):
                self.assert_allowed(action)

    def test_missing_authorization_blocks_start_even_with_held_lock(self):
        self.pending_update()
        self.authorization.unlink()
        self.assert_blocked("start")

    def test_dead_updater_pid_blocks_start_even_with_held_lock(self):
        self.pending_update(pid=2147483647)
        self.assert_blocked("start")

    def test_invalid_updater_pids_cannot_authorize_start(self):
        self.pending_update()
        for pid in ("", "0", "00", "000", "-1", "+1", "1x", "1 2"):
            with self.subTest(pid=pid):
                self.authorization.write_text(pid + "\n")
                self.assert_blocked("start")

    def test_authorization_requires_exact_private_file_permissions(self):
        self.pending_update()
        for mode in (0o400, 0o640, 0o644, 0o660, 0o700):
            with self.subTest(mode=oct(mode)):
                self.authorization.chmod(mode)
                self.assert_blocked("start")

    def test_non_root_authorization_blocks_start(self):
        self.pending_update()
        self.environment["TEST_AUTHORIZATION_OWNER"] = "1000"
        self.assert_blocked("start")

    def test_symlink_authorization_blocks_start(self):
        self.pending_update()
        target = self.authorization.with_suffix(".target")
        self.authorization.rename(target)
        self.authorization.symlink_to(target)
        self.assert_blocked("start")

    def test_non_regular_authorization_blocks_without_reading(self):
        self.pending_update()
        self.authorization.unlink()
        self.authorization.mkdir(mode=0o600)
        self.assert_blocked("start")
        self.authorization.rmdir()
        os.mkfifo(self.authorization, mode=0o600)
        self.assert_blocked("start")

    def test_unheld_lock_blocks_authorized_live_updater(self):
        self.pending_update()
        self.environment["TEST_FLOCK_STATUS"] = "0"
        self.assert_blocked("start")

    def test_broken_flock_is_not_treated_as_a_held_lock(self):
        self.pending_update()
        for status in ("2", "127"):
            with self.subTest(status=status):
                self.environment["TEST_FLOCK_STATUS"] = status
                self.assert_blocked("start")

    def test_clearing_marker_restores_normal_service_start(self):
        self.marker.touch()
        self.assert_blocked("start")
        self.marker.unlink()
        self.assert_allowed("start")
        self.assert_allowed("restart")


@unittest.skipUnless(
    sys.platform.startswith("linux") and os.geteuid() == 0 and shutil.which("flock"),
    "Real ownership and flock checks require Linux, root, and flock",
)
class LinuxRecoveryGuardTests(GuardFixture, unittest.TestCase):
    def setUp(self):
        self.make_fixture(stub_commands=False)

    def test_actual_root_authorization_needs_live_pid_and_held_lock(self):
        import fcntl

        self.pending_update()
        self.assertEqual(self.authorization.stat().st_uid, 0)
        self.assert_blocked("start")
        with self.lock.open("a") as held_lock:
            fcntl.flock(held_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assert_allowed("start")
            self.authorization.write_text("2147483647\n")
            self.assert_blocked("restart")
        self.authorization.write_text(str(os.getpid()) + "\n")
        self.assert_blocked("start")

    def test_actual_non_root_owner_is_rejected_despite_a_held_lock(self):
        import fcntl

        self.pending_update()
        os.chown(self.authorization, 65534, 65534)
        with self.lock.open("a") as held_lock:
            fcntl.flock(held_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assert_blocked("start")


if __name__ == "__main__":
    unittest.main()
