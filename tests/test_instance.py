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


if __name__ == "__main__":
    unittest.main()
