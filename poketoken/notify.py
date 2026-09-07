"""Desktop notifications for companion events (hatch, evolve, graduate, candy, egg).

Best effort and fire-and-forget: `send` never raises and returns within a few hundred
milliseconds; the backend process is detached and its output is appended to
`<state_dir>/notify.log`. Backends, in order: `notify-send` when it is on PATH (Linux
desktops), a Windows toast through powershell.exe (WSL and native Windows), else nothing.
The on/off switch lives in `<state_dir>/settings.json` under the key "notifications".
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape
from . import settings

SETTINGS_FILE = "settings.json"
LOG_FILE = "notify.log"
APP_NAME = "PokeToken"
# powershell.exe's own AppUserModelID: Windows already has a Start-menu entry for it, so a toast
# from an unregistered script is shown instead of silently dropped.
TOAST_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
WSL_POWERSHELL = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
SCRIPT_PREFIX = "toast-"
SCRIPT_MAX_AGE = 120            # seconds before a spent toast script is deleted

# Written as UTF-8 with BOM (Windows PowerShell 5.1 reads BOM-less files as ANSI). The payload is
# a literal here-string, so nothing in title/body is interpolated; both are XML-escaped one-liners.
TOAST_SCRIPT = """$ErrorActionPreference = 'Stop'
[void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
[void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]
$appId = '{app_id}'
$payload = @'
<toast duration="short"><visual><binding template="ToastGeneric"><text>{title}</text><text>{body}</text></binding></visual></toast>
'@
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($payload)
$toast = New-Object Windows.UI.Notifications.ToastNotification $xml
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
Write-Output 'toast shown'
"""


# ---------------------------------------------------------------- settings
def settings_path(state_dir: Path | str) -> Path:
    return settings.path(Path(state_dir))


def load_settings(state_dir: Path | str) -> dict:
    """The shared settings dict (see settings.py); empty when missing or unreadable."""
    return settings.load(Path(state_dir))


def save_settings(state_dir: Path | str, data: dict) -> None:
    settings.save(Path(state_dir), data)


def enabled(state_dir: Path | str) -> bool:
    return bool(load_settings(state_dir).get("notifications", True))


def set_enabled(state_dir: Path | str, on: bool) -> None:
    settings.set(Path(state_dir), "notifications", bool(on))


# ---------------------------------------------------------------- backends
def is_wsl() -> bool:
    if Path("/mnt/wslg").is_dir():
        return True
    try:
        return "microsoft" in Path("/proc/version").read_text("utf-8", errors="replace").lower()
    except OSError:
        return False


def powershell() -> str | None:
    """Path to powershell.exe when a Windows desktop is reachable (native, or through WSL interop)."""
    if os.name == "nt":
        root = os.environ.get("SystemRoot") or r"C:\Windows"
        return shutil.which("powershell.exe") or rf"{root}\System32\WindowsPowerShell\v1.0\powershell.exe"
    if is_wsl():
        return WSL_POWERSHELL if Path(WSL_POWERSHELL).exists() else shutil.which("powershell.exe")
    return None


def backend() -> str | None:
    """'notify-send', 'toast', or None when nothing here can show a notification."""
    if shutil.which("notify-send"):
        return "notify-send"
    if powershell():
        return "toast"
    return None


def windows_path(path: Path) -> str | None:
    """How powershell.exe should spell `path`: as-is on Windows, a \\\\wsl.localhost UNC path from WSL."""
    if os.name == "nt":
        return str(path)
    try:
        r = subprocess.run(["wslpath", "-w", str(path)], capture_output=True, text=True, timeout=2)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    distro = os.environ.get("WSL_DISTRO_NAME")
    return rf"\\wsl.localhost\{distro}" + str(path).replace("/", "\\") if distro else None


def _line(s: str) -> str:
    return " ".join(str(s).split())


def toast_script(title: str, body: str) -> str:
    return TOAST_SCRIPT.format(app_id=TOAST_APP_ID, title=escape(_line(title)), body=escape(_line(body)))


def _prune_scripts(state_dir: Path, now: float) -> None:
    """Delete toast scripts old enough that their PowerShell has long finished reading them."""
    for p in state_dir.glob(f"{SCRIPT_PREFIX}*.ps1"):
        try:
            if now - p.stat().st_mtime > SCRIPT_MAX_AGE:
                p.unlink()
        except OSError:
            pass


def _log(state_dir: Path, msg: str) -> None:
    try:
        with open(state_dir / LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} {msg}\n")
    except OSError:
        pass


def command(title: str, body: str, state_dir: Path) -> tuple[str, list[str]] | None:
    """(backend, argv) for this machine, or None. The toast backend gets its own script file:
    text travels through the file, never through cmd/wsl quoting."""
    be = backend()
    if be == "notify-send":
        return be, ["notify-send", f"--app-name={APP_NAME}", _line(title), _line(body)]
    if be == "toast":
        state_dir.mkdir(parents=True, exist_ok=True)
        _prune_scripts(state_dir, time.time())
        script = state_dir / f"{SCRIPT_PREFIX}{time.time_ns()}.ps1"
        script.write_text(toast_script(title, body), encoding="utf-8-sig")
        win = windows_path(script)
        if not win:
            script.unlink(missing_ok=True)
            return None
        return be, [powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", win]
    return None


def send(title: str, body: str, state_dir: Path | str, wait: float = 0.0) -> bool:
    """Show a desktop notification. Never raises; True when a backend took the request.

    Fire-and-forget by default: the backend runs detached (`start_new_session`) with its output
    appended to notify.log and the call returns well within a second. With `wait` > 0 the process
    gets that many seconds to finish and its exit code decides the result.
    """
    state_dir = state_dir if isinstance(state_dir, Path) else Path(state_dir)
    try:
        cmd = command(title, body, state_dir)
        if cmd is None:
            _log(state_dir, f"no backend for: {_line(title)}")
            return False
        be, argv = cmd
        _log(state_dir, f"{be}: {_line(title)} | {_line(body)}")
        kw = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os.name == "nt" else {}
        with open(state_dir / LOG_FILE, "a", encoding="utf-8") as log:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                    start_new_session=True, **kw)
        if wait <= 0:
            return True
        try:
            rc = proc.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            _log(state_dir, f"{be}: still running after {wait:g}s")
            return True
        _log(state_dir, f"{be}: exit {rc}")
        return rc == 0
    except Exception as e:  # noqa: BLE001 — a notification must never take the app down
        _log(state_dir, f"notify failed: {e!r}")
        return False


# ---------------------------------------------------------------- events
def event_text(ev: dict) -> tuple[str, str] | None:
    """(title, body) for an event worth announcing; None for the quiet kinds (buy, mint)."""
    kind, name = ev.get("kind"), ev.get("name") or "Your Pokémon"
    if kind == "hatch":
        detail = [ev.get("rarity") or "", f"{ev['nature'].title()} nature" if ev.get("nature") else ""]
        return f"{'Shiny ' if ev.get('shiny') else ''}{name} hatched!", " · ".join(d for d in detail if d)
    if kind == "evolve":
        return f"Evolved into {name}!", f"Stage {ev['stage']}" if ev.get("stage") else ""
    if kind == "graduate":
        return f"{name} joined the Pokédex", ("Shiny! " if ev.get("shiny") else "") + "A new egg is incubating."
    if kind == "candy":
        return f"+{ev.get('count', 1)} Rare Candy", str(ev.get("reason") or "")
    if kind == "egg":
        tier = ev.get("tier") or "plain"
        detail = [f"{ev['released']} was released." if ev.get("released") else "",
                  f"Guaranteed {tier} or better." if tier != "plain" else ""]
        return "A new egg arrived", " ".join(d for d in detail if d)
    return None


def notify_event(ev: dict, state_dir: Path | str) -> bool:
    """Announce a companion event on the desktop when notifications are on and the kind warrants it."""
    text = event_text(ev)
    if text is None or not enabled(state_dir):
        return False
    return send(text[0], text[1], state_dir)
