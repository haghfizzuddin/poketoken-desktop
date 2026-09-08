"""Single-instance bookkeeping for the window.

A pid file says whether a window is running; a one-line command file lets the CLI talk to
it (`quit`, `raise`). The window reads the command file on its 150 ms poll, so no signals
are needed and the same code works on POSIX and Windows.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

STILL_ACTIVE = 259


def pid_file(state_dir: Path) -> Path:
    return Path(state_dir) / "app.pid"


def cmd_file(state_dir: Path) -> Path:
    return Path(state_dir) / "app.cmd"


def log_file(state_dir: Path) -> Path:
    return Path(state_dir) / "app.log"


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k32 = ctypes.windll.kernel32                       # type: ignore[attr-defined]
        handle = k32.OpenProcess(0x1000, False, pid)       # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(handle, ctypes.byref(code))
        k32.CloseHandle(handle)
        return bool(ok) and code.value == STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _is_poketoken(pid: int) -> bool:
    """Guard against pid reuse: on Linux confirm the process really is poketoken (or us)."""
    if pid == os.getpid():
        return True
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return True                                    # no /proc (Windows, macOS): trust the pid
    return b"poketoken" in cmdline


def running_pid(state_dir: Path) -> int | None:
    """Pid of the live window, or None (a stale pid file is removed)."""
    f = pid_file(state_dir)
    try:
        pid = int(f.read_text().strip())
    except (OSError, ValueError):
        return None
    if pid_alive(pid) and _is_poketoken(pid):
        return pid
    try:
        f.unlink()
    except OSError:
        pass
    return None


def write_pid(state_dir: Path) -> None:
    pid_file(state_dir).write_text(str(os.getpid()))


def clear_pid(state_dir: Path) -> None:
    f = pid_file(state_dir)
    try:
        if f.read_text().strip() == str(os.getpid()):
            f.unlink()
    except OSError:
        pass


def send(state_dir: Path, command: str) -> bool:
    """Queue a command for the running window. False if no window is running."""
    if running_pid(state_dir) is None:
        return False
    cmd_file(state_dir).write_text(command.strip() + "\n")
    return True


def take_command(state_dir: Path) -> str | None:
    f = cmd_file(state_dir)
    try:
        cmd = f.read_text().strip()
    except OSError:
        return None
    try:
        f.unlink()
    except OSError:
        pass
    return cmd or None


def spawn_detached(argv: list[str], state_dir: Path) -> int:
    """Start `python -m poketoken <argv>` in its own session with output in app.log."""
    log = open(log_file(state_dir), "ab")  # noqa: SIM115 — handed to the child
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen([sys.executable, "-m", "poketoken", *argv], stdin=subprocess.DEVNULL,
                            stdout=log, stderr=log, cwd=str(Path(__file__).resolve().parents[1]), **kwargs)
    log.close()
    return proc.pid


SUPERVISED_ENV = "POKETOKEN_SUPERVISED"     # set on the window when a supervisor owns the pid file


def probe_display(timeout: float = 5.0) -> bool:
    """Whether a Tk window can be opened right now. Runs in a subprocess so a hung X server
    (WSLg's Xwayland after a lock-screen reconnect) cannot hang us: the connect blocks, the
    child is killed after `timeout`, and the answer is no."""
    code = "import tkinter as tk\nr = tk.Tk()\nr.withdraw()\nr.update()\nr.destroy()\n"
    try:
        p = subprocess.run([sys.executable, "-c", code], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return p.returncode == 0


def supervise(state_dir: Path, spawn: Callable[[], subprocess.Popen], probe: Callable[[], bool],
              log: Callable[[str], None], sleep: Callable[[float], None] = time.sleep,
              waits: tuple = (2, 5, 10, 20, 30), quick: float = 10.0, max_quick: int = 5,
              clock: Callable[[], float] = time.monotonic) -> int:
    """Keep the window alive across display crashes. The supervisor owns the pid file and runs
    the window as a child (with SUPERVISED_ENV set), because when the X server dies under a Tk
    window Xlib exits the process on the spot — nothing inside it can recover. Here we notice
    the death, wait until `probe` says the display answers again, and open the window anew.
    A clean exit (the user closed it, or `quit` arrived while waiting) ends the loop; so do
    `max_quick` deaths in a row within `quick` seconds of starting, which is a bug, not a lost
    display. Returns the exit code to report."""
    write_pid(state_dir)
    child: subprocess.Popen | None = None
    stop: list[bool] = []
    if os.name != "nt":
        import signal

        def on_term(*_) -> None:
            stop.append(True)
            if child is not None and child.poll() is None:
                child.terminate()
        signal.signal(signal.SIGTERM, on_term)
    quick_deaths = 0
    try:
        while not stop:
            started = clock()
            child = spawn()
            rc = child.wait()
            if stop or rc == 0:
                return 0
            quick_deaths = quick_deaths + 1 if clock() - started < quick else 0
            if quick_deaths >= max_quick:
                log(f"window died {max_quick} times in a row (exit {rc}); giving up")
                return rc
            log(f"window died (exit {rc}); waiting for the display to come back")
            i = 0
            while not stop:
                if take_command(state_dir) == "quit":
                    return 0
                if probe():
                    break
                sleep(waits[min(i, len(waits) - 1)])
                i += 1
    finally:
        clear_pid(state_dir)
    return 0


def wait_for(pred: Callable[[], bool], timeout: float, step: float = 0.1) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(step)
    return pred()
