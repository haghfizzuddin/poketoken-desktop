"""Claude Code JSONL usage reader.

Port of the Claude path in upstream `LocalUsageReader.swift`:
- one Entry per `type:"assistant"` line carrying `message.usage`
- global dedup on `(message.id, requestId)` keeping the entry with the largest total
  (streaming/resume re-logs the same message with growing output)
- local-day bucketing, 5-hour rolling "active block" for burn rate

Adds an incremental reader: files are re-read from their last offset (logs are
append-only), so a refresh after the cold scan is cheap.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Iterator

from . import pricing

BLOCK_WINDOW = 5 * 3600
PROVIDER_ID = "claude_code"


@dataclass(slots=True)
class Entry:
    id: str
    ts: float            # epoch seconds
    local_day: str       # yyyy-MM-dd in local time
    model: str
    input: int
    output: int
    cache_write_5m: int
    cache_write_1h: int
    cache_read: int

    @property
    def cache_write(self) -> int:
        return self.cache_write_5m + self.cache_write_1h

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_write + self.cache_read

    @property
    def cost(self) -> float:
        return pricing.cost(self.model, self.input, self.output,
                            self.cache_write_5m, self.cache_write_1h, self.cache_read)


def _int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _iso_epoch(s: str) -> float | None:
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def parse_line(line: str) -> Entry | None:
    if '"usage"' not in line or '"assistant"' not in line:
        return None
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    if not isinstance(obj, dict) or obj.get("type") != "assistant":
        return None
    msg = obj.get("message")
    if not isinstance(msg, dict):
        return None
    usage = msg.get("usage")
    if not isinstance(usage, dict):
        return None
    ts = obj.get("timestamp")
    if not isinstance(ts, str):
        return None
    epoch = _iso_epoch(ts)
    if epoch is None:
        return None
    cw_total = _int(usage.get("cache_creation_input_tokens"))
    cc = usage.get("cache_creation")
    cw1 = min(cw_total, _int(cc.get("ephemeral_1h_input_tokens"))) if isinstance(cc, dict) else 0
    model = msg.get("model") if isinstance(msg.get("model"), str) else "unknown"
    return Entry(
        id=f"{msg.get('id') or ''}|{obj.get('requestId') or ''}",
        ts=epoch,
        local_day=datetime.fromtimestamp(epoch).strftime("%Y-%m-%d"),
        model=model,
        input=_int(usage.get("input_tokens")),
        output=_int(usage.get("output_tokens")),
        cache_write_5m=cw_total - cw1,
        cache_write_1h=cw1,
        cache_read=_int(usage.get("cache_read_input_tokens")),
    )


def claude_project_roots() -> list[Path]:
    """`CLAUDE_CONFIG_DIR` (comma-separated, `projects` appended) or the two defaults,
    plus any extra roots from `POKETOKEN_SCAN_ROOTS`."""
    roots: list[Path] = []
    env = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if env:
        for part in env.split(","):
            part = part.strip()
            if part:
                roots.append(Path(part).expanduser() / "projects")
    else:
        home = Path.home()
        roots += [home / ".claude" / "projects", home / ".config" / "claude" / "projects"]
    for part in os.environ.get("POKETOKEN_SCAN_ROOTS", "").split(","):
        part = part.strip()
        if part:
            roots.append(Path(part).expanduser())
    seen: set[str] = set()
    out: list[Path] = []
    for r in roots:
        if not r.is_dir():
            continue
        key = str(r.resolve())
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def dedup_keep_max(entries: Iterable[Entry]) -> list[Entry]:
    by: dict[str, Entry] = {}
    for e in entries:
        ex = by.get(e.id)
        if ex is None or e.total > ex.total:
            by[e.id] = e
    return list(by.values())


@dataclass
class _FileState:
    size: int = 0
    mtime: float = 0.0
    offset: int = 0
    partial: bytes = b""
    entries: dict[str, Entry] = field(default_factory=dict)


class UsageReader:
    """Incremental scanner over all *.jsonl under the project roots."""

    def __init__(self, roots: list[Path] | None = None):
        self.roots = list(roots) if roots is not None else claude_project_roots()
        self._files: dict[str, _FileState] = {}
        self.last_scan_files = 0
        self.last_scan_lines = 0

    def _iter_jsonl(self) -> Iterator[str]:
        for root in self.roots:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for fn in filenames:
                    if fn.endswith(".jsonl") and not fn.startswith("."):
                        yield os.path.join(dirpath, fn)

    def scan(self, modified_since: float) -> list[Entry]:
        live: set[str] = set()
        parsed = 0
        for path in self._iter_jsonl():
            try:
                st = os.stat(path)
            except OSError:
                continue
            if st.st_mtime < modified_since:
                continue
            live.add(path)
            fs = self._files.get(path)
            if fs is not None and fs.size == st.st_size and fs.mtime == st.st_mtime:
                continue
            if fs is None or st.st_size < fs.size:      # new or truncated/rewritten
                fs = _FileState()
                self._files[path] = fs
            parsed += self._read_from(path, fs)
            fs.size, fs.mtime = st.st_size, st.st_mtime
        for gone in set(self._files) - live:
            del self._files[gone]
        self.last_scan_files = len(live)
        self.last_scan_lines = parsed
        return dedup_keep_max(e for fs in self._files.values() for e in fs.entries.values())

    def _read_from(self, path: str, fs: _FileState) -> int:
        try:
            with open(path, "rb") as f:
                f.seek(fs.offset)
                data = f.read()
        except OSError:
            return 0
        fs.offset += len(data)
        lines = (fs.partial + data).split(b"\n")
        fs.partial = lines.pop()          # incomplete tail; b"" if data ended on a newline
        n = 0
        for raw in lines:
            if b'"usage"' not in raw or b'"assistant"' not in raw:
                continue
            e = parse_line(raw.decode("utf-8", "replace"))
            if e is None:
                continue
            n += 1
            ex = fs.entries.get(e.id)
            if ex is None or e.total > ex.total:
                fs.entries[e.id] = e
        return n


# ---------------------------------------------------------------- aggregation

@dataclass
class Bucket:
    input: int = 0
    output: int = 0
    cache_write: int = 0
    cache_read: int = 0
    cost: float = 0.0
    count: int = 0

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_write + self.cache_read

    def add(self, e: Entry) -> None:
        self.input += e.input
        self.output += e.output
        self.cache_write += e.cache_write
        self.cache_read += e.cache_read
        self.cost += e.cost
        self.count += 1


def _start_of_day(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def scan_start(now: datetime) -> float:
    """Earliest mtime worth reading: min(start of month, start of week, now-5h)."""
    som = _start_of_day(now).replace(day=1)
    sow = _start_of_day(now) - timedelta(days=now.weekday())
    blk = now - timedelta(seconds=BLOCK_WINDOW)
    return min(som, sow, blk).timestamp()


def burn_tier(tokens_per_minute: float | None) -> str:
    if tokens_per_minute is None or tokens_per_minute <= 1_000:
        return "idle"
    if tokens_per_minute < 100_000:
        return "normal"
    if tokens_per_minute < 400_000:
        return "fast"
    return "blazing"


@dataclass
class Snapshot:
    now: datetime
    today_date: str
    today: Bucket
    week: Bucket
    month: Bucket
    block: Bucket | None
    block_start: float | None
    tokens_per_minute: float | None
    burn_tier: str
    models_today: dict[str, int]
    entries: int

    @property
    def has_data(self) -> bool:
        return self.entries > 0

    def today_by_provider(self) -> dict[str, int]:
        """Upstream omits a provider whose today total is 0 (its daily report is nil)."""
        return {PROVIDER_ID: self.today.total} if self.today.total > 0 else {}


def summarize(entries: list[Entry], now: datetime | None = None) -> Snapshot:
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    month_prefix = now.strftime("%Y-%m")
    week_start = (_start_of_day(now) - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
    now_ts = now.timestamp()

    t, w, m = Bucket(), Bucket(), Bucket()
    models: dict[str, int] = {}
    recent: list[Entry] = []
    for e in entries:
        if e.local_day == today:
            t.add(e)
            if e.total > 0:
                models[e.model] = models.get(e.model, 0) + e.total
        if e.local_day >= week_start:
            w.add(e)
        if e.local_day.startswith(month_prefix):
            m.add(e)
        if e.ts >= now_ts - BLOCK_WINDOW:
            recent.append(e)

    block = block_start = tpm = None
    if recent:
        block = Bucket()
        for e in recent:
            block.add(e)
        block_start = min(e.ts for e in recent)
        minutes = max(1.0, (now_ts - block_start) / 60)
        tpm = block.total / minutes

    return Snapshot(now=now, today_date=today, today=t, week=w, month=m, block=block,
                    block_start=block_start, tokens_per_minute=tpm, burn_tier=burn_tier(tpm),
                    models_today=dict(sorted(models.items(), key=lambda kv: -kv[1])),
                    entries=len(entries))
