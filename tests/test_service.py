"""Timer, autostart and export/import: unit texts, mocked systemctl / cmd.exe, temp dirs — all offline.

Nothing here touches ~/.config, the Windows Startup folder or the real save: every system call
goes through a recorder and every path is a temp directory.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path, PosixPath
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from poketoken import cli, companion as C, instance, service  # noqa: E402

PROBE = ["systemctl", "--user", "is-system-running"]
STATE = {   # a realistic save: Pidgeotto at stage 2/3, one graduated shiny Venusaur, two days of history
    "installBaselineSet": True, "usedSinceInstall": 1_250_000_000, "spentTokens": 100_000_000,
    "eggUsage": 0, "eggTier": None, "pendingHatchID": None, "claimedTodayTokensByProvider": {"claude-code": 5},
    "lastDate": "2026-09-07",
    "active": {"baseID": 16, "pathIDs": [16, 17], "plannedPathIDs": [16, 17, 18], "stageIndex": 1,
               "usedAtStage": 12_000_000, "rarity": "common", "totalForms": 3, "isShiny": False, "nature": "jolly"},
    "dex": [{"id": "d1", "baseID": 1, "finalID": 3, "chainOrder": [1, 2, 3], "rarity": "rare",
             "caughtAt": "2026-08-01T10:00:00+03:00", "isShiny": True, "nature": "bold",
             "names": {"1": {"en": "Bulbasaur"}, "2": {"en": "Ivysaur"}, "3": {"en": "Venusaur"}}}],
    "collectedFinals": ["1:3"], "language": "en", "inventory": {"rareCandy": 2}, "candyGrantTier": {},
    "candyFeatureSeeded": True,
    "history": {"2026-09-06": {"tokens": 3_000_000, "cost": 1.0}, "2026-09-07": {"tokens": 5_000_000, "cost": 2.0}},
    "historyBackfilled": True,
}


class FakeRun:
    """Stands in for subprocess.run: records argv, answers from a small table, spawns nothing."""

    def __init__(self, system_running: str = "running"):
        self.calls: list[list[str]] = []
        self.kwargs: list[dict] = []
        self.system_running = system_running
        self.show: dict[str, str] = {}
        self.journal = ""
        self.appdata = "C:\\Users\\x\\AppData\\Roaming\r\n"
        self.wslpath = "/mnt/c/Users/x/AppData/Roaming\n"
        self.fail: set[str] = set()                     # systemctl verbs that exit 1

    def __call__(self, argv, **kw):
        argv = list(argv)
        self.calls.append(argv)
        self.kwargs.append(kw)
        out, err, rc = "", "", 0
        if argv[:3] == PROBE:
            out = self.system_running + "\n"
            rc = 0 if self.system_running == "running" else 1
        elif argv[:2] == ["systemctl", "--user"]:
            if argv[2] == "show":
                out = self.show.get(argv[3], "LoadState=not-found\nActiveState=inactive\nLastTriggerUSec=\n")
            if argv[2] in self.fail:
                rc, err = 1, f"Failed to {argv[2]}: boom\n"
        elif argv[:1] == ["journalctl"]:
            out = self.journal
        elif argv[:1] == ["cmd.exe"]:
            out = self.appdata
        elif argv[:1] == ["wslpath"]:
            out = self.wslpath
        return subprocess.CompletedProcess(argv, rc, out, err)


def run_cli(*argv: str) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(list(argv))
    return rc, buf.getvalue()


# ------------------------------------------------------------------ pure text
class UnitFileTests(unittest.TestCase):
    def test_service_and_timer_text(self):
        units = service.unit_files(Path("/home/u/.local/share/poketoken"), "/usr/bin/python3", Path("/home/u/poketoken-desktop"))
        self.assertEqual(set(units), {"poketoken-refresh.service", "poketoken-refresh.timer"})
        svc = units["poketoken-refresh.service"]
        self.assertIn("[Service]\nType=oneshot\n", svc)
        self.assertIn("WorkingDirectory=/home/u/poketoken-desktop\n", svc)
        self.assertIn("ExecStart=/usr/bin/python3 -m poketoken --state-dir /home/u/.local/share/poketoken refresh\n", svc)
        tm = units["poketoken-refresh.timer"]
        for line in ("[Timer]", "OnBootSec=2min", "OnUnitActiveSec=15min", "Persistent=true",
                     "Unit=poketoken-refresh.service", "[Install]", "WantedBy=timers.target"):
            self.assertIn(line + "\n", tm)
        self.assertTrue(svc.startswith("[Unit]\nDescription=") and tm.startswith("[Unit]\nDescription="))

    def test_awkward_paths_are_quoted_and_percent_doubled(self):
        units = service.unit_files(Path("/tmp/my dir/100%"), "/opt/py thon/python3", Path("/r/100%"))
        svc = units["poketoken-refresh.service"]
        self.assertIn("ExecStart='/opt/py thon/python3' -m poketoken --state-dir '/tmp/my dir/100%%' refresh\n", svc)
        self.assertIn("WorkingDirectory=/r/100%%\n", svc)

    def test_systemd_user_dir_follows_xdg(self):
        tmp = Path(tempfile.mkdtemp())
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(tmp)}):
            self.assertEqual(service.systemd_user_dir(), tmp / "systemd" / "user")
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": ""}):
            self.assertEqual(service.systemd_user_dir(), Path.home() / ".config" / "systemd" / "user")
        self.assertFalse((tmp / "systemd").exists())                     # a path, never a mkdir

    def test_cron_hint(self):
        hint = service.cron_hint(Path("/s d"), "/usr/bin/python3", Path("/repo"))
        self.assertTrue(hint.startswith("*/15 * * * * cd /repo && /usr/bin/python3 -m poketoken --state-dir '/s d' refresh"))
        sd, repo = Path("C:/s"), Path("C:/repo")               # built first: pathlib picks its class from os.name
        with mock.patch.object(service.os, "name", "nt"):
            hint = service.cron_hint(sd, "C:/py.exe", repo)
        self.assertTrue(hint.startswith("schtasks /Create /SC MINUTE /MO 15 /TN PokeTokenRefresh /TR "))
        self.assertIn("C:/py.exe -m poketoken --state-dir C:/s refresh", hint)


class AutostartTextTests(unittest.TestCase):
    def test_wsl_vbs(self):
        text = service.vbs_wsl_text("Ubuntu-22.04")
        self.assertIn('Set sh = CreateObject("WScript.Shell")\n', text)
        self.assertIn('sh.Run "wsl.exe -d Ubuntu-22.04 -- bash -lc ""$HOME/.local/bin/poketoken app""", 0, False\n', text)
        self.assertTrue(text.startswith("' PokeToken"))
        self.assertNotIn("toggle", text)                                  # autostart opens; it must never close

    def test_windows_vbs(self):
        text = service.vbs_windows_text(r"C:\Python311\pythonw.exe", Path(r"C:\Users\me\poketoken-desktop"),
                                        Path(r"C:\Users\me\AppData\Local\poketoken"))
        self.assertIn('sh.CurrentDirectory = "C:\\Users\\me\\poketoken-desktop"\n', text)
        self.assertIn('sh.Run """C:\\Python311\\pythonw.exe"" -m poketoken --state-dir '
                      '""C:\\Users\\me\\AppData\\Local\\poketoken"" app", 0, False\n', text)
        self.assertNotIn("wsl.exe", text)

    def test_linux_desktop_entry(self):
        text = service.desktop_text("/usr/bin/python3", Path("/home/u/poketoken-desktop"), Path("/home/u/my state"))
        self.assertTrue(text.startswith("[Desktop Entry]\nType=Application\nName=PokeToken\n"))
        self.assertIn('Exec=/usr/bin/python3 -m poketoken --state-dir "/home/u/my state" app\n', text)
        self.assertIn("Path=/home/u/poketoken-desktop\n", text)
        self.assertIn("Terminal=false\n", text)
        self.assertIn("X-GNOME-Autostart-enabled=true\n", text)
        plain = service.desktop_text("/usr/bin/python3", Path("/r"), Path("/s/50%"))
        self.assertIn("Exec=/usr/bin/python3 -m poketoken --state-dir /s/50%% app\n", plain)

    def test_dispatch(self):
        sd, root = Path("/s"), Path("/r")
        self.assertEqual(service.autostart_text("wsl", sd, "/py", root, distro="Deb"), service.vbs_wsl_text("Deb"))
        self.assertEqual(service.autostart_text("windows", sd, "/py", root), service.vbs_windows_text("/py", root, sd))
        self.assertEqual(service.autostart_text("linux", sd, "/py", root), service.desktop_text("/py", root, sd))
        with mock.patch.dict(os.environ, {"WSL_DISTRO_NAME": "Fedora"}):
            self.assertIn("wsl.exe -d Fedora ", service.autostart_text("wsl", sd))
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(service.wsl_distro(), "Ubuntu")


class EnvelopeTests(unittest.TestCase):
    def test_export_envelope_and_validation(self):
        env = service.export_envelope(STATE, host="box")
        self.assertEqual((env["format"], env["version"], env["host"]), ("poketoken-save", 1, "box"))
        self.assertIs(env["state"], STATE)
        self.assertRegex(env["exportedAt"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
        self.assertIs(service.validate_envelope(env), STATE)
        for bad, msg in (([], "not a JSON object"), ({"format": "x", "version": 1, "state": {}}, "format is 'x'"),
                         ({"format": "poketoken-save", "version": 2, "state": {}}, "version 2 is not supported"),
                         ({"format": "poketoken-save", "version": 1, "state": [1]}, "'state' is missing or not an object"),
                         ({"format": "poketoken-save", "version": 1}, "'state' is missing")):
            with self.assertRaises(ValueError) as cm:
                service.validate_envelope(bad)
            self.assertIn(msg, str(cm.exception))

    def test_read_envelope_rejects_non_json(self):
        p = Path(tempfile.mkdtemp()) / "x.json"
        p.write_text("{not json")
        with self.assertRaises(ValueError) as cm:
            service.read_envelope(p)
        self.assertIn("not valid JSON", str(cm.exception))
        p.write_text(json.dumps(service.export_envelope(STATE, host="h")))
        env, state = service.read_envelope(p)
        self.assertEqual((env["host"], state), ("h", STATE))

    def test_default_path_and_backup_name(self):
        from datetime import date
        self.assertEqual(service.default_export_path(date(2026, 9, 8)), Path("poketoken-save-2026-09-08.json"))
        self.assertRegex(service.default_export_path().name, r"^poketoken-save-\d{4}-\d{2}-\d{2}\.json$")
        b = service.backup_path(Path("/s/state.json"), now=0)
        self.assertEqual(b.parent, Path("/s"))
        self.assertRegex(b.name, r"^state\.json\.bak-\d{8}-\d{6}$")

    def test_save_summary(self):
        self.assertEqual(service.save_summary(STATE),
                         "#17 common stage 2/3 · Pokédex 1 graduated (1 entries) · 1.25B tokens since install · 2 days of history")
        self.assertTrue(service.save_summary(STATE, lambda base, sid: f"{base}/{sid}").startswith("16/17 common"))
        egg = {"usedSinceInstall": 42_000_000, "eggUsage": 2_500_000}
        self.assertEqual(service.save_summary(egg), "egg 50% · Pokédex 0 graduated (0 entries) · 42M tokens since install · 0 days of history")
        shiny = dict(STATE, active=dict(STATE["active"], isShiny=True))
        self.assertIn("#17 ✨ common", service.save_summary(shiny))

    def test_cached_name_is_offline(self):
        cache = Path(tempfile.mkdtemp())
        self.assertEqual(service.cached_name(cache, 16, 17), "#17")
        (cache / "lines").mkdir()
        (cache / "lines" / "16.json").write_text(json.dumps({          # EvoLine.to_dict shape
            "baseID": 16, "rarity": "common",
            "tree": {"speciesID": 16, "children": [{"speciesID": 17, "children": []}]},
            "names": {"16": {"en": "Pidgey"}, "17": {"en": "Pidgeotto", "fr": "Roucoups"}}}))
        self.assertEqual(service.cached_name(cache, 16, 17), "Pidgeotto")
        self.assertEqual(service.cached_name(cache, 16, 17, "fr"), "Roucoups")
        self.assertEqual(service.cached_name(cache, 16, 99), "#99")
        (cache / "lines" / "16.json").write_text("garbage")
        self.assertEqual(service.cached_name(cache, 16, 17), "#17")


# ------------------------------------------------------------------ systemd timer
class TimerTests(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp())
        self.units = Path(tempfile.mkdtemp()) / "systemd" / "user"
        self.run = FakeRun()
        for p in (mock.patch.object(service.subprocess, "run", self.run),
                  mock.patch.object(service.shutil, "which", lambda n: f"/usr/bin/{n}"),
                  mock.patch.object(service, "systemd_user_dir", lambda: self.units)):
            p.start()
            self.addCleanup(p.stop)

    def cli(self, *argv):
        return run_cli("--state-dir", str(self.state), "timer", *argv)

    def test_on_writes_units_and_enables(self):
        rc, out = self.cli("on")
        self.assertEqual(rc, 0, out)
        expected = service.unit_files(self.state, sys.executable, service.repo_root())
        for name, text in expected.items():
            self.assertEqual((self.units / name).read_text("utf-8"), text)
        self.assertEqual(self.run.calls, [PROBE, ["systemctl", "--user", "daemon-reload"],
                                          ["systemctl", "--user", "enable", "--now", "poketoken-refresh.timer"]])
        self.assertIn("streaks", out)
        self.assertIn("midnight", out)
        self.assertIn(str(self.units / "poketoken-refresh.timer"), out)

    def test_on_reports_enable_failure(self):
        self.run.fail.add("enable")
        rc, out = self.cli("on")
        self.assertEqual(rc, 1)
        self.assertIn("enable --now poketoken-refresh.timer failed (exit 1): Failed to enable: boom", out)
        self.assertTrue((self.units / "poketoken-refresh.service").exists())      # files stay for inspection

    def test_off_disables_and_removes(self):
        service.timer_on(self.state, unit_dir=self.units)
        self.run.calls.clear()
        rc, out = self.cli("off")
        self.assertEqual(rc, 0)
        self.assertEqual(self.run.calls, [PROBE, ["systemctl", "--user", "disable", "--now", "poketoken-refresh.timer"],
                                          ["systemctl", "--user", "daemon-reload"]])
        self.assertEqual(sorted(p.name for p in self.units.iterdir()), [])
        self.assertIn("removed poketoken-refresh.timer, poketoken-refresh.service", out)
        rc, out = self.cli("off")                                                  # idempotent
        self.assertEqual(rc, 0)
        self.assertIn("removed nothing", out)

    def test_status_not_installed(self):
        rc, out = self.cli()
        self.assertEqual(rc, 0)
        self.assertIn("timer: not installed", out)
        self.assertIn("poketoken timer on", out)
        self.assertEqual(self.run.calls[0], PROBE)
        self.assertEqual([c[2] for c in self.run.calls[1:3]], ["show", "show"])
        self.assertEqual(self.run.calls[3][:4], ["journalctl", "--user", "-u", "poketoken-refresh.service"])

    def test_status_installed(self):
        service.timer_on(self.state, unit_dir=self.units)
        self.run.show["poketoken-refresh.timer"] = ("LoadState=loaded\nActiveState=active\n"
                                                    "LastTriggerUSec=Mon 2026-09-07 18:48:36 +08\n"
                                                    "NextElapseUSecRealtime=Mon 2026-09-07 19:03:36 +08\n")
        self.run.show["poketoken-refresh.service"] = "ActiveState=inactive\nResult=success\n"
        self.run.journal = "2026-09-07 today=1,234 state=idle files=3 events=0\n"
        rc, out = self.cli("status")
        self.assertEqual(rc, 0)
        self.assertIn("timer: active", out)
        self.assertIn("poketoken-refresh.service: present", out)
        self.assertIn("last run: Mon 2026-09-07 18:48:36 +08 (success)", out)
        self.assertIn("next run: Mon 2026-09-07 19:03:36 +08", out)
        self.assertIn("last output: 2026-09-07 today=1,234", out)

    def test_status_tolerates_missing_journalctl(self):
        def run(argv, **kw):
            if argv[0] == "journalctl":
                raise FileNotFoundError("journalctl")
            return self.run(argv, **kw)
        with mock.patch.object(service.subprocess, "run", run):
            st = service.timer_status(self.units)
        self.assertEqual(st["last_log"], "")
        self.assertFalse(st["loaded"])

    def test_without_systemd_prints_cron_hint(self):
        self.run.system_running = "offline"
        for action in ("on", "off", "status"):
            self.run.calls.clear()
            rc, out = self.cli(action)
            self.assertEqual(rc, 1, action)
            self.assertIn("no systemd user session", out)
            self.assertIn("*/15 * * * * cd ", out)
            self.assertIn(f"-m poketoken --state-dir {self.state} refresh >/dev/null 2>&1", out)
            self.assertEqual(self.run.calls, [PROBE])
        self.assertFalse(self.units.exists())

    def test_degraded_counts_as_running(self):
        self.run.system_running = "degraded"
        self.assertTrue(service.has_systemd_user())

    def test_no_systemctl_binary_or_windows(self):
        with mock.patch.object(service.shutil, "which", lambda n: None):
            self.assertFalse(service.has_systemd_user())
        with mock.patch.object(service.os, "name", "nt"):
            self.assertFalse(service.has_systemd_user())
        self.assertEqual(self.run.calls, [])
        with mock.patch.object(service.subprocess, "run", mock.Mock(side_effect=FileNotFoundError("systemctl"))):
            r = service.systemctl("is-system-running")
            self.assertEqual(r.returncode, 127)
            self.assertFalse(service.has_systemd_user())


# ------------------------------------------------------------------ autostart
class AutostartTargetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.run = FakeRun()
        p = mock.patch.object(service.subprocess, "run", self.run)
        p.start()
        self.addCleanup(p.stop)

    def test_wsl_resolves_appdata_through_cmd_and_wslpath(self):
        with mock.patch.object(service, "is_wsl", lambda: True), mock.patch.object(service.os, "name", "posix"):
            flavour, path = service.autostart_target()
        self.assertEqual(flavour, "wsl")
        self.assertEqual(path, Path("/mnt/c/Users/x/AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup/PokeToken.vbs"))
        self.assertEqual(self.run.calls, [["cmd.exe", "/c", "echo %APPDATA%"], ["wslpath", "-u", "C:\\Users\\x\\AppData\\Roaming"]])
        self.assertIn(self.run.kwargs[0].get("cwd"), ("/mnt/c", None))

    def test_wsl_failures_are_unsupported(self):
        with mock.patch.object(service, "is_wsl", lambda: True):
            self.run.appdata = "%APPDATA%\r\n"                       # cmd.exe did not expand it
            self.assertIsNone(service.windows_appdata())
            with self.assertRaises(service.Unsupported):
                service.autostart_target()
            self.run.appdata = ""
            self.assertIsNone(service.windows_appdata())
            self.run.appdata, self.run.wslpath = "C:\\x\r\n", ""
            self.assertIsNone(service.windows_appdata())
        with mock.patch.object(service, "is_wsl", lambda: True), \
             mock.patch.object(service.subprocess, "run", mock.Mock(side_effect=OSError("no interop"))):
            self.assertIsNone(service.windows_appdata())

    def test_native_windows_uses_environment(self):
        # os.name patched to "nt" on Linux makes pathlib refuse Path(): give the module PosixPath for the duration
        with mock.patch.object(service.os, "name", "nt"), mock.patch.object(service, "Path", PosixPath), \
             mock.patch.dict(os.environ, {"APPDATA": str(self.tmp)}):
            flavour, path = service.autostart_target()
            self.assertEqual((flavour, path), ("windows", self.tmp / "Microsoft/Windows/Start Menu/Programs/Startup/PokeToken.vbs"))
            self.assertEqual(self.run.calls, [])
        with mock.patch.object(service.os, "name", "nt"), mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(service.windows_appdata())
            with self.assertRaises(service.Unsupported):
                service.autostart_target()

    def test_linux_xdg(self):
        with mock.patch.object(service, "is_wsl", lambda: False), mock.patch.object(service.os, "name", "posix"), \
             mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.tmp)}):
            self.assertEqual(service.autostart_target(), ("linux", self.tmp / "autostart" / "poketoken.desktop"))
        with mock.patch.object(service, "is_wsl", lambda: False), mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": ""}):
            self.assertEqual(service.autostart_target()[1], Path.home() / ".config" / "autostart" / "poketoken.desktop")
        self.assertEqual(self.run.calls, [])


class AutostartCliTests(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp())
        self.tmp = Path(tempfile.mkdtemp())
        p = mock.patch.object(service.subprocess, "run", FakeRun())
        p.start()
        self.addCleanup(p.stop)

    def cli(self, *argv):
        return run_cli("--state-dir", str(self.state), "autostart", *argv)

    def test_linux_on_status_off(self):
        target = self.tmp / "autostart" / "poketoken.desktop"
        with mock.patch.object(service, "autostart_target", lambda: ("linux", target)):
            rc, out = self.cli("status")
            self.assertEqual(rc, 0)
            self.assertIn("autostart: off · XDG autostart", out)
            rc, out = self.cli("on")
            self.assertEqual(rc, 0)
            self.assertIn(f"autostart on — XDG autostart: {target}", out)
            self.assertEqual(target.read_text("utf-8"), service.desktop_text(sys.executable, service.repo_root(), self.state))
            rc, out = self.cli()
            self.assertIn("autostart: on", out)
            rc, out = self.cli("off")
            self.assertEqual(rc, 0)
            self.assertFalse(target.exists())
            self.assertIn("removed", out)
            rc, out = self.cli("off")
            self.assertEqual(rc, 0)
            self.assertIn("already off", out)

    def test_wsl_vbs_and_install_hint(self):
        target = self.tmp / "Startup" / "PokeToken.vbs"
        home = self.tmp / "home"
        home.mkdir()
        with mock.patch.object(service, "autostart_target", lambda: ("wsl", target)), \
             mock.patch.dict(os.environ, {"WSL_DISTRO_NAME": "Ubuntu-24.04", "HOME": str(home)}):
            rc, out = self.cli("on")
            self.assertEqual(rc, 0)
            self.assertEqual(target.read_text("utf-8"), service.vbs_wsl_text("Ubuntu-24.04"))
            self.assertIn("Windows Startup folder (through wsl.exe)", out)
            self.assertIn("~/.local/bin/poketoken is missing", out)
            (home / ".local" / "bin").mkdir(parents=True)
            (home / ".local" / "bin" / "poketoken").write_text("#!/bin/sh\n")
            rc, out = self.cli("on")
            self.assertNotIn("is missing", out)

    def test_windows_vbs_through_cli(self):
        target = self.tmp / "Startup" / "PokeToken.vbs"
        with mock.patch.object(service, "autostart_target", lambda: ("windows", target)):
            rc, out = self.cli("on")
        self.assertEqual(rc, 0)
        self.assertEqual(target.read_text("utf-8"), service.vbs_windows_text(sys.executable, service.repo_root(), self.state))

    def test_unsupported(self):
        with mock.patch.object(service, "autostart_target", mock.Mock(side_effect=service.Unsupported("no Windows here"))):
            rc, out = self.cli("on")
        self.assertEqual(rc, 1)
        self.assertIn("autostart unavailable: no Windows here", out)


# ------------------------------------------------------------------ export / import
class ExportImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.a = self.tmp / "a"
        self.a.mkdir()
        (self.a / "state.json").write_text(json.dumps(STATE), "utf-8")
        p = mock.patch.object(cli.App, "tick", lambda self, now=None: None)      # no log scan, no network
        p.start()
        self.addCleanup(p.stop)

    def state(self, d: Path) -> dict:
        return json.loads((d / "state.json").read_text("utf-8"))

    def backups(self, d: Path) -> list[Path]:
        return sorted(d.glob("state.json.bak-*"))

    def export(self, *argv, state: Path | None = None):
        return run_cli("--state-dir", str(state or self.a), "export", *argv)

    def import_(self, *argv, state: Path):
        return run_cli("--state-dir", str(state), "import", *argv)

    def test_export_default_name_in_cwd(self):
        cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, cwd)
        rc, out = self.export()
        self.assertEqual(rc, 0, out)
        (f,) = self.tmp.glob("poketoken-save-*.json")
        self.assertEqual(f.name, service.default_export_path().name)
        env = json.loads(f.read_text("utf-8"))
        self.assertEqual((env["format"], env["version"], env["state"]), ("poketoken-save", 1, STATE))
        self.assertTrue(env["host"] and env["exportedAt"])
        self.assertIn(f"exported {f.name}", out)
        self.assertIn("Pokédex 1 graduated", out)
        self.assertIn("1.25B tokens since install", out)
        self.assertIn("poketoken import", out)

    def test_export_to_directory_and_to_file(self):
        rc, out = self.export(str(self.tmp))
        self.assertEqual(rc, 0)
        self.assertTrue((self.tmp / service.default_export_path().name).exists())
        rc, out = self.export(str(self.tmp / "my-save.json"))
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads((self.tmp / "my-save.json").read_text())["state"], STATE)
        self.assertFalse(list(self.tmp.glob("*.tmp")))

    def test_export_without_save_or_unwritable(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        rc, out = self.export(str(self.tmp / "x.json"), state=empty)
        self.assertEqual(rc, 1)
        self.assertIn("no save to export yet", out)
        rc, out = self.export(str(self.tmp / "missing-dir" / "x.json"))
        self.assertEqual(rc, 1)
        self.assertIn("cannot write", out)

    def test_export_survives_refresh_failure(self):
        with mock.patch.object(cli.App, "tick", mock.Mock(side_effect=RuntimeError("no logs"))):
            rc, out = self.export(str(self.tmp / "x.json"))
        self.assertEqual(rc, 0)
        self.assertIn("refresh before export failed (no logs)", out)
        self.assertEqual(json.loads((self.tmp / "x.json").read_text())["state"], STATE)

    def test_roundtrip_into_fresh_dir(self):
        rc, _ = self.export(str(self.tmp / "e.json"))
        self.assertEqual(rc, 0)
        b = self.tmp / "b"
        rc, out = self.import_(str(self.tmp / "e.json"), state=b)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.state(b), STATE)
        self.assertEqual(self.backups(b), [])
        self.assertIn("incoming (", out)
        self.assertNotIn("current (", out)
        self.assertIn(f"imported into {b / 'state.json'}", out)
        self.assertNotIn("backed up", out)
        loaded = C.CompanionState.from_dict(self.state(b))
        self.assertEqual((loaded.active.current_id, len(loaded.dex)), (17, 1))

    def test_refuses_existing_save_without_replace(self):
        other = dict(STATE, usedSinceInstall=7)
        (self.tmp / "e.json").write_text(json.dumps(service.export_envelope(other, host="laptop")))
        rc, out = self.import_(str(self.tmp / "e.json"), state=self.a)
        self.assertEqual(rc, 1)
        self.assertIn("add --replace", out)
        self.assertIn("incoming (laptop", out)
        self.assertIn(f"current ({self.a})", out)
        self.assertEqual(self.state(self.a), STATE)
        self.assertEqual(self.backups(self.a), [])

    def test_replace_backs_up_then_installs(self):
        other = dict(STATE, usedSinceInstall=7, dex=[])
        (self.tmp / "e.json").write_text(json.dumps(service.export_envelope(other)))
        rc, out = self.import_(str(self.tmp / "e.json"), "--replace", state=self.a)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.state(self.a), other)
        (bak,) = self.backups(self.a)
        self.assertEqual(json.loads(bak.read_text("utf-8")), STATE)
        self.assertIn(f"previous save backed up to {bak}", out)
        self.assertFalse((self.a / "state.json.tmp").exists())

    def test_envelope_errors(self):
        cases = {"notjson.json": "{oops", "list.json": "[1]", "format.json": json.dumps({"format": "x", "version": 1, "state": {}}),
                 "version.json": json.dumps({"format": "poketoken-save", "version": 99, "state": {}}),
                 "state.json": json.dumps({"format": "poketoken-save", "version": 1, "state": "nope"})}
        expect = {"notjson.json": "not valid JSON", "list.json": "top level is not a JSON object", "format.json": "format is 'x'",
                  "version.json": "version 99 is not supported", "state.json": "'state' is missing or not an object"}
        for name, text in cases.items():
            (self.tmp / name).write_text(text)
            rc, out = self.import_(str(self.tmp / name), "--replace", state=self.a)
            self.assertEqual(rc, 1, name)
            self.assertIn("is not a poketoken export: " + expect[name], out)
        rc, out = self.import_(str(self.tmp / "absent.json"), state=self.a)
        self.assertEqual(rc, 1)
        self.assertIn("cannot read", out)
        self.assertEqual(self.state(self.a), STATE)
        self.assertEqual(self.backups(self.a), [])

    def test_refuses_while_window_runs(self):
        (self.tmp / "e.json").write_text(json.dumps(service.export_envelope(dict(STATE, usedSinceInstall=1))))
        instance.write_pid(self.a)                                   # our own pid: alive and "poketoken" by definition
        self.addCleanup(instance.clear_pid, self.a)
        rc, out = self.import_(str(self.tmp / "e.json"), "--replace", state=self.a)
        self.assertEqual(rc, 1)
        self.assertIn("window is running", out)
        self.assertIn("poketoken close", out)
        self.assertEqual(self.state(self.a), STATE)
        self.assertEqual(self.backups(self.a), [])
        with self.assertRaises(service.ImportRefused):
            service.import_state(self.a, {}, replace=True)
        fresh = self.tmp / "fresh"
        fresh.mkdir()
        instance.write_pid(fresh)
        self.addCleanup(instance.clear_pid, fresh)
        with self.assertRaises(service.ImportRefused):
            service.import_state(fresh, {})
        self.assertFalse((fresh / "state.json").exists())

    def test_rollback_when_written_save_does_not_load(self):
        with mock.patch.object(service.C.CompanionState, "from_dict", mock.Mock(side_effect=TypeError("bad"))):
            with self.assertRaises(service.ImportRefused) as cm:
                service.import_state(self.a, {"x": 1}, replace=True)
            self.assertIn("previous save restored", str(cm.exception))
            fresh = self.tmp / "fresh"
            fresh.mkdir()
            with self.assertRaises(service.ImportRefused):
                service.import_state(fresh, {"x": 1})
        self.assertEqual(self.state(self.a), STATE)
        self.assertEqual(self.backups(self.a), [])
        self.assertFalse((fresh / "state.json").exists())

    def test_import_state_direct(self):
        b = self.tmp / "b"
        self.assertIsNone(service.import_state(b, STATE))
        self.assertEqual(self.state(b), STATE)
        with self.assertRaises(service.ImportRefused) as cm:
            service.import_state(b, {})
        self.assertIn("--replace", str(cm.exception))
        bak = service.import_state(b, {"usedSinceInstall": 1}, replace=True, now=0)
        self.assertEqual(bak, service.backup_path(b / "state.json", now=0))
        self.assertEqual(json.loads(bak.read_text()), STATE)
        self.assertEqual(self.state(b), {"usedSinceInstall": 1})


if __name__ == "__main__":
    unittest.main()
