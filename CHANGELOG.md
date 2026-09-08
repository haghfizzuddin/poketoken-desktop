# Changelog

## v0.2.0 — in progress

### Game
- Activity history (120 days) backfilled from every Claude Code log; streaks (a day counts at 1M+ tokens) and a weekly goal (median of your previous weeks, floor 50M).
- Rare Candy from consistency instead of rate limits: streak milestones 3 / 7 / 14 / 30 days pay 1 / 2 / 3 / 5 candies (then 5 per further 30 days); beating the weekly goal pays 5.
- Stats: six hidden IVs per hatch, level 5→100 from growth, the games' stat formula with the nature's ±10 %, saved on Pokédex records at graduation.
- Luck: streaks (7 / 14 days) and cache-read efficiency (70 % / 90 %) add best-of IV rolls; a 7-day streak cuts the shiny denominator by a quarter. Rarity odds untouched.
- Ditto disguise (upstream port): 1/128 common multi-stage hatches reveal a Ditto at the first evolution threshold.
- Wild encounters: a streak day or a personal-best 5-hour block spawns a wild Pokémon; Poké / Great / Ultra Balls in the Shop; capture-rate based catches go straight into the Pokédex.
- Battle cards: `poketoken card` and `poketoken battle <card>` — deterministic, serverless fights with real type matchups.

### Window
- Species page (mini sprite header, stats card, evolution line, records) reachable from Home and from any Pokédex cell; the next evolution form shows blurred and sharpens with progress.
- Sprites fill a fixed container (256 / 320 / 384 / 448 px) using whole-number scaling; Home shows the companion, its evolution line and today's usage on the first screen.
- Encounter card with a Throw button; balls in Shop and Bag; refreshes coalesce; toasts for every event.
- Desktop notifications (Windows toast through PowerShell, `notify-send` on Linux) with an on/off switch.

### CLI and platform
- `app` opens in the background and returns the prompt (`--fg` attaches); `close`, `toggle`, single-instance raise.
- `history`, `stats`, `encounter`, `card`, `battle`, `notify` commands.
- Pricing per model from the current rate card, cache writes split by 5-minute and 1-hour TTL.

## v0.1.0 — 2026-09-07
- Initial port of PokeTokenBar's core for WSL, Linux and Windows: Claude Code log reader, token economy and companion loop, Shop and Bag, SwiftUI-styled Tk window, terminal CLI.
