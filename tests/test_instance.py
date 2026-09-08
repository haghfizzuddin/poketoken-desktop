"""Single-instance bookkeeping (pid file + command file)."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from poketoken import instance  # noqa: E402


class InstanceTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_pid_alive(self):
        self.assertTrue(instance.pid_alive(os.getpid()))
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        self.assertFalse(instance.pid_alive(p.pid))
        self.assertFalse(instance.pid_alive(0))

    def test_pid_file_roundtrip_and_stale_cleanup(self):
        self.assertIsNone(instance.running_pid(self.dir))
        instance.write_pid(self.dir)
        self.assertEqual(instance.running_pid(self.dir), os.getpid())
        instance.clear_pid(self.dir)
        self.assertIsNone(instance.running_pid(self.dir))
        instance.pid_file(self.dir).write_text("999999999")          # stale
        self.assertIsNone(instance.running_pid(self.dir))
        self.assertFalse(instance.pid_file(self.dir).exists())
        instance.pid_file(self.dir).write_text("garbage")
        self.assertIsNone(instance.running_pid(self.dir))

    def test_pid_reuse_guard(self):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
        try:
            instance.pid_file(self.dir).write_text(str(p.pid))     # alive, but not poketoken
            if Path("/proc").exists():
                self.assertIsNone(instance.running_pid(self.dir))
        finally:
            p.kill(); p.wait()

    def test_commands(self):
        self.assertFalse(instance.send(self.dir, "quit"))             # nobody running
        self.assertIsNone(instance.take_command(self.dir))
        instance.write_pid(self.dir)
        self.assertTrue(instance.send(self.dir, "raise"))
        self.assertEqual(instance.take_command(self.dir), "raise")
        self.assertIsNone(instance.take_command(self.dir))            # consumed
        instance.clear_pid(self.dir)

    def test_wait_for(self):
        hits = []
        self.assertTrue(instance.wait_for(lambda: hits.append(1) or len(hits) >= 2, 2, step=0.01))
        self.assertFalse(instance.wait_for(lambda: False, 0.05, step=0.01))


class SuperviseTests(unittest.TestCase):
    """The launcher's supervisor: reopen the window when the display dies under it."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.logs: list[str] = []

    @staticmethod
    def _spawn(codes):
        it = iter(codes)
        return lambda: subprocess.Popen([sys.executable, "-c", f"raise SystemExit({next(it)})"])

    def test_clean_exit_ends_it(self):
        rc = instance.supervise(self.dir, self._spawn([0]), lambda: True, self.logs.append, sleep=lambda s: None)
        self.assertEqual(rc, 0)
        self.assertIsNone(instance.running_pid(self.dir))          # pid file released
        self.assertEqual(self.logs, [])

    def test_respawns_once_the_display_answers(self):
        probes = iter([False, False, True])
        slept: list[float] = []
        rc = instance.supervise(self.dir, self._spawn([1, 0]), lambda: next(probes), self.logs.append,
                                sleep=slept.append)
        self.assertEqual(rc, 0)
        self.assertEqual(slept, [2, 5])                               # backoff between probes
        self.assertEqual(len(self.logs), 1)
        self.assertIn("waiting for the display", self.logs[0])
        self.assertIsNone(instance.running_pid(self.dir))

    def test_quit_while_waiting_stops_it(self):
        def probe():
            instance.cmd_file(self.dir).write_text("quit\n")         # `poketoken close` while the display is down
            return False
        rc = instance.supervise(self.dir, self._spawn([1]), probe, self.logs.append, sleep=lambda s: None)
        self.assertEqual(rc, 0)
        self.assertIsNone(instance.take_command(self.dir))            # consumed, not left for the next window

    def test_gives_up_on_a_crash_loop(self):
        rc = instance.supervise(self.dir, self._spawn([3] * 5 + [0]), lambda: True, self.logs.append,
                                sleep=lambda s: None, max_quick=5)
        self.assertEqual(rc, 3)
        self.assertIn("giving up", self.logs[-1])
        self.assertIsNone(instance.running_pid(self.dir))

    def test_pid_file_is_held_while_running(self):
        seen: list = []

        def spawn():
            seen.append(instance.running_pid(self.dir))
            return subprocess.Popen([sys.executable, "-c", "raise SystemExit(0)"])
        instance.supervise(self.dir, spawn, lambda: True, self.logs.append, sleep=lambda s: None)
        self.assertEqual(seen, [os.getpid()])

    @unittest.skipIf(os.name == "nt", "a bogus DISPLAY only fails on X11")
    def test_probe_display_says_no_to_a_dead_display(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {"DISPLAY": ":99"}):
            self.assertFalse(instance.probe_display(timeout=20))


if __name__ == "__main__":
    unittest.main()
