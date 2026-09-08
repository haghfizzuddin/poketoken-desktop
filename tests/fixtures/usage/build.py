"""Rebuild the committed usage fixture tree.

    cd tests/fixtures/usage && python3 build.py
    cd tests && POKETOKEN_UPDATE_GOLDEN=1 python3 -m unittest test_usage_snapshot

The .jsonl files this writes are committed, so the golden snapshot pins exact bytes; this
script exists to document what each line is for and to keep the tree extensible. Every line
below earns its place by failing at least one deliberately injected reader bug — if you add
one, check that removing it changes the golden. Times are Asia/Kuala_Lumpur (UTC+8, no DST),
which is the zone tests/test_usage_snapshot.py pins.
"""
import json, os, shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
KL = timezone(timedelta(hours=8))

def z(local: str) -> str:
    """'2026-03-11 10:00' local -> the UTC ISO string a Claude log would carry."""
    dt = datetime.strptime(local, "%Y-%m-%d %H:%M").replace(tzinfo=KL)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

def line(mid, req, local, model, i=0, o=0, cw=None, c1h=None, cr=0):
    usage = {"input_tokens": i, "output_tokens": o, "cache_read_input_tokens": cr}
    if cw is not None:
        usage["cache_creation_input_tokens"] = cw
    if c1h is not None:
        usage["cache_creation"] = {"ephemeral_5m_input_tokens": max(0, (cw or 0) - c1h),
                                   "ephemeral_1h_input_tokens": c1h}
    msg = {"id": mid, "model": model, "usage": usage}
    return json.dumps({"type": "assistant", "timestamp": z(local), "requestId": req, "message": msg})

for generated in ("root-a", "root-b"):
    shutil.rmtree(ROOT / generated, ignore_errors=True)
A = ROOT / "root-a" / "projects"
for d in ("-home-fixture-alpha", "-srv-work-beta", "-home-fixture", "-home-fixture-gamma", ".hidden"):
    (A / d).mkdir(parents=True)
(ROOT / "root-b" / "loose").mkdir(parents=True)

# --- today, inside the 5-hour block (now = 2026-03-11 14:30 local) -----------
a = [
    # logged twice in this file: keep-max within one file
    line("msg-1", "req-1", "2026-03-11 10:00", "claude-fable-5-1", i=40, o=100, cw=500, cr=9000),
    line("msg-1", "req-1", "2026-03-11 10:00", "claude-fable-5-1", i=40, o=380, cw=500, cr=9000),
    # ephemeral_1h larger than the total write: min() must clamp it
    line("msg-2", "req-2", "2026-03-11 11:15", "claude-opus-5", i=12, o=64, cw=200, c1h=999, cr=1500),
    # today but before the block window opens at 09:30
    line("msg-3", "req-3", "2026-03-11 03:00", "claude-sonnet-5", i=8, o=40, cw=100, c1h=25, cr=700),
    # 00:10 local is 2026-03-10 16:10 UTC — it must still land on today
    line("msg-5", "req-5", "2026-03-11 00:10", "claude-fable-5-1", i=5, o=20, cw=50, cr=300),
    # 23:50 local the night before — yesterday, not today
    line("msg-6", "req-6", "2026-03-10 23:50", "claude-fable-5-1", i=6, o=30, cw=60, cr=400),
    # the other half of a cross-file pair, larger here than in session-b. Together with msg-1
    # (larger in session-b) this pins keep-max whichever order os.walk returns the two files in.
    line("msg-13", "req-13", "2026-03-11 11:40", "claude-opus-5", i=14, o=700, cw=180, cr=1600),
]
(A / "-home-fixture-alpha" / "session-a.jsonl").write_text("\n".join(a) + "\n")

b = [
    # same (message.id, requestId) as msg-1 but larger: keep-max must win across files
    line("msg-1", "req-1", "2026-03-11 10:00", "claude-fable-5-1", i=40, o=900, cw=500, cr=9000),
    # ...and msg-13 the other way round: smaller here than in session-a
    line("msg-13", "req-13", "2026-03-11 11:40", "claude-opus-5", i=14, o=90, cw=180, cr=1600),
    # earlier this week (Mon), earlier this month, and last month
    line("msg-7", "req-7", "2026-03-09 10:00", "claude-opus-5", i=20, o=200, cw=300, c1h=100, cr=2000),
    line("msg-8", "req-8", "2026-03-02 10:00", "claude-sonnet-5", i=15, o=150, cw=250, cr=1800),
    line("msg-9", "req-9", "2026-02-25 10:00", "claude-opus-5", i=10, o=90, cw=150, cr=1200),
]
(A / "-home-fixture-alpha" / "session-b.jsonl").write_text("\n".join(b) + "\n")

# a second project, and a folder that is exactly the encoded home ("~")
(A / "-srv-work-beta" / "session-c.jsonl").write_text("\n".join([
    line("msg-10", "req-10", "2026-03-11 13:45", "claude-sonnet-5", i=30, o=250, cw=400, c1h=150, cr=3000),
    # exactly five hours apart, and the later one exactly at `now`. This pins two boundaries
    # that nothing else in the fixture reaches: summarize keeps an entry sitting exactly on
    # now-5h, and best_block's sweep keeps a pair exactly BLOCK_WINDOW apart in one window.
    line("msg-14", "req-14", "2026-03-11 09:30", "claude-sonnet-5", i=4, o=44, cw=40, cr=440),
    line("msg-15", "req-15", "2026-03-11 14:30", "claude-sonnet-5", i=5, o=55, cw=50, cr=3000),
]) + "\n")
(A / "-home-fixture" / "session-home.jsonl").write_text(
    line("msg-11", "req-11", "2026-03-11 09:00", "claude-fable-5-1", i=7, o=70, cw=90, cr=800) + "\n")

# A zero-usage turn: it counts as an entry and as a today `count`, but must contribute to no
# model or project total. Its model and folder appear nowhere else, so if the `e.total > 0`
# guard ever goes, a zero-valued key shows up in the golden instead of changing nothing.
(A / "-home-fixture-gamma" / "session-e.jsonl").write_text(
    line("msg-4", "req-4", "2026-03-11 14:20", "claude-haiku-4-5-20251001") + "\n")

# a root that is not a Claude "projects" tree: no project label, so it buckets as "other",
# and an unrecognised model, so it prices at zero
(ROOT / "root-b" / "loose" / "session-d.jsonl").write_text(
    line("msg-12", "req-12", "2026-03-11 12:00", "some-unreleased-model", i=11, o=110, cw=120, cr=900) + "\n")

# --- lines the reader must ignore -------------------------------------------
noise = [
    json.dumps({"type": "user", "timestamp": z("2026-03-11 10:30"),
                "message": {"id": "u-1", "usage": {"input_tokens": 999999}}}),
    # carries the role inside the message, so it clears the '"usage"' + '"assistant"' prefilter
    # and only the type check stops it. Without that check this line alone adds 778k tokens.
    json.dumps({"type": "user", "timestamp": z("2026-03-11 10:33"), "requestId": "req-noise",
                "message": {"id": "msg-noise", "role": "assistant", "model": "claude-opus-5",
                            "usage": {"input_tokens": 777777, "output_tokens": 777}}}),
    json.dumps({"type": "assistant", "timestamp": z("2026-03-11 10:31"),
                "message": {"id": "m-x", "model": "claude-opus-5"}}),          # no usage
    json.dumps({"type": "assistant", "note": 'the word "usage" in prose',
                "timestamp": z("2026-03-11 10:32")}),                          # passes the prefilter, no message
    json.dumps({"type": "assistant", "message": {"id": "m-y", "usage": {"input_tokens": 5}}}),  # no timestamp
    json.dumps({"type": "assistant", "timestamp": "not-a-date",
                "message": {"id": "m-z", "usage": {"input_tokens": 5}}}),      # unparseable timestamp
    '{"type":"assistant","message":{"usage":{"input_tokens":5}',               # truncated JSON
    "",
]
(A / "-home-fixture-alpha" / "noise.jsonl").write_text("\n".join(noise) + "\n")

# hidden file and hidden directory: both skipped by the walker
(A / "-home-fixture-alpha" / ".hidden.jsonl").write_text(
    line("msg-hidden", "req-hidden", "2026-03-11 10:00", "claude-opus-5", i=1, o=1) + "\n")
(A / ".hidden" / "skipped.jsonl").write_text(
    line("msg-skipped", "req-skipped", "2026-03-11 10:00", "claude-opus-5", i=1, o=1) + "\n")

for p in sorted(ROOT.rglob("*.jsonl")):
    print(p, p.stat().st_size)
