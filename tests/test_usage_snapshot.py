"""A golden snapshot of the Claude Code usage reader.

`usage.py` decides how much progress everybody gets. A drift of a few percent in its
totals would not fail a single other test in this suite, and nobody would notice until
their companion started growing at the wrong speed. So this pins the whole output —
every parsed entry, the day/week/month/block aggregation, and the per-day history — for
one committed fixture tree, and fails on any change.

Three things make the reader machine-dependent, and all three are pinned here:

* `local_day` converts each timestamp through the *local* zone, so TZ is fixed to
  Asia/Kuala_Lumpur (UTC+8, no DST) and `now` is passed explicitly.
* `project_label` strips the encoded `$HOME` prefix, so HOME is fixed.
* `project_label` takes the component after the *first* path part named `projects`, so
  the fixture is staged under a temporary directory. Reading it in place would break for
  anyone who cloned this repo into `~/projects/`.

Regenerate after an intentional change:  POKETOKEN_UPDATE_GOLDEN=1 python3 -m unittest test_usage_snapshot
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from poketoken import usage as U  # noqa: E402

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "fixtures" / "usage"
GOLDEN = HERE / "fixtures" / "usage_golden.json"

NOW = datetime(2026, 3, 11, 14, 30, 0)      # a Wednesday; the week starts Mon 2026-03-09
TZ = "Asia/Kuala_Lumpur"                    # UTC+8, no DST, so the fixture never shifts
HOME = "/home/fixture"                      # matches the -home-fixture-* fixture folders
UPDATE = os.environ.get("POKETOKEN_UPDATE_GOLDEN") == "1"


@contextlib.contextmanager
def pinned_environment():
    """Fix TZ and HOME for the duration, then put the process back as it was."""
    before = {k: os.environ.get(k) for k in ("TZ", "HOME")}
    os.environ["TZ"] = TZ
    os.environ["HOME"] = HOME
    time.tzset()
    try:
        yield
    finally:
        for k, v in before.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        time.tzset()


@contextlib.contextmanager
def staged_fixture():
    """A private copy of the fixture, under a path with no component named 'projects'."""
    tmp = tempfile.mkdtemp(prefix="ptusage-")
    try:
        dst = Path(tmp) / "fx"
        shutil.copytree(FIXTURE, dst)
        assert "projects" not in Path(tmp).parts, f"temp dir would confuse project_label: {tmp}"
        yield [dst / "root-a" / "projects", dst / "root-b" / "loose"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _bucket(b) -> dict | None:
    if b is None:
        return None
    return {"input": b.input, "output": b.output, "cacheWrite": b.cache_write,
            "cacheRead": b.cache_read, "cost": round(b.cost, 6), "count": b.count,
            "total": b.total}


def _entry(e) -> dict:
    return {"id": e.id, "ts": round(e.ts, 3), "localDay": e.local_day, "model": e.model,
            "input": e.input, "output": e.output, "cacheWrite5m": e.cache_write_5m,
            "cacheWrite1h": e.cache_write_1h, "cacheRead": e.cache_read,
            "project": e.project, "total": e.total, "cost": round(e.cost, 6)}


def observed(reader: U.UsageReader, entries: list) -> dict:
    """Everything the rest of the app can see coming out of the reader."""
    snap = U.summarize(entries, NOW)
    return {
        "readerFiles": reader.last_scan_files,
        "entries": [_entry(e) for e in sorted(entries, key=lambda e: (e.ts, e.id))],
        "snapshot": {
            "todayDate": snap.today_date,
            "today": _bucket(snap.today), "week": _bucket(snap.week), "month": _bucket(snap.month),
            "block": _bucket(snap.block),
            "blockStart": None if snap.block_start is None else round(snap.block_start, 3),
            "tokensPerMinute": None if snap.tokens_per_minute is None else round(snap.tokens_per_minute, 6),
            "burnTier": snap.burn_tier,
            "modelsToday": snap.models_today,
            "modelsCostToday": {k: round(v, 6) for k, v in snap.models_cost_today.items()},
            "projectsToday": snap.projects_today,
            "entryCount": snap.entries,
            "todayByProvider": snap.today_by_provider(),
        },
        "dayStats": U.day_stats(entries),
        "bestBlockAll": U.best_block(entries),
    }


def read_all(roots, reader=None, modified_since=0.0):
    r = reader or U.UsageReader(roots)
    return r, r.scan(modified_since)


@unittest.skipUnless(hasattr(time, "tzset"), "needs POSIX tzset to pin the local zone")
class UsageSnapshotTests(unittest.TestCase):
    """The reader's whole output, pinned."""

    def test_matches_the_golden_snapshot(self):
        with pinned_environment(), staged_fixture() as roots:
            reader, entries = read_all(roots)
            got = observed(reader, entries)
        if UPDATE:
            GOLDEN.write_text(json.dumps(got, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            self.skipTest(f"rewrote {GOLDEN.name}")
        want = json.loads(GOLDEN.read_text(encoding="utf-8"))
        self.assertEqual(want, got, "the usage reader's output changed; if that was deliberate, "
                                    "rerun with POKETOKEN_UPDATE_GOLDEN=1 and read the diff carefully")

    def test_a_zero_usage_turn_counts_as_an_entry_but_not_as_usage(self):
        with pinned_environment(), staged_fixture() as roots:
            reader, entries = read_all(roots)
            snap = U.summarize(entries, NOW)
        self.assertIn("msg-4|req-4", {e.id for e in entries})
        self.assertNotIn("claude-haiku-4-5-20251001", snap.models_today)
        self.assertNotIn("gamma", snap.projects_today)

    def test_hidden_files_and_directories_are_not_read(self):
        with pinned_environment(), staged_fixture() as roots:
            _, entries = read_all(roots)
        ids = {e.id for e in entries}
        self.assertNotIn("msg-hidden|req-hidden", ids)
        self.assertNotIn("msg-skipped|req-skipped", ids)

    def test_the_largest_logging_of_a_message_wins_across_files(self):
        """The fixture logs one message larger in session-b and another larger in session-a.
        `os.walk` returns the two in whatever order the filesystem gives, so checking only one
        direction would pass by luck under a keep-first bug."""
        with pinned_environment(), staged_fixture() as roots:
            _, entries = read_all(roots)
        by_id = {e.id: e for e in entries}
        self.assertEqual(1, len([e for e in entries if e.id == "msg-1|req-1"]))
        self.assertEqual(900, by_id["msg-1|req-1"].output, "larger copy lives in session-b")
        self.assertEqual(700, by_id["msg-13|req-13"].output, "larger copy lives in session-a")


@unittest.skipUnless(hasattr(time, "tzset"), "needs POSIX tzset to pin the local zone")
class IncrementalReadTests(unittest.TestCase):
    """Logs are append-only and re-read from their last offset. That offset bookkeeping is
    the part most likely to lose or double-count tokens, so each branch of it is exercised."""

    def _cold(self, roots) -> dict:
        reader, entries = read_all(roots, reader=U.UsageReader(roots))
        return observed(reader, entries)

    def test_an_unchanged_tree_is_not_read_a_second_time(self):
        with pinned_environment(), staged_fixture() as roots:
            reader, first = read_all(roots)
            self.assertGreater(reader.last_scan_lines, 0)
            second = reader.scan(0.0)
            self.assertEqual(0, reader.last_scan_lines, "unchanged files were parsed again")
            self.assertEqual({e.id for e in first}, {e.id for e in second})

    def test_appending_gives_the_same_result_as_a_cold_scan(self):
        with pinned_environment(), staged_fixture() as roots:
            reader, _ = read_all(roots)
            extra = ('{"type":"assistant","timestamp":"2026-03-11T06:00:00.000Z","requestId":"req-20",'
                     '"message":{"id":"msg-20","model":"claude-opus-5",'
                     '"usage":{"input_tokens":9,"output_tokens":99,"cache_read_input_tokens":500}}}\n')
            target = roots[0] / "-home-fixture-alpha" / "session-a.jsonl"
            with open(target, "a", encoding="utf-8") as f:
                f.write(extra)
            warm = observed(reader, reader.scan(0.0))
            # the point of the offset bookkeeping: only the appended line is parsed again.
            # Re-reading the file whole is invisible in the output — the reader is idempotent —
            # so the line count is the only thing that can catch it.
            self.assertEqual(1, reader.last_scan_lines, "the whole file was re-parsed, not the tail")
            cold = self._cold(roots)
            self.assertEqual(cold, warm, "an incremental read diverged from a cold read")
            self.assertIn("msg-20|req-20", {e["id"] for e in warm["entries"]})

    def test_a_line_split_across_two_scans_is_stitched_not_lost(self):
        with pinned_environment(), staged_fixture() as roots:
            reader, _ = read_all(roots)
            whole = ('{"type":"assistant","timestamp":"2026-03-11T06:05:00.000Z","requestId":"req-21",'
                     '"message":{"id":"msg-21","model":"claude-opus-5",'
                     '"usage":{"input_tokens":3,"output_tokens":33,"cache_read_input_tokens":300}}}\n')
            head, tail = whole[:60], whole[60:]
            target = roots[0] / "-home-fixture-alpha" / "session-a.jsonl"
            with open(target, "a", encoding="utf-8") as f:
                f.write(head)                       # no newline yet: a torn write
            mid = reader.scan(0.0)
            self.assertNotIn("msg-21|req-21", {e.id for e in mid}, "half a line was parsed")
            with open(target, "a", encoding="utf-8") as f:
                f.write(tail)
            done = reader.scan(0.0)
            got = [e for e in done if e.id == "msg-21|req-21"]
            self.assertEqual(1, len(got))
            self.assertEqual(33, got[0].output)
            self.assertEqual(self._cold(roots), observed(reader, done))

    def test_a_rewritten_shorter_file_is_read_from_the_start(self):
        with pinned_environment(), staged_fixture() as roots:
            reader, _ = read_all(roots)
            target = roots[0] / "-home-fixture-alpha" / "session-a.jsonl"
            target.write_text(
                '{"type":"assistant","timestamp":"2026-03-11T06:10:00.000Z","requestId":"req-22",'
                '"message":{"id":"msg-22","model":"claude-opus-5",'
                '"usage":{"input_tokens":1,"output_tokens":11}}}\n', encoding="utf-8")
            after = reader.scan(0.0)
            ids = {e.id for e in after}
            self.assertIn("msg-22|req-22", ids)
            self.assertNotIn("msg-3|req-3", ids, "entries from the replaced file survived")
            self.assertEqual(self._cold(roots), observed(reader, after))

    def test_a_deleted_file_takes_its_entries_with_it(self):
        with pinned_environment(), staged_fixture() as roots:
            reader, _ = read_all(roots)
            (roots[0] / "-srv-work-beta" / "session-c.jsonl").unlink()
            after = reader.scan(0.0)
            self.assertNotIn("msg-10|req-10", {e.id for e in after})
            self.assertEqual(self._cold(roots), observed(reader, after))

    def test_files_older_than_the_cutoff_are_skipped(self):
        with pinned_environment(), staged_fixture() as roots:
            for p in Path(roots[0]).rglob("*.jsonl"):
                os.utime(p, (1_000_000, 1_000_000))
            reader, entries = read_all(roots, modified_since=2_000_000)
            self.assertEqual(0, len([e for e in entries if e.project]))
            self.assertEqual(1, reader.last_scan_files, "only the untouched second root should be read")


@unittest.skipUnless(hasattr(time, "tzset"), "needs POSIX tzset to pin the local zone")
class ScanCutoffTests(unittest.TestCase):
    """`scan_start` decides which files are opened at all, so a bug here loses tokens
    silently rather than loudly."""

    def test_it_reaches_back_to_the_start_of_the_month(self):
        with pinned_environment():
            got = datetime.fromtimestamp(U.scan_start(datetime(2026, 3, 11, 14, 30)))
        self.assertEqual(datetime(2026, 3, 1, 0, 0), got)

    def test_a_week_that_began_last_month_is_not_cut_off(self):
        """1 April 2026 is a Wednesday, so the current week starts on 30 March. The cutoff
        has to follow the week back past the start of the month."""
        with pinned_environment():
            got = datetime.fromtimestamp(U.scan_start(datetime(2026, 4, 1, 9, 0)))
        self.assertEqual(datetime(2026, 3, 30, 0, 0), got)

    def test_the_five_hour_block_never_widens_the_cutoff(self):
        with pinned_environment():
            got = datetime.fromtimestamp(U.scan_start(datetime(2026, 3, 2, 2, 0)))
        self.assertEqual(datetime(2026, 3, 1, 0, 0), got)


if __name__ == "__main__":
    unittest.main()
