"""Desktop notifications: settings, event text, backend selection and App.tick dedupe (all offline)."""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from poketoken import cli, notify  # noqa: E402


class FakeProc:
    def __init__(self, rc: int = 0):
        self.returncode = rc

    def wait(self, timeout=None):
        return self.returncode


class Recorder:
    """Stands in for subprocess.Popen: records argv/kwargs, spawns nothing."""

    def __init__(self, rc: int = 0, raise_: Exception | None = None):
        self.calls: list[tuple[list[str], dict]] = []
        self.rc, self.raise_ = rc, raise_

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), kw))
        if self.raise_:
            raise self.raise_
        return FakeProc(self.rc)


def no_run(*a, **k):
    raise AssertionError("subprocess.run must not be called from tests")


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_default_on_without_file(self):
        self.assertTrue(notify.enabled(self.dir))
        self.assertFalse(notify.settings_path(self.dir).exists())

    def test_roundtrip_keeps_other_keys(self):
        notify.save_settings(self.dir, {"theme": "dark"})
        notify.set_enabled(self.dir, False)
        self.assertFalse(notify.enabled(self.dir))
        self.assertEqual(json.loads(notify.settings_path(self.dir).read_text()), {"theme": "dark", "notifications": False})
        notify.set_enabled(self.dir, True)
        self.assertTrue(notify.enabled(self.dir))
        self.assertEqual(notify.load_settings(self.dir)["theme"], "dark")

    def test_unreadable_settings_default_on(self):
        notify.settings_path(self.dir).write_text("{not json")
        self.assertTrue(notify.enabled(self.dir))
        notify.settings_path(self.dir).write_text("[1, 2]")
        self.assertEqual(notify.load_settings(self.dir), {})
        notify.set_enabled(self.dir, False)                          # recovers by rewriting a dict
        self.assertFalse(notify.enabled(self.dir))


class EventTextTests(unittest.TestCase):
    def test_hatch(self):
        t, b = notify.event_text({"kind": "hatch", "at": 1.0, "species": 1, "name": "Bulbasaur", "rarity": "rare",
                                  "shiny": False, "nature": "jolly", "forms": 3})
        self.assertEqual(t, "Bulbasaur hatched!")
        self.assertEqual(b, "rare · Jolly nature")
        t, b = notify.event_text({"kind": "hatch", "name": "Pidgey", "shiny": True})
        self.assertEqual((t, b), ("Shiny Pidgey hatched!", ""))

    def test_evolve_and_graduate(self):
        self.assertEqual(notify.event_text({"kind": "evolve", "name": "Ivysaur", "stage": "2/3"}),
                         ("Evolved into Ivysaur!", "Stage 2/3"))
        t, b = notify.event_text({"kind": "graduate", "name": "Venusaur", "rarity": "rare", "shiny": True})
        self.assertEqual(t, "Venusaur joined the Pokédex")
        self.assertTrue(b.startswith("Shiny!"))
        t, b = notify.event_text({"kind": "graduate", "name": "Pidgeot", "shiny": False})
        self.assertEqual(b, "A new egg is incubating.")

    def test_candy(self):
        self.assertEqual(notify.event_text({"kind": "candy", "count": 2, "reason": "7-day streak"}),
                         ("+2 Rare Candy", "7-day streak"))

    def test_egg(self):
        self.assertEqual(notify.event_text({"kind": "egg", "tier": "plain", "released": None}), ("A new egg arrived", ""))
        t, b = notify.event_text({"kind": "egg", "tier": "rare", "released": "Pidgey"})
        self.assertEqual(b, "Pidgey was released. Guaranteed rare or better.")

    def test_quiet_kinds(self):
        for ev in ({"kind": "buy", "item": "mint"}, {"kind": "mint", "nature": "bold"}, {"kind": "other"}, {}):
            self.assertIsNone(notify.event_text(ev))


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.popen = Recorder()
        for p in (mock.patch.object(notify.subprocess, "Popen", self.popen),
                  mock.patch.object(notify.subprocess, "run", no_run)):
            p.start()
            self.addCleanup(p.stop)

    def log(self) -> str:
        return (self.dir / notify.LOG_FILE).read_text("utf-8")

    def test_notify_send_wins_when_on_path(self):
        with mock.patch.object(notify.shutil, "which", lambda n: "/usr/bin/notify-send" if n == "notify-send" else None):
            self.assertEqual(notify.backend(), "notify-send")
            self.assertTrue(notify.send("Title", "Body <x>\nmore", self.dir))
        (argv, kw), = self.popen.calls
        self.assertEqual(argv, ["notify-send", "--app-name=PokeToken", "Title", "Body <x> more"])
        self.assertTrue(kw["start_new_session"])
        self.assertEqual(kw["stdin"], notify.subprocess.DEVNULL)
        self.assertEqual(Path(kw["stdout"].name), self.dir / notify.LOG_FILE)
        self.assertNotIn("creationflags", kw)
        self.assertIn("notify-send: Title | Body <x> more", self.log())

    def test_toast_under_wsl_writes_escaped_script(self):
        with mock.patch.object(notify.shutil, "which", lambda n: None), \
             mock.patch.object(notify, "powershell", lambda: notify.WSL_POWERSHELL), \
             mock.patch.object(notify, "windows_path", lambda p: r"\\wsl.localhost\Ubuntu" + str(p).replace("/", "\\")):
            self.assertEqual(notify.backend(), "toast")
            self.assertTrue(notify.send("Tom & Jerry", "<3 the Pokédex", self.dir))
        (argv, kw), = self.popen.calls
        self.assertEqual(argv[:6], [notify.WSL_POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File"])
        self.assertTrue(argv[6].startswith(r"\\wsl.localhost\Ubuntu" + "\\"))
        self.assertTrue(kw["start_new_session"])
        scripts = list(self.dir.glob("toast-*.ps1"))
        self.assertEqual(len(scripts), 1)
        raw = scripts[0].read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))                        # BOM for PowerShell 5.1
        text = raw.decode("utf-8-sig")
        self.assertIn("<text>Tom &amp; Jerry</text><text>&lt;3 the Pokédex</text>", text)
        self.assertIn(f"$appId = '{notify.TOAST_APP_ID}'", text)
        self.assertNotIn("Tom & Jerry", text)
        self.assertEqual(argv[6], r"\\wsl.localhost\Ubuntu" + str(scripts[0]).replace("/", "\\"))

    def test_stale_scripts_are_pruned(self):
        import os
        old = self.dir / "toast-1.ps1"
        old.write_text("x")
        os.utime(old, (0, 0))
        fresh = self.dir / "toast-2.ps1"
        fresh.write_text("x")
        with mock.patch.object(notify.shutil, "which", lambda n: None), \
             mock.patch.object(notify, "powershell", lambda: "pwsh.exe"), \
             mock.patch.object(notify, "windows_path", lambda p: str(p)):
            self.assertTrue(notify.send("a", "b", self.dir))
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())

    def test_no_backend(self):
        with mock.patch.object(notify.shutil, "which", lambda n: None), mock.patch.object(notify, "is_wsl", lambda: False):
            self.assertIsNone(notify.powershell())
            self.assertIsNone(notify.backend())
            self.assertFalse(notify.send("a", "b", self.dir))
        self.assertEqual(self.popen.calls, [])
        self.assertIn("no backend for: a", self.log())

    def test_native_windows(self):
        script = Path("C:/x/toast.ps1")          # built first: pathlib picks its flavour from os.name at construction
        with mock.patch.object(notify.os, "name", "nt"), mock.patch.object(notify.shutil, "which", lambda n: None), \
             mock.patch.dict(notify.os.environ, {"SystemRoot": r"C:\Windows"}):
            self.assertEqual(notify.powershell(), r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
            self.assertEqual(notify.backend(), "toast")
            self.assertEqual(notify.windows_path(script), "C:/x/toast.ps1")               # no wslpath on Windows
            self.assertTrue(notify.send("a", "b", self.dir))
        (argv, kw), = self.popen.calls
        self.assertTrue(argv[0].endswith("powershell.exe"))
        self.assertIn("creationflags", kw)

    def test_wsl_detection_is_a_bool(self):
        self.assertIsInstance(notify.is_wsl(), bool)
        with mock.patch.object(notify, "is_wsl", lambda: True), \
             mock.patch.object(notify.shutil, "which", lambda n: "/usr/bin/powershell.exe" if n == "powershell.exe" else None):
            self.assertTrue(notify.powershell().endswith("powershell.exe"))

    def test_send_never_raises(self):
        self.popen.raise_ = OSError("boom")
        with mock.patch.object(notify.shutil, "which", lambda n: "/usr/bin/notify-send"):
            self.assertFalse(notify.send("a", "b", self.dir))
        self.assertIn("notify failed: OSError('boom')", self.log())

    def test_wait_reports_exit_code(self):
        self.popen.rc = 1
        with mock.patch.object(notify.shutil, "which", lambda n: "/usr/bin/notify-send"):
            self.assertFalse(notify.send("a", "b", self.dir, wait=1))
            self.popen.rc = 0
            self.assertTrue(notify.send("a", "b", self.dir, wait=1))
        self.assertIn("notify-send: exit 1", self.log())
        self.assertIn("notify-send: exit 0", self.log())

    def test_notify_event_respects_enabled_and_kind(self):
        sent: list[tuple[str, str]] = []
        with mock.patch.object(notify, "send", lambda t, b, d, wait=0.0: sent.append((t, b)) or True):
            notify.set_enabled(self.dir, False)
            self.assertFalse(notify.notify_event({"kind": "hatch", "name": "A"}, self.dir))
            notify.set_enabled(self.dir, True)
            self.assertFalse(notify.notify_event({"kind": "buy", "item": "mint"}, self.dir))
            self.assertTrue(notify.notify_event({"kind": "hatch", "name": "A", "shiny": True}, self.dir))
        self.assertEqual(sent, [("Shiny A hatched!", "")])


class TickDedupeTests(unittest.TestCase):
    def make_app(self) -> cli.App:
        app = cli.App(Path(tempfile.mkdtemp()))
        app.reader.scan = lambda since: []                              # no log files, no network
        app.companion.update = lambda *a, **k: "egg"
        return app

    def test_each_event_notified_once_across_ticks(self):
        app, comp, sent = self.make_app(), None, []
        comp = app.companion
        with mock.patch.object(notify, "notify_event", lambda ev, d: sent.append(ev["kind"]) or True):
            comp.events += [{"kind": "hatch", "at": 1.0, "name": "A"},
                            {"kind": "candy", "at": 1.0, "count": 1, "reason": "3-day streak"}]   # same stamp
            app.tick()
            app.tick()                                                  # not drained yet: nothing new
            self.assertEqual(sent, ["hatch", "candy"])
            comp.events.append({"kind": "evolve", "at": 2.0, "name": "B"})
            app.tick()
            self.assertEqual(sent, ["hatch", "candy", "evolve"])
            self.assertEqual(len(app.notified), 3)
            self.assertEqual(len(comp.drain_events()), 3)
            app.tick()
            self.assertEqual(app.notified, set())                       # forgotten once drained
            comp.events.append({"kind": "graduate", "at": 3.0, "name": "C"})
            app.tick()
            self.assertEqual(sent, ["hatch", "candy", "evolve", "graduate"])

    def test_notify_new_events_counts_only_sent(self):
        app = self.make_app()
        app.companion.events += [{"kind": "buy", "at": 1.0, "item": "mint"}, {"kind": "egg", "at": 2.0, "tier": "plain"}]
        with mock.patch.object(notify, "send", lambda t, b, d, wait=0.0: True):
            self.assertEqual(app.notify_new_events(), 1)
            self.assertEqual(app.notify_new_events(), 0)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.base = ["--state-dir", str(self.dir), "notify"]

    def run_cli(self, *argv) -> tuple[int, str]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(self.base + list(argv))
        return rc, buf.getvalue()

    def test_on_off_status(self):
        rc, out = self.run_cli("off")
        self.assertEqual((rc, notify.enabled(self.dir)), (0, False))
        with mock.patch.object(notify, "backend", lambda: "toast"):
            rc, out = self.run_cli()
            self.assertEqual(rc, 0)
            self.assertIn("notifications: off", out)
            self.assertIn("backend: toast", out)
        rc, _ = self.run_cli("on")
        self.assertTrue(notify.enabled(self.dir))
        rc, out = self.run_cli("status")
        self.assertIn("notifications: on", out)

    def test_test_command(self):
        with mock.patch.object(notify, "backend", lambda: None):
            rc, out = self.run_cli("test")
            self.assertEqual(rc, 1)
            self.assertIn("no notification backend", out)
        sent = []
        with mock.patch.object(notify, "backend", lambda: "notify-send"), \
             mock.patch.object(notify, "send", lambda t, b, d, wait=0.0: sent.append((t, b, wait)) or True):
            notify.set_enabled(self.dir, False)
            rc, out = self.run_cli("test")
        self.assertEqual(rc, 0)
        self.assertIn("backend notify-send: accepted", out)
        self.assertIn("notifications are off", out)
        self.assertEqual(sent[0][:2], ("PokeToken", "Notifications are working."))
        self.assertGreater(sent[0][2], 0)


if __name__ == "__main__":
    unittest.main()
