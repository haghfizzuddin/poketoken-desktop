"""Small persisted settings dict (`<state_dir>/settings.json`), shared by features that need a
user preference outside the game save: trainer name, notifications, …"""
from __future__ import annotations

import json
from pathlib import Path


def path(state_dir: Path) -> Path:
    return Path(state_dir) / "settings.json"


def load(state_dir: Path) -> dict:
    try:
        d = json.loads(path(state_dir).read_text("utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def get(state_dir: Path, key: str, default=None):
    return load(state_dir).get(key, default)


def set(state_dir: Path, key: str, value) -> None:  # noqa: A001 — mirrors dict.get/set naming
    d = load(state_dir)
    d[key] = value
    p = path(state_dir)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(p)
