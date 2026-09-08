# Changelog

## Unreleased

### Interface
- A responsive layout system: four breakpoints, one centred content frame capped at 1240 px, and
  a shared grid used by every page. Wide windows fill with cells; narrow ones reflow to a single
  column with shorter copy and lower density instead of shrinking the desktop layout.
- Design tokens in `poketoken/theme.py` — palettes, spacing, radii, the type ladder, sprite tiers
  and breakpoints — so pages stop inventing pixel values. Muted greys were lifted for contrast.
- Header: brand, sync state and Refresh no longer collide at 360 px; the Refresh label collapses
  to an icon with a tooltip. Tabs get touch-sized targets and all five stay reachable.
- Home: the companion leads, then a real evolution track (progress, percentage, what is left),
  then rewards, then telemetry. The detailed breakdown is a disclosure on narrow viewports.
- Pokédex is a collection grid with larger sprites and a per-species state; Shop is grouped into
  Balls, Training and Eggs and says how much you are short instead of greying a price out; Bag
  shares the Shop's grid; Battle leads with the arena once a fight exists and puts the two cards
  side by side on a wide window.
- Sprites follow fixed tiers (hero, battle, dex, card, micro) and stay whole-number scaled.
- Keyboard navigation, tooltips on icon-only controls, and no horizontal overflow at 360 px.
- The footer no longer carries build information; it moved to About in the menu.
- Home no longer shows the same stage progress twice. The companion card is identity and state:
  name, rarity, a "Stage 2 of 2" pill and its mood. The evolution card owns the progress — the
  track, the percentage, and now the "61.8M / 500M" figure beside what is left. An egg, which
  has no evolution card, keeps its hatch bar on the companion card.

### Added
- **Raise a caught Pokémon.** A caught or released Pokémon can be taken out of the Pokédex and
  raised as your companion, from its species page or `poketoken raise <name> --yes`. It keeps
  the IVs, nature and shininess it was caught with but starts at level 5 with no progress, and
  it costs a plain egg so that hatching — and its surprise — stays the free default. Graduated
  Pokémon are finished trophies and cannot be raised again.
- **Honest battle levels.** A Pokédex record now fields at the level it earned: a graduation at
  100, a Pokémon released part-way at the level it reached, one caught in the wild at the level
  it was met. Previously every record fielded at 100, so a lucky catch was worth as much as a
  three-billion-token graduation.
- **Flat fights.** Both sides scale to Lv 50 by default (the games' flat rules), so species,
  IVs, nature and types decide a battle instead of who has burned more tokens; `--raw` or the
  Battle tab's toggle keeps each Pokémon's own level. Cards are now version 2 and carry base
  stats and IVs so either side can re-level them exactly; version 1 cards are rescaled
  approximately and say so.
- **A wider, less repetitive wild.** Encounters draw from all 648 Gen 1-5 species rather than
  the 328 base forms an egg can hatch, so evolved Pokémon can turn up. A species already in the
  Pokédex is half as likely, the species you met last cannot appear twice running, and the
  egg's rarity guarantee no longer leaks into wild encounters.
- **Choose who fights.** Any Pokémon in the Pokédex can be fielded instead of the companion,
  from its species page ("Use in battle") or with `--with <name>` on `card` and `battle`. A
  record fights at level 100 with its saved IVs — the same numbers its species page shows —
  while the companion fights at the level it has grown to. The choice is remembered, shown on
  the Battle tab, and falls back to the companion if that species leaves the Pokédex.
- **Buddy.** A pin on the companion's own line is ignored, so the home card follows it through
  its evolutions instead of freezing on the form it was pinned at. Pin any owned Pokémon to the home card from its species page or with
  `poketoken buddy <name|#id>`; `--clear` goes back to following the companion. Display only —
  the companion keeps growing underneath and the progress bar names it. Ownership is required,
  so a pin cannot reveal an unseen species, and a pin to a species that later leaves the
  Pokédex is dropped automatically.

### Fixed
- **The window comes back when the display dies under it.** On WSL, WSLg's X server can crash
  when the session reconnects after a screen lock; Xlib then exits the Tk process on the spot and
  the window was simply gone. The background launcher (`poketoken app`, `toggle`, the Windows
  shortcut) now runs a small supervisor that owns the pid file, runs the window as a child, and
  when the child dies waits for the display to answer again before opening it anew. A clean
  close, or `poketoken close` while it is waiting, ends it; five quick deaths in a row make it
  give up and log why. Launching while the display is already hung now names the cause and the
  fix (`wsl --shutdown`) instead of "no window appeared".
- Species page: the whole evolution line, not just the forms on the record. A caught Quagsire
  shows Wooper before it; a caught Clamperl shows a pixelated preview of what it can still
  become. Forms up to the deepest one you own are sharp and named, the rest are pixelated
  previews with no name (one branch is drawn where a line forks, the one you own if any).
- Species page: the caption no longer trails off at "19.5…" on a narrow card; it wraps its size
  onto a second line.
- Battle record: each row now says who beat whom — "Quagsire beat Nidorino · 2 turns" — instead
  of an opponent and a trainer name, which told you nothing when every fight was against
  yourself. The challenger's trainer is named only when it is someone else.
- Species page: the disabled raise button says what it is for — "Raise · 783.2M short" — rather
  than a bare shortfall.
- On a narrow window the companion, its evolution, rewards and today's usage now share the first
  screen: the hero sprite is sized from the height left over after the other cards, between a
  96 px floor and the size chosen in the menu.
- Tooltips no longer outlive the pointer. A right-click grabs the pointer so no leave event ever
  arrives, which left the hover label on screen; it is now dismissed by the menu, by any click,
  by leaving the canvas and on quit.
- Battle result mapping. `simulate` now records each hit and the remaining HP **by side**, so a
  fight between two identical cards no longer depletes the wrong HP bar, mis-reports the winner's
  HP, or attributes every log line to both fighters. The RNG, damage and outcome are unchanged.

## v0.2.0 — 2026-09-08

### Game
- Activity history (120 days) backfilled from every Claude Code log; streaks (a day counts at 1M+ tokens) and a weekly goal (median of your previous weeks, floor 50M).
- Rare Candy from consistency instead of rate limits: streak milestones 3 / 7 / 14 / 30 days pay 1 / 2 / 3 / 5 candies (then 5 per further 30 days); beating the weekly goal pays 5.
- Stats: six hidden IVs per hatch, level 5→100 from growth, the games' stat formula with the nature's ±10 %, saved on Pokédex records at graduation.
- Luck: streaks (7 / 14 days) and cache-read efficiency (70 % / 90 %) add best-of IV rolls; a 7-day streak cuts the shiny denominator by a quarter. Rarity odds untouched.
- Ditto disguise (upstream port): 1/128 common multi-stage hatches reveal a Ditto at the first evolution threshold.
- Wild encounters: a streak day or a personal-best 5-hour block spawns a wild Pokémon; Poké / Great / Ultra Balls in the Shop; capture-rate based catches go straight into the Pokédex.
- Battle cards: `poketoken card` and `poketoken battle <card>` — deterministic, serverless fights with real type matchups.
- Battle tab in the window: copy your card, paste a challenger's, watch the fight turn by turn, keep a win/loss record.

### Window
- Species page (mini sprite header, stats card, evolution line, records) reachable from Home and from any Pokédex cell; the next evolution form shows pixelated and resolves with progress.
- Header sync indicator (Live / Refreshing / Sync failed) with a Refresh control; Home cards for Rewards, Cost by model and Tokens by project; wide or maximised windows lay Home out in balanced columns.
- Sprites fill a fixed container (256 / 320 / 384 / 448 px) using whole-number scaling; Home shows the companion, its evolution line and today's usage on the first screen.
- Encounter card with a Throw button; balls in Shop and Bag; refreshes coalesce; toasts for every event.
- Desktop notifications (Windows toast through PowerShell, `notify-send` on Linux) with an on/off switch.

### CLI and platform
- `app` opens in the background and returns the prompt (`--fg` attaches); `close`, `toggle`, single-instance raise.
- `history`, `stats`, `encounter`, `card`, `battle`, `notify`, `timer`, `autostart`, `export`, `import` commands.
- `timer on` installs a systemd user timer that refreshes every 15 minutes so progress accrues with the window closed; `autostart on` opens the window at Windows sign-in; `export`/`import` move the save between machines.
- Pricing per model from the current rate card, cache writes split by 5-minute and 1-hour TTL.

## v0.1.0 — 2026-09-07
- Initial port of PokeTokenBar's core for WSL, Linux and Windows: Claude Code log reader, token economy and companion loop, Shop and Bag, SwiftUI-styled Tk window, terminal CLI.
