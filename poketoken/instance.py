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


def running_pid(state_dir: Path) -> int | None:
    """Pid of the live window, or None (a stale pid file is removed)."""
    f = pid_file(state_dir)
    try:
        pid = int(f.read_text().strip())
    except (OSError, ValueError):
        return None
    if pid_alive(pid):
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


def wait_for(pred: Callable[[], bool], timeout: float, step: float = 0.1) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(step)
    return pred()
