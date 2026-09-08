"""Keep the game advancing when the window is closed, and move the save between machines.

Three features, all built the same way: a pure function produces the text (systemd units,
Startup .vbs, XDG autostart entry, export envelope) and a thin wrapper touches the file system
or calls `systemctl`, so tests assert on the text and mock the wrappers.

* `timer`     — a systemd *user* timer that runs `poketoken refresh` every 15 minutes. Streaks,
                Rare Candy grants and hatching advance while the window is closed, and the
                pre-midnight gap closes: tokens burned after the last refresh of a day are
                never credited once the date rolls over, so a tick at most 15 minutes before
                midnight keeps that loss small.
* `autostart` — open the window at sign-in: a .vbs in the Windows Startup folder (WSL and
                native Windows) or `~/.config/autostart/poketoken.desktop` (Linux desktops).
* `export` / `import` — a JSON envelope around `state.json`, with backups on replace.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from . import companion as C, fmt, instance
from .notify import is_wsl
from .pokeapi import EvoLine

SERVICE = "poketoken-refresh.service"
TIMER = "poketoken-refresh.timer"
REFRESH_EVERY = "15min"
REFRESH_AFTER_BOOT = "2min"
STARTUP_SUBDIR = ("Microsoft", "Windows", "Start Menu", "Programs", "Startup")
VBS_NAME = "PokeToken.vbs"
DESKTOP_NAME = "poketoken.desktop"
WSL_POKETOKEN = "$HOME/.local/bin/poketoken"
EXPORT_FORMAT = "poketoken-save"
EXPORT_VERSION = 1
STATE_FILE = "state.json"


def repo_root() -> Path:
    """The checkout `python -m poketoken` must run from (the package's parent)."""
    return Path(__file__).resolve().parents[1]


def _config_home() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    return Path(xdg).expanduser() if xdg else Path.home() / ".config"


# ------------------------------------------------------------------ systemd user timer
def systemd_user_dir() -> Path:
    return _config_home() / "systemd" / "user"


def _unit_arg(s: str) -> str:
    """One ExecStart argument: shell-style quoting (systemd accepts it) with `%` doubled."""
    return shlex.quote(s).replace("%", "%%")


def unit_files(state_dir: Path, python: str, repo_root: Path) -> dict[str, str]:
    """Text of the oneshot service and its timer, keyed by unit file name."""
    exec_start = " ".join(_unit_arg(a) for a in (python, "-m", "poketoken", "--state-dir", str(state_dir), "refresh"))
    service = (
        "[Unit]\n"
        "Description=PokeToken refresh tick (advances the companion while the window is closed)\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"WorkingDirectory={str(repo_root).replace('%', '%%')}\n"
        f"ExecStart={exec_start}\n"
    )
    timer = (
        "[Unit]\n"
        f"Description=PokeToken refresh every {REFRESH_EVERY}\n"
        "\n"
        "[Timer]\n"
        f"OnBootSec={REFRESH_AFTER_BOOT}\n"
        f"OnUnitActiveSec={REFRESH_EVERY}\n"
        "Persistent=true\n"
        f"Unit={SERVICE}\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    return {SERVICE: service, TIMER: timer}


def systemctl(*args: str, timeout: float = 30) -> subprocess.CompletedProcess:
    """`systemctl --user <args>`; never raises, a missing binary reads as exit 127."""
    try:
        return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return subprocess.CompletedProcess(["systemctl", "--user", *args], 127, "", str(e))


def has_systemd_user() -> bool:
    """True when a systemd user manager answers (`is-system-running` says running or degraded)."""
    if os.name == "nt" or not shutil.which("systemctl"):
        return False
    r = systemctl("is-system-running", timeout=5)
    return r.stdout.strip() in ("running", "degraded")


def cron_hint(state_dir: Path, python: str = sys.executable, root: Path | None = None) -> str:
    """What to paste into `crontab -e` (or Task Scheduler on Windows) when there is no systemd."""
    cmd = f"{shlex.quote(python)} -m poketoken --state-dir {shlex.quote(str(state_dir))} refresh"
    if os.name == "nt":
        return (f'schtasks /Create /SC MINUTE /MO 15 /TN PokeTokenRefresh /TR "cmd /c cd /d '
                f'{root or repo_root()} && {cmd}"')
    return f"*/15 * * * * cd {shlex.quote(str(root or repo_root()))} && {cmd} >/dev/null 2>&1"


def timer_on(state_dir: Path, python: str = sys.executable, root: Path | None = None,
             unit_dir: Path | None = None) -> tuple[list[Path], subprocess.CompletedProcess]:
    """Write both units, reload, `enable --now` the timer. Returns (paths written, enable result)."""
    unit_dir = unit_dir or systemd_user_dir()
    unit_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, text in unit_files(state_dir, python, root or repo_root()).items():
        (unit_dir / name).write_text(text, "utf-8")
        written.append(unit_dir / name)
    systemctl("daemon-reload")
    return written, systemctl("enable", "--now", TIMER)


def timer_off(unit_dir: Path | None = None) -> list[Path]:
    """`disable --now`, remove the unit files, reload. Returns the files that were removed."""
    unit_dir = unit_dir or systemd_user_dir()
    systemctl("disable", "--now", TIMER)
    removed = []
    for name in (TIMER, SERVICE):
        p = unit_dir / name
        if p.exists():
            p.unlink()
            removed.append(p)
    systemctl("daemon-reload")
    return removed


def _show(unit: str, *props: str) -> dict[str, str]:
    r = systemctl("show", unit, "-p", ",".join(props))
    out = {}
    for line in r.stdout.splitlines():
        k, _, v = line.partition("=")
        if k:
            out[k.strip()] = v.strip()
    return out


def timer_status(unit_dir: Path | None = None) -> dict:
    """Unit files present, timer load/active state, last and next trigger, last journal line."""
    unit_dir = unit_dir or systemd_user_dir()
    t = _show(TIMER, "LoadState", "ActiveState", "LastTriggerUSec", "NextElapseUSecRealtime")
    s = _show(SERVICE, "ActiveState", "Result")
    last_log = ""
    try:
        j = subprocess.run(["journalctl", "--user", "-u", SERVICE, "-n", "1", "--no-pager", "-o", "cat"],
                           capture_output=True, text=True, timeout=10)
        last_log = j.stdout.strip() if j.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        pass
    return {"unit_dir": unit_dir, "files": {n: (unit_dir / n).exists() for n in (SERVICE, TIMER)},
            "loaded": t.get("LoadState") == "loaded", "active": t.get("ActiveState") == "active",
            "last_run": t.get("LastTriggerUSec") or "", "next_run": t.get("NextElapseUSecRealtime") or "",
            "service_result": s.get("Result") or "", "last_log": last_log}


# ------------------------------------------------------------------ autostart
class Unsupported(Exception):
    """No autostart mechanism could be located on this system."""


def wsl_distro() -> str:
    return os.environ.get("WSL_DISTRO_NAME") or "Ubuntu"


def windows_appdata() -> Path | None:
    """%APPDATA% as a path usable from here: straight from the environment on Windows, via
    `cmd.exe` + `wslpath` under WSL. None when Windows is not reachable."""
    if os.name == "nt":
        v = os.environ.get("APPDATA")
        return Path(v) if v else None
    try:
        # cwd on the Windows side: cmd.exe complains about (but tolerates) a UNC working directory
        r = subprocess.run(["cmd.exe", "/c", "echo %APPDATA%"], capture_output=True, text=True, timeout=15,
                           cwd="/mnt/c" if Path("/mnt/c").is_dir() else None)
        win = r.stdout.strip()
        if r.returncode != 0 or not win or "%" in win:
            return None
        w = subprocess.run(["wslpath", "-u", win], capture_output=True, text=True, timeout=5)
        return Path(w.stdout.strip()) if w.returncode == 0 and w.stdout.strip() else None
    except (OSError, subprocess.SubprocessError):
        return None


def startup_dir(appdata: Path) -> Path:
    return appdata.joinpath(*STARTUP_SUBDIR)


def autostart_target() -> tuple[str, Path]:
    """('wsl' | 'windows' | 'linux', path of the autostart file)."""
    if os.name == "nt":
        appdata = windows_appdata()
        if appdata is None:
            raise Unsupported("%APPDATA% is not set")
        return "windows", startup_dir(appdata) / VBS_NAME
    if is_wsl():
        appdata = windows_appdata()
        if appdata is None:
            raise Unsupported("cannot resolve the Windows Startup folder (cmd.exe / wslpath failed)")
        return "wsl", startup_dir(appdata) / VBS_NAME
    return "linux", _config_home() / "autostart" / DESKTOP_NAME


def _vbs_str(s: str) -> str:
    """A VBScript string literal."""
    return '"' + s.replace('"', '""') + '"'


def vbs_wsl_text(distro: str, poketoken: str = WSL_POKETOKEN) -> str:
    """Startup script for WSL: `poketoken app` through wsl.exe in a login shell, no console flash."""
    cmd = f'wsl.exe -d {distro} -- bash -lc "{poketoken} app"'
    return ("' PokeToken — opens the companion window at sign-in (written by `poketoken autostart on`;\n"
            "' remove it with `poketoken autostart off`). Needs WSLg and scripts/install.sh run in the distro.\n"
            'Set sh = CreateObject("WScript.Shell")\n'
            f"sh.Run {_vbs_str(cmd)}, 0, False\n")


def vbs_windows_text(python: str, repo_root: Path, state_dir: Path) -> str:
    """Startup script for native Windows: run the interpreter directly from the checkout."""
    cmd = f'"{python}" -m poketoken --state-dir "{state_dir}" app'
    return ("' PokeToken — opens the companion window at sign-in (written by `poketoken autostart on`;\n"
            "' remove it with `poketoken autostart off`).\n"
            'Set sh = CreateObject("WScript.Shell")\n'
            f"sh.CurrentDirectory = {_vbs_str(str(repo_root))}\n"
            f"sh.Run {_vbs_str(cmd)}, 0, False\n")


def _desktop_arg(s: str) -> str:
    """One Exec argument per the desktop-entry spec: double-quoted when needed, `%` doubled."""
    s = s.replace("%", "%%")
    if any(ch in s for ch in ' \t"\'\\><~|&;$*?#()`'):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$") + '"'
    return s


def desktop_text(python: str, repo_root: Path, state_dir: Path) -> str:
    """XDG autostart entry for Linux desktops."""
    exec_line = " ".join(_desktop_arg(a) for a in (python, "-m", "poketoken", "--state-dir", str(state_dir), "app"))
    return ("[Desktop Entry]\n"
            "Type=Application\n"
            "Name=PokeToken\n"
            "Comment=Your Claude Code tokens, hatched into Pokémon\n"
            f"Exec={exec_line}\n"
            f"Path={repo_root}\n"
            "Terminal=false\n"
            "X-GNOME-Autostart-enabled=true\n")


def autostart_text(flavour: str, state_dir: Path, python: str = sys.executable, root: Path | None = None,
                   distro: str | None = None) -> str:
    root = root or repo_root()
    if flavour == "wsl":
        return vbs_wsl_text(distro or wsl_distro())
    if flavour == "windows":
        return vbs_windows_text(python, root, state_dir)
    return desktop_text(python, root, state_dir)


def autostart_on(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, "utf-8")


def autostart_off(path: Path) -> bool:
    """Remove the autostart file; False when there was none."""
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


# ------------------------------------------------------------------ export / import
class ImportRefused(Exception):
    """The import was not performed; the message says why and what to do."""


def default_export_path(today: date | None = None) -> Path:
    return Path(f"poketoken-save-{(today or date.today()).isoformat()}.json")


def export_envelope(state: dict, now: datetime | None = None, host: str | None = None) -> dict:
    return {"format": EXPORT_FORMAT, "version": EXPORT_VERSION,
            "exportedAt": (now or datetime.now().astimezone()).isoformat(timespec="seconds"),
            "host": host or socket.gethostname(), "state": state}


def validate_envelope(data) -> dict:
    """The `state` object of a valid envelope; ValueError says what is wrong otherwise."""
    if not isinstance(data, dict):
        raise ValueError("top level is not a JSON object")
    if data.get("format") != EXPORT_FORMAT:
        raise ValueError(f"format is {data.get('format')!r}, expected {EXPORT_FORMAT!r}")
    if data.get("version") != EXPORT_VERSION:
        raise ValueError(f"version {data.get('version')!r} is not supported (this build reads {EXPORT_VERSION})")
    state = data.get("state")
    if not isinstance(state, dict):
        raise ValueError("'state' is missing or not an object")
    return state


def read_envelope(path: Path) -> tuple[dict, dict]:
    """(envelope, state) from an export file; ValueError for anything that is not a valid envelope."""
    try:
        data = json.loads(Path(path).read_text("utf-8"))
    except ValueError as e:
        raise ValueError(f"not valid JSON: {e}") from None
    return data, validate_envelope(data)


def read_state(state_dir: Path) -> dict | None:
    """The raw `state.json` object, or None when there is no (readable) save."""
    try:
        data = json.loads((Path(state_dir) / STATE_FILE).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_state(path: Path, state: dict) -> None:
    """Atomic write (tmp + os.replace), the same way Companion.save does it."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), "utf-8")
    os.replace(tmp, path)


def cached_name(cache_dir: Path, base_id: int, sid: int, lang: str = "en") -> str:
    """Species name from the on-disk line cache only (no network); `#id` when not cached."""
    try:
        line = EvoLine.from_dict(json.loads((Path(cache_dir) / "lines" / f"{base_id}.json").read_text("utf-8")))
        return line.name(sid, lang)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return f"#{sid}"


def save_summary(state: dict, name: Callable[[int, int], str] | None = None) -> str:
    """One line about a raw save: the Pokémon (or egg), Pokédex size, lifetime tokens."""
    s = C.CompanionState.from_dict(state)
    a = s.active
    if a is None:
        who = f"egg {fmt.percent(min(1.0, s.egg_usage / C.EGG_HATCH_THRESHOLD) * 100)}"
    else:
        label = name(a.base_id, a.current_id) if name else f"#{a.current_id}"
        who = f"{label}{' ✨' if a.shiny_visible else ''} {a.rarity} stage {a.stage_index + 1}/{a.total_forms}"
    grads = sum(1 for e in s.dex if not e.is_released)
    return (f"{who} · Pokédex {grads} graduated ({len(s.dex)} entries) · "
            f"{fmt.compact(s.used_since_install)} tokens since install · {len(s.history)} days of history")


def backup_path(state_path: Path, now: float | None = None) -> Path:
    stamp = datetime.fromtimestamp(now if now is not None else time.time()).strftime("%Y%m%d-%H%M%S")
    return state_path.with_name(f"{state_path.name}.bak-{stamp}")


def import_state(state_dir: Path, state: dict, replace: bool = False, now: float | None = None) -> Path | None:
    """Install `state` as `<state_dir>/state.json`. Returns the backup made of the previous save
    (None when there was none). Raises ImportRefused instead of touching anything when a window is
    running, or when a save exists and `replace` is False."""
    state_dir = Path(state_dir)
    target = state_dir / STATE_FILE
    if instance.running_pid(state_dir) is not None:
        raise ImportRefused("the PokeToken window is running and would overwrite the imported save on its next "
                            "refresh — close it first:  poketoken close")
    if target.exists() and not replace:
        raise ImportRefused(f"{target} already holds a save; add --replace to overwrite it (a backup is kept)")
    backup = None
    if target.exists():
        backup = backup_path(target, now)
        shutil.copy2(target, backup)
    state_dir.mkdir(parents=True, exist_ok=True)
    write_state(target, state)
    try:
        C.CompanionState.from_dict(json.loads(target.read_text("utf-8")))
    except Exception as e:  # noqa: BLE001 — anything: the written save must load, else roll back
        if backup is not None:
            os.replace(backup, target)
        else:
            target.unlink(missing_ok=True)
        raise ImportRefused(f"imported save does not load ({e}); previous save restored") from None
    return backup
