# RetroAchievements Leaderboard — Design Spec

**Date:** 2026-06-14
**Status:** Approved (brainstorming) — ready for implementation plan
**Feature area:** From the RomM API review — "RetroAchievements depth" → an RA leaderboard.

## Overview

Add an on-demand `/ra-leaderboard [game]` slash command to the romm-comm Discord bot that ranks users by their RetroAchievements progress, sourced from RomM 4.9's RA integration. It produces a **global** board (across all of a user's RA progress) by default, and a **per-game** board when a game name is supplied.

The bot already surfaces RA minimally: a single "Achievements" link built from a ROM's `ra_id` in the search embed ([cogs/search.py:402-405](../../../cogs/search.py#L402-L405)). This feature is the first use of RomM's per-user RA progression data.

## Decisions (locked during brainstorming)

| Decision | Choice |
|----------|--------|
| Board shape | **Both**: global (default) + per-game (when a game arg is given) |
| Rank metric | **Total achievements earned** (`num_awarded`); other stats shown as columns |
| Delivery | **On-demand slash command only** (no auto-posted/updating board) |
| Roster | **Hybrid**: rank all RomM users with RA progression; decorate Discord-linked users with their Discord display name + 🔗 marker; show unlinked users by RomM username |
| Top N | **10** (footer shows total participants) |
| Identity | Discord **display name** (not a mention — avoids ping noise); 🔗 marker for linked |
| Tiebreak | hardcore earned → games mastered → name (alphabetical) |

## RomM 4.9 API surface (verified against the 4.9.0 source)

**Per-user** — `UserSchema` (returned by `GET /api/users` and `GET /api/users/{id}`, both require `Scope.USERS_READ`; the bot's token already requests `users.read`):
- `ra_username: str | None`
- `ra_progression: RAProgression | None` — wraps `RAUserProgression`:
  - `total: int`
  - `results: list[RAUserGameProgression]`, where each `RAUserGameProgression` has:
    - `rom_ra_id: int | None` — the game's RA id (matches a ROM's `ra_id`)
    - `max_possible: int | None` — achievements in the set
    - `num_awarded: int | None` — achievements earned
    - `num_awarded_hardcore: int | None`
    - `most_recent_awarded_date: str | None` (NotRequired)
    - `highest_award_kind: str | None` (NotRequired) — RA award tier (e.g. mastered/completed)
    - `earned_achievements: list[EarnedAchievement]`

**Per-game** — ROM detail (`GET /api/roms/{id}`, `roms.read`): `ra_id`, `ra_hash`, and `merged_ra_metadata` (wraps `RAMetadata`, whose `achievements: list[RAGameRomAchievement]` gives the achievement-set size for the per-game board's "X/Y" and footer).

**Not needed:** `POST /api/users/{id}/ra/refresh` (`ME_WRITE`) triggers a server-side RA refresh. The leaderboard is read-only and reflects whatever progression RomM has already stored; we do not call refresh.

**Bot-side mapping:** the `user_links` table (`discord_id, romm_username, romm_id`) and `db_manager.get_user_link(discord_id)` provide the Discord ↔ RomM mapping for the hybrid identity decoration ([cogs/user_manager.py:229](../../../cogs/user_manager.py#L229)).

### Verification carried into the plan

`ra_progression` is a field on `UserSchema` (default `None`). Confirm during implementation that the **list** endpoint `GET /api/users` actually populates it (not only `/users/{id}`). **Defined fallback:** if the list omits it, fetch per-user via `GET /api/users/{id}` with caching + light concurrency. Either path yields the same feature; only the call count differs. Likewise confirm the concrete `highest_award_kind` string values used to count "mastered" (treat values containing "master"/"complet" as mastery; refine to RA's exact tier strings during implementation).

## Command UX

```
/ra-leaderboard [game]
  • no arg     → global board
  • game given → per-game board (game name fuzzy-resolved via the existing search flow)
```

### Global board (`/ra-leaderboard`)
```
🏆 RetroAchievements Leaderboard — <Server Name>
Ranked by total achievements earned

#1   🔗 JakeD        1,204 🏆   · 812 hardcore · 14 mastered
#2   🔗 Maria        1,050 🏆   · 640 hardcore · 11 mastered
#3   retro_pat         980 🏆   · 300 hardcore ·  9 mastered
…
Top 10 of 22 players with RA progress · 🔗 = linked Discord member
```
Thumbnail: RomM logo (the existing logo URL used elsewhere in embeds).

### Per-game board (`/ra-leaderboard Sonic the Hedgehog 2`)
```
🏆 RetroAchievements — Sonic the Hedgehog 2 (Genesis)
Ranked by achievements earned · 50 achievements in this set

#1   🔗 JakeD        50/50 🏆  ✦ Mastered    · 50 hardcore
#2   🔗 Maria        47/50 🏆  ✦ Completed   · 12 hardcore
#3   retro_pat       31/50 🏆                ·  0 hardcore
…
8 of 22 players have played this game · 🔗 = linked Discord member
```
Thumbnail: the game's cover (reusing the existing cover-fetch path).

## Architecture & components

**New cog: `cogs/achievements.py`** — follows the existing cog pattern (`commands.Cog` subclass + module-level `setup(bot)`). Holds the `/ra-leaderboard` slash command (py-cord) with an optional `game: str` parameter, plus embed building.

**Pure ranking functions (module-level functions in `cogs/achievements.py`, importable by tests — matching the existing style `from cogs.scan import Scan`):**

- `compute_global_leaderboard(users, links) -> list[LeaderboardRow]`
  For each user with a non-empty `ra_progression.results`: `earned = sum(num_awarded)`, `hardcore = sum(num_awarded_hardcore)`, `mastered = count(highest_award_kind is a mastery tier)`. Attach identity from `links`. Include only users with `earned >= 1`. Sort by `earned` desc, then the tiebreak chain. Return rows (rank assigned by caller or within).

- `compute_game_leaderboard(users, links, rom_ra_id) -> list[LeaderboardRow]`
  For each user, select the `results` entry where `rom_ra_id` matches; skip users with no such entry. Rank by that entry's `num_awarded` (tiebreak: hardcore → name). Rows carry `earned`, `max_possible`, `num_awarded_hardcore`, `highest_award_kind`.

A `LeaderboardRow` is a small dataclass/dict: `display_name`, `is_linked`, `earned`, `hardcore`, `mastered` (global) or `earned`/`max_possible`/`hardcore`/`award_kind` (per-game). Keeping these as standalone pure functions isolates all logic from Discord so it is unit-testable with plain dicts.

**Identity resolution:** read `user_links` via `db_manager`, build `{romm_id|romm_username → discord_id}`. For each ranked RomM user, if linked, resolve the guild member's display name (fallback to RomM username if the member left the guild); else use the RomM username. Linked rows get the 🔗 marker.

**Game resolution (per-game mode):** reuse the existing search flow — resolve the `game` string to a ROM via `/api/roms?search_term=…` (+ platform list), take the **top match** (no disambiguation UI in v1), then fetch ROM detail for `ra_id` and `merged_ra_metadata` (set size + cover). Match users' `results` on `rom_ra_id == ra_id`.

## Data flow

**Global:**
1. `fetch_api_endpoint('users')` (cached) → all users with `ra_progression`.
2. Load `user_links` → identity map.
3. `compute_global_leaderboard(users, links)` → rows.
4. Build embed (top 10; footer = total participants).

**Per-game:**
1. Resolve `game` → ROM (top search match) → `ra_id` + `merged_ra_metadata` (set size, cover).
2. `fetch_api_endpoint('users')` (cached) + identity map.
3. `compute_game_leaderboard(users, links, ra_id)` → rows.
4. Build embed (top 10; footer = players who have played).

## Error & edge handling

- **No RA data anywhere** (no users with progression) → friendly empty-state embed ("No RetroAchievements progress is tracked yet").
- **Per-game, game not found** → reuse the search "couldn't find that game" response.
- **Per-game, game has no `ra_id`** → "That game isn't tracked on RetroAchievements."
- **Per-game, nobody has played** → "No one has RA progress on this game yet."
- **Users with `ra_username` but empty/zero progression** → excluded.
- **Linked member left the guild** → fall back to RomM username (unlinked styling).
- **Large `/api/users` payload** → rely on the bot's existing `APICache` TTL; top-10 cap on display.
- **Ties** → hardcore → mastered → alphabetical.

## Testing

Unit tests (stdlib `unittest`, `tests/` conventions) for the two pure functions, importing them directly from `cogs.achievements`:
- Global: ranking order by earned; tiebreak chain (hardcore, then mastered, then name); mastery counting from `highest_award_kind`; identity decoration (linked vs unlinked); exclusion of zero/empty users; empty-input → empty result.
- Per-game: filtering/selection by `rom_ra_id`; ranking by that game's `num_awarded`; users without an entry excluded; empty/edge inputs.

Embed building and command wiring are verified manually at runtime against a live RomM 4.9 instance (consistent with how the bot tests Discord I/O — logic is unit-tested, Discord side is manual).

## Out of scope (possible follow-ups)

- Auto-posted / periodically refreshed leaderboard message in a channel.
- Points-based (RA score) ranking.
- Per-game disambiguation UI when a name matches multiple ROMs.
- Per-user RA profile command, mastery-notification feed (separate items from the API review).
- Triggering RomM's RA refresh from the bot.
