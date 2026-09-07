"""State directory resolution.

Honours `POKETOKEN_STATE_DIR` (or upstream's `PTB_STATE_DIR`) for isolation; otherwise
`$XDG_DATA_HOME/poketoken` (default `~/.local/share/poketoken`), or `%LOCALAPPDATA%\poketoken` on Windows.
"""
from __future__ import annotations

import os
from pathlib import Path


def state_dir() -> Path:
    override = (os.environ.get("POKETOKEN_STATE_DIR") or os.environ.get("PTB_STATE_DIR") or "").strip()
    if override:
        d = Path(override).expanduser()
    elif os.name == "nt":
        d = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "poketoken"
    else:
        xdg = os.environ.get("XDG_DATA_HOME", "").strip()
        base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
        d = base / "poketoken"
    for sub in ("", "sprites", "cache", "cache/species", "cache/lines"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    return d
