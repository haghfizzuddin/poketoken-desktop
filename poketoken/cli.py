"""Command-line front end: status card, live watch, statusline segment, shop, bag, dex, pet."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from . import __version__, battle as B, companion as C, fmt, instance, notify, settings, usage as U
from .paths import state_dir
from .pokeapi import PokeAPI

RARITY_EMOJI = {"common": "○", "uncommon": "◐", "rare": "●", "legendary": "★"}


class App:
    def __init__(self, state: Path | None = None):
        self.dir = state or state_dir()
        self.api = PokeAPI(self.dir / "cache", self.dir / "sprites")
        self.reader = U.UsageReader()
        self.companion = C.Companion(self.api, self.dir / "state.json", log=self.log)
        self.last: U.Snapshot | None = None
        self.notified: set[tuple] = set()      # companion events already announced on the desktop

    def log(self, msg: str) -> None:
        try:
            with open(self.dir / "events.log", "a", encoding="utf-8") as f:
                f.write(f"{datetime.now().isoformat(timespec='seconds')} {msg}\n")
        except OSError:
            pass

    def tick(self, now: datetime | None = None) -> U.Snapshot:
        now = now or datetime.now()
        entries = self.reader.scan(U.scan_start(now))
        snap = U.summarize(entries, now)
        if not self.companion.state.history_backfilled:
            # one-time: read every log file ever written so streaks and the weekly goal start
            # from real history instead of from today
            all_entries = self.reader.scan(0)
            self.companion.record_history(U.day_stats(all_entries), snap.today_date, backfill=True)
            self.log(f"history backfilled: {len(self.companion.state.history)} days from {self.reader.last_scan_files} files")
        else:
            self.companion.record_history(U.day_stats(entries), snap.today_date)
        self.companion.update(snap.today_by_provider(), snap.today_date, snap.burn_tier,
                              limit_warning=False, has_usage_data=snap.has_data)
        self.notify_new_events()
        self.last = snap
        return snap

    def notify_new_events(self) -> int:
        """Desktop-notify companion events not announced yet; returns how many were sent.

        Events stay in `companion.events` until a consumer drains them and tick() may run several
        times before that, so each announced event is remembered by its `at` stamp and fields.
        """
        sent, seen = 0, set()
        for ev in self.companion.events:
            key = tuple(sorted((k, str(v)) for k, v in ev.items()))
            seen.add(key)
            if key in self.notified:
                continue
            self.notified.add(key)
            sent += int(notify.notify_event(ev, self.dir))
        self.notified &= seen                  # drained events never come back; keep the set small
        return sent

    def streak_line(self, today: str) -> str:
        n, start, counts = self.companion.streak(today)
        if n == 0:
            return "no streak yet — a day counts at 1M+ tokens"
        nd, nc = C.next_streak_milestone(n)
        return (f"{n}-day streak since {start} · +{nc} candy at {nd} days"
                + ("  ✓ today counts" if counts else "  · today not yet counted"))

    def goal_line(self, today: str) -> str:
        g = self.companion.weekly_goal(today)
        if g["target"] is None:
            return f"weekly goal unlocks after {g['weeks_needed']} more week(s) of history"
        return (f"weekly goal {fmt.compact(g['current'])} / {fmt.compact(g['target'])}  "
                f"{fmt.percent(g['progress'] * 100)}" + ("  ✓ reached" if g["progress"] >= 1 else ""))

    # --------------------------------------------------------------- views
    def status_lines(self, snap: U.Snapshot) -> list[str]:
        c, s = self.companion, self.companion.state
        st = c.display_state
        out: list[str] = []
        if c.is_egg:
            out.append(f"{C.STATE_EMOJI['egg']}  Token Egg   {fmt.bar(c.egg_progress)} {fmt.percent(c.egg_progress * 100)}")
            out.append(f"    {fmt.compact(s.egg_usage)} / {fmt.compact(C.EGG_HATCH_THRESHOLD)} incubated · "
                       f"{fmt.compact(c.egg_tokens_to_hatch)} to hatch"
                       + (f" · guaranteed {s.egg_tier}+" if s.egg_tier else ""))
            if not s.install_baseline_set:
                out.append("    (baseline not set yet — the first refresh that sees today's usage starts the clock)")
        else:
            a = s.active
            name = c.display_name()
            shiny = " ✨shiny" if a.shiny_visible else ""
            nature = f" · {a.nature.title()}" if a.nature else ""
            out.append(f"{C.STATE_EMOJI.get(st, '🐾')}  {name}{shiny}  [{RARITY_EMOJI[a.rarity]} {a.rarity}{nature}]  "
                       f"stage {c.stage_text}  ·  {st}")
            out.append(f"    {fmt.bar(c.progress)} {fmt.percent(c.progress * 100)}  "
                       f"{fmt.compact(a.used_at_stage)} / {fmt.compact(c.threshold)}"
                       + ("  → graduation" if c.is_final_stage else "  → next form"))
            chain = []
            for sid, state in c.line_items():
                label = c.line.name(sid, s.language) if (sid and c.line) else "?"
                chain.append(f"[{label}]" if state == "current" else label)
            out.append("    line: " + " → ".join(chain))
        out.append("")
        t = snap.today
        out.append(f"Today {snap.today_date}:  {fmt.grouped(t.total)} tokens  ·  {fmt.cost(t.cost)}")
        out.append(f"    in {fmt.compact(t.input)} · out {fmt.compact(t.output)} · "
                   f"cache write {fmt.compact(t.cache_write)} · cache read {fmt.compact(t.cache_read)}")
        if snap.models_today:
            out.append("    " + " · ".join(f"{m}: {fmt.compact(n)}" for m, n in list(snap.models_today.items())[:4]))
        if snap.block:
            mins = (snap.now.timestamp() - (snap.block_start or snap.now.timestamp())) / 60
            out.append(f"5h block: {fmt.compact(snap.block.total)} tokens over {mins:.0f} min · "
                       f"{fmt.compact(int(snap.tokens_per_minute or 0))} tpm ({snap.burn_tier}) · {fmt.cost(snap.block.cost)}")
        out.append(f"Week: {fmt.compact(snap.week.total)} · {fmt.cost(snap.week.cost)}    "
                   f"Month: {fmt.compact(snap.month.total)} · {fmt.cost(snap.month.cost)}")
        out.append(f"Streak: {self.streak_line(snap.today_date)}")
        out.append(f"Goal:   {self.goal_line(snap.today_date)}")
        out.append("")
        inv = " ".join(f"{C.ITEMS[k]['emoji']}×{n}" for k, n in s.inventory.items() if n > 0) or "empty"
        grads = sum(1 for e in s.dex if not e.is_released)
        out.append(f"Wallet {fmt.compact(c.wallet)} tokens · Bag {inv} · Pokédex {grads} graduated "
                   f"({len({sid for e in s.dex for sid in e.chain_order})} species)")
        return out

    def statusline(self, snap: U.Snapshot) -> str:
        c, s = self.companion, self.companion.state
        emoji = C.STATE_EMOJI.get(c.display_state, "🐾")
        if c.is_egg:
            head = f"{emoji} egg {fmt.percent(c.egg_progress * 100)}"
        else:
            a = s.active
            head = f"{emoji} {c.display_name()}{'✨' if a.shiny_visible else ''} {c.stage_text} {fmt.percent(c.progress * 100)}"
        parts = [head, f"{fmt.compact(snap.today.total)} {fmt.cost_compact(snap.today.cost)}"]
        if snap.tokens_per_minute and snap.tokens_per_minute > 1000:
            parts.append(f"{fmt.compact(int(snap.tokens_per_minute))}/min")
        return " │ ".join(parts)


def _print(lines: list[str]) -> None:
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


def cmd_status(app: App, args) -> int:
    snap = app.tick()
    _print(app.status_lines(snap))
    for ev in app.companion.drain_events():
        print(f"  ★ {ev['kind']}: " + " ".join(f"{k}={v}" for k, v in ev.items() if k not in ("kind", "at")))
    return 0


def cmd_watch(app: App, args) -> int:
    try:
        while True:
            snap = app.tick()
            os.system("clear" if os.name != "nt" else "cls")
            print(f"poketoken {__version__} · {snap.now:%H:%M:%S} · refresh {args.interval}s · Ctrl-C to quit\n")
            _print(app.status_lines(snap))
            for ev in app.companion.drain_events():
                print(f"\n  ★ {ev['kind']}: " + " ".join(f"{k}={v}" for k, v in ev.items() if k not in ("kind", "at")))
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


def cmd_statusline(app: App, args) -> int:
    snap = app.tick()
    print(app.statusline(snap))
    return 0


def cmd_refresh(app: App, args) -> int:
    snap = app.tick()
    evs = app.companion.drain_events()
    print(f"{snap.today_date} today={snap.today.total:,} state={app.companion.display_state} "
          f"files={app.reader.last_scan_files} events={len(evs)}")
    return 0


def cmd_history(app: App, args) -> int:
    snap = app.tick()
    h = app.companion.state.history
    days = sorted(h)[-args.days:]
    if not days:
        print("no history yet")
        return 0
    peak = max(h[d]["tokens"] for d in days) or 1
    print(f"{'date':<11}{'tokens':>8}{'cost':>9}{'cache':>7}{'best 5h':>9}  activity")
    for d in days:
        r = h[d]
        mark = "★" if r["tokens"] >= C.STREAK_MIN_TOKENS else " "
        print(f"{d:<11}{fmt.compact(r['tokens']):>8}{fmt.cost(r['cost']):>9}{fmt.percent(r.get('cacheRatio', 0) * 100):>7}"
              f"{fmt.compact(r.get('bestBlock', 0)):>9}  {fmt.bar(r['tokens'] / peak, 24)} {mark}")
    print(f"\n★ = counts toward the streak (≥ {fmt.compact(C.STREAK_MIN_TOKENS)})")
    print(app.streak_line(snap.today_date))
    print(app.goal_line(snap.today_date))
    return 0


TYPE_ABBR = {"special-attack": "SpA", "special-defense": "SpD"}


def stats_lines(app: App) -> list[str]:
    c, s = app.companion, app.companion.state
    a = s.active
    if a is None:
        return ["No Pokémon yet — the egg has no stats."]
    try:
        meta = app.api.pokemon(a.current_id)
    except Exception as e:  # noqa: BLE001 — PokeAPIError or a cache problem
        return [f"stats unavailable: {e}"]
    v = c.stats_view(meta)
    if v is None:
        return ["stats unavailable"]
    out = [f"{c.display_name()}{' ✨' if a.shiny_visible else ''}  Lv {v['level']}  ·  {'/'.join(t.title() for t in v['types'])}"
           f"  ·  {(a.nature or 'unknown').title()}  ·  {v['height_m']:.1f} m · {v['weight_kg']:.1f} kg"]
    abil = ", ".join(x["name"].replace("-", " ").title() + (" (hidden)" if x["hidden"] else "") for x in v["abilities"])
    out.append(f"    abilities: {abil}")
    peak = max(r["value"] for r in v["rows"]) or 1
    for r in v["rows"]:
        mark = " +" if r["mod"] > 0 else " −" if r["mod"] < 0 else "  "
        iv = f"IV {r['iv']:>2}" if r["iv"] is not None else "IV  ?"
        out.append(f"    {r['label']:<8}{r['value']:>4}{mark}  {fmt.bar(r['value'] / peak, 18)}  base {r['base']:>3}  {iv}")
    if v["iv_total"] is not None:
        out.append(f"    IV total {v['iv_total']}/186")
    lk = v["luck"] or {}
    if lk:
        out.append(f"    luck at hatch: {lk.get('bonusRolls', 0)} bonus roll(s) · {lk.get('streak', 0)}-day streak · "
                   f"{lk.get('cacheRatio', 0) * 100:.0f}% cache reads · shiny 1/{lk.get('shinyDenominator', '?')}")
    return out


def cmd_stats(app: App, args) -> int:
    app.tick()
    _print(stats_lines(app))
    return 0


def _read_card_arg(text: str) -> dict:
    """A card token, '-' for stdin, or a path to a file holding one."""
    if text == "-":
        text = sys.stdin.read()
    elif not text.startswith(B.CARD_PREFIX) and Path(text).is_file():
        text = Path(text).read_text("utf-8")
    return B.decode_card(text)


def _my_card(app: App) -> dict | None:
    a = app.companion.state.active
    if a is None:
        return None
    meta = app.api.pokemon(a.current_id)
    trainer = settings.get(app.dir, "trainer") or B.default_trainer()
    return B.make_card(app.companion, meta, trainer)


def cmd_card(app: App, args) -> int:
    if args.trainer:
        settings.set(app.dir, "trainer", args.trainer[:24])
        print(f"trainer name set to {args.trainer[:24]!r}")
    app.tick()
    card = _my_card(app)
    if card is None:
        print("No Pokémon to put on a card yet (egg).")
        return 1
    if args.json:
        print(json.dumps(card, indent=1))
        return 0
    print(B.card_summary(card))
    print(f"power {B.power_score(card)}\n")
    print(B.encode_card(card))
    print("\nSend that line to a colleague; they run:  poketoken battle <card>")
    return 0


def cmd_battle(app: App, args) -> int:
    chart = app.api.type_chart()
    try:
        if args.other:
            card_a, card_b = _read_card_arg(args.card), _read_card_arg(args.other)
        else:
            app.tick()
            card_a = _my_card(app)
            if card_a is None:
                print("You need a hatched Pokémon to battle.")
                return 1
            card_b = _read_card_arg(args.card)
    except ValueError as e:
        print(f"✗ {e}")
        return 1
    res = B.simulate(card_a, card_b, chart)
    print(f"{B.card_summary(card_a)}\n    vs\n{B.card_summary(card_b)}\n")
    for line in res["log"][: args.log]:
        print("  " + line)
    if len(res["log"]) > args.log:
        print(f"  … {len(res['log']) - args.log} more turns")
    w, l = res["winner"], res["loser"]
    print(f"\n🏆 {w['name']} ({w['trainer']}) wins in {res['turns']} turn{'s' if res['turns'] != 1 else ''} · "
          f"{res['remaining'][w['name']]} HP left · {l['name']} ({l['trainer']}) fainted")
    print(f"power {B.power_score(card_a)} vs {B.power_score(card_b)}")
    return 0


def cmd_dex(app: App, args) -> int:
    s = app.companion.state
    if not s.dex:
        print("Pokédex is empty — graduate your first Pokémon.")
        return 0
    for e in sorted(s.dex, key=lambda e: e.caught_at or ""):
        chain = " → ".join(e.name(sid, s.language) for sid in e.chain_order)
        tag = "released" if e.is_released else "graduated"
        print(f"{RARITY_EMOJI[e.rarity]} #{e.final_id:<4} {e.name(e.final_id, s.language):<14} "
              f"{'✨ ' if e.is_shiny else '   '}{e.rarity:<9} {(e.nature or '').title():<8} "
              f"{tag:<9} {(e.caught_at or '')[:10]}   {chain}")
    return 0


def cmd_shop(app: App, args) -> int:
    c = app.companion
    if args.buy:
        alias = {"candy": "rareCandy", "rarecandy": "rareCandy", "mint": "mint", "charm": "shinyCharm",
                 "shinycharm": "shinyCharm", "egg": "egg:plain", "egg-uncommon": "egg:uncommon",
                 "egg-rare": "egg:rare"}
        ok, msg = c.buy(alias.get(args.buy.lower(), args.buy))
        print(("✓ " if ok else "✗ ") + msg)
        return 0 if ok else 1
    print(f"Wallet: {fmt.grouped(c.wallet)} tokens ({fmt.compact(c.wallet)})\n")
    for r in c.shop_entries():
        afford = "✓" if c.wallet >= r["price"] else " "
        owned = f"  (owned ×{r['owned']})" if r["owned"] else ""
        print(f" {afford} {r['emoji']} {r['label']:<14} {fmt.compact(r['price']):>6}   {r['blurb']}{owned}")
    print("\nbuy with: poketoken shop --buy candy|mint|charm|egg|egg-uncommon|egg-rare")
    return 0


def cmd_bag(app: App, args) -> int:
    c = app.companion
    if args.use:
        ok, msg = c.use_rare_candy() if args.use.lower() in ("candy", "rarecandy") else \
            c.use_mint() if args.use.lower() == "mint" else (False, "use: candy | mint")
        print(("✓ " if ok else "✗ ") + msg)
        return 0 if ok else 1
    items = [(k, n) for k, n in c.state.inventory.items() if n > 0]
    if not items:
        print("Bag is empty.")
        return 0
    for k, n in items:
        it = C.ITEMS[k]
        print(f" {it['emoji']} {it['label']:<12} ×{n}  {'(active)' if it['passive'] else ''}")
    print("\nuse with: poketoken bag --use candy|mint")
    return 0


def cmd_app(app: App, args) -> int:
    if instance.running_pid(app.dir) is not None:
        instance.send(app.dir, "raise")
        print("PokeToken is already open — brought it to the front.")
        return 0
    compact = bool(getattr(args, "compact", False))
    if getattr(args, "detach", False):
        argv = ["--state-dir", str(app.dir), "app", "-i", str(args.interval)]   # global option first
        argv += ["--compact"] if compact else []
        argv += ["--dark"] if args.dark else ["--light"] if args.light else []
        pid = instance.spawn_detached(argv, app.dir)
        if instance.wait_for(lambda: instance.running_pid(app.dir) is not None, 10):
            print(f"PokeToken opened in the background (pid {pid}).")
            return 0
        print(f"started pid {pid} but no window appeared — see {instance.log_file(app.dir)}")
        return 1
    from .ui import run_window
    dark = True if args.dark else (False if args.light else None)
    return run_window(app, compact=compact, dark=dark, interval=args.interval)


def cmd_pet(app: App, args) -> int:
    args.compact = True
    return cmd_app(app, args)


def cmd_close(app: App, args) -> int:
    if not instance.send(app.dir, "quit"):
        print("PokeToken is not running.")
        return 1
    if instance.wait_for(lambda: instance.running_pid(app.dir) is None, 6):
        print("PokeToken closed.")
        return 0
    print("asked PokeToken to close, but it is still running.")
    return 1


def cmd_toggle(app: App, args) -> int:
    if instance.running_pid(app.dir) is not None:
        return cmd_close(app, args)
    args.detach = True
    return cmd_app(app, args)


def cmd_debug(app: App, args) -> int:
    t0 = time.perf_counter()
    snap = app.tick()
    dt = time.perf_counter() - t0
    print(f"state dir: {app.dir}")
    print("roots:", *[f"  {r}" for r in app.reader.roots] or ["  (none found)"], sep="\n")
    print(f"scan: {app.reader.last_scan_files} files, {app.reader.last_scan_lines} usage lines parsed, "
          f"{snap.entries} unique entries in window, {dt:.2f}s")
    t0 = time.perf_counter()
    app.tick()
    print(f"incremental re-scan: {time.perf_counter() - t0:.3f}s")
    s = app.companion.state
    print(f"companion: baseline={s.install_baseline_set} usedSinceInstall={s.used_since_install:,} "
          f"eggUsage={s.egg_usage:,} pending={s.pending_hatch_id} active={s.active.to_dict() if s.active else None}")
    return 0


def cmd_notify(app: App, args) -> int:
    d = app.dir
    if args.action in ("on", "off"):
        notify.set_enabled(d, args.action == "on")
        print(f"notifications {args.action}  ({notify.settings_path(d)})")
        return 0
    be = notify.backend()
    if args.action == "test":
        if be is None:
            print("no notification backend: needs notify-send on PATH, or powershell.exe (WSL / Windows)")
            return 1
        ok = notify.send("PokeToken", "Notifications are working.", d, wait=15)
        print(f"backend {be}: {'accepted' if ok else 'failed'}  (details: {d / notify.LOG_FILE})")
        if not notify.enabled(d):
            print("note: notifications are off, events are not announced — `poketoken notify on`")
        return 0 if ok else 1
    print(f"notifications: {'on' if notify.enabled(d) else 'off'} · backend: {be or 'none'} · "
          f"settings: {notify.settings_path(d)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="poketoken", description="PokeTokenBar for WSL — tokens → Pokémon")
    p.add_argument("--state-dir", type=Path, help="override the state directory")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("status", help="one-shot status card (default)")
    w = sub.add_parser("watch", help="live terminal view")
    w.add_argument("-i", "--interval", type=int, default=30)
    sub.add_parser("statusline", help="one compact line for the Claude Code status line")
    sub.add_parser("refresh", help="single refresh tick (for cron/systemd timers)")
    hi = sub.add_parser("history", help="daily usage table, streak and weekly goal")
    hi.add_argument("-n", "--days", type=int, default=30)
    sub.add_parser("stats", help="level, types, abilities and stats of your current Pokémon")
    cd = sub.add_parser("card", help="print your battle card to share with a colleague")
    cd.add_argument("--json", action="store_true", help="raw card instead of the token")
    cd.add_argument("--trainer", help="set the trainer name shown on your card")
    bt = sub.add_parser("battle", help="fight a colleague's card (or two cards against each other)")
    bt.add_argument("card", help="a PT1. card token, a file containing one, or - for stdin")
    bt.add_argument("other", nargs="?", help="second card: spectate two cards instead of using yours")
    bt.add_argument("--log", type=int, default=12, help="turns of battle log to print")
    sub.add_parser("dex", help="Pokédex / catch log")
    sh = sub.add_parser("shop", help="token shop")
    sh.add_argument("--buy", help="candy | mint | charm | egg | egg-uncommon | egg-rare")
    bg = sub.add_parser("bag", help="inventory")
    bg.add_argument("--use", help="candy | mint")
    def window_flags(sp):
        sp.add_argument("-d", "--detach", action="store_true", help="open in the background and return")
        sp.add_argument("--dark", action="store_true", help="dark appearance")
        sp.add_argument("--light", action="store_true", help="light appearance")
        sp.add_argument("-i", "--interval", type=int, default=30, help="refresh seconds")

    ap = sub.add_parser("app", aliases=["window", "ui", "open"], help="open the live window (or bring it to front)")
    ap.add_argument("--compact", action="store_true", help="start in the small companion-only view")
    window_flags(ap)
    pt = sub.add_parser("pet", help="same window, compact view")
    window_flags(pt)
    sub.add_parser("close", help="close the running window")
    tg = sub.add_parser("toggle", help="open the window in the background, or close it if it is open")
    window_flags(tg)
    sub.add_parser("debug", help="scan roots, timings, raw companion state")
    nt = sub.add_parser("notify", help="desktop notifications: on | off | test | status")
    nt.add_argument("action", nargs="?", choices=["on", "off", "test", "status"], default="status")
    args = p.parse_args(argv)

    app = App(args.state_dir)
    handler = {"status": cmd_status, "watch": cmd_watch, "statusline": cmd_statusline, "refresh": cmd_refresh,
               "dex": cmd_dex, "shop": cmd_shop, "bag": cmd_bag, "pet": cmd_pet, "debug": cmd_debug,
               "history": cmd_history, "stats": cmd_stats, "card": cmd_card, "battle": cmd_battle,
               "notify": cmd_notify,
               "app": cmd_app, "window": cmd_app, "ui": cmd_app, "open": cmd_app,
               "close": cmd_close, "toggle": cmd_toggle, None: cmd_status}[args.cmd]
    return handler(app, args)
