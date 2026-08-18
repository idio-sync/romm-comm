# RetroAchievements Leaderboard — Design Spec

**Date:** 2026-06-14 (rev. 2026-06-15 after spec review)
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
| Tiebreak (global) | hardcore earned → games mastered → name (alphabetical) |
| Tiebreak (per-game) | hardcore earned → name (mastery is implied by `num_awarded == max_possible`, so it is intentionally dropped here) |

## RomM 4.9 API surface (verified against the 4.9.0 source)

**Per-user** — `UserSchema` (returned by `GET /api/users` and `GET /api/users/{id}`, both require `Scope.USERS_READ`):
- `ra_username: str | None`
- `ra_progression: RAProgression | None` — over the wire this is a **flattened JSON dict**, not a nested object. `RAProgression` is a TypedDict of `RAUserProgression`'s fields, all `NotRequired`, so both keys may be absent. Consume defensively: `prog = user.get("ra_progression") or {}; results = prog.get("results", [])`. Keys:
  - `total: int` (optional in the wire dict)
  - `results: list[RAUserGameProgression]` (optional in the wire dict), where each entry has:
    - `rom_ra_id: int | None` — the game's RetroAchievements id
    - `max_possible: int | None` — achievements in the set
    - `num_awarded: int | None` — achievements earned
    - `num_awarded_hardcore: int | None`
    - `most_recent_awarded_date: str | None` (NotRequired)
    - `highest_award_kind: str | None` (NotRequired) — RA award tier (string, no enum in source)
    - `earned_achievements: list[EarnedAchievement]`

**`ra_progression` is a stored JSON column** on the RomM user model (`backend/models/user.py`: `Mapped[dict | None] = mapped_column(CustomJSON(), default=dict)`), and `GET /users` serializes the full `UserSchema` per row (`[UserSchema.model_validate(u) for u in get_users()]`). So a **single `GET /api/users` call returns every user's full `ra_progression`** — no per-user calls needed. (This resolves the earlier open concern; the per-user fallback below is a defensive contingency only.)

**Per-game** — ROM detail/list (`roms.read`): `ra_id`, `ra_hash`, and `merged_ra_metadata` (type `RomRAMetadata`, a TypedDict of `RAMetadata`'s fields all-`NotRequired`; read `(merged_ra_metadata or {}).get("achievements", [])` for the set size — the object itself or the key may be absent). `ra_id` and `merged_ra_metadata` are on the base `RomSchema`, so search-list ROM items may already carry them. If metadata does not expose the set size, fall back to the matched progression row's `max_possible`; if that is also missing, omit the set-size line rather than guessing.

**Not needed:** `POST /api/users/{id}/ra/refresh` (`ME_WRITE`) triggers a server-side RA refresh. The leaderboard is read-only and reflects whatever progression RomM has already stored; we do not call refresh.

**Bot-side mapping:** the `user_links` table (`discord_id, romm_username, romm_id`) and `db_manager.get_user_link(discord_id)` provide the Discord ↔ RomM mapping ([cogs/user_manager.py:229](../../../cogs/user_manager.py#L229), `database_manager.py`).

### Auth requirement (important)

The feature needs `users.read` on the bot's RomM credential:
- **OAuth password grant** path: the bot already requests `users.read` ([bot.py](../../../bot.py), `get_oauth_token` scope string), so it works automatically.
- **Client API token** path (`ROMM_CLIENT_TOKEN`, the preferred/documented credential): scopes are **fixed when the token is minted in the RomM UI** — the bot cannot request them at runtime. If the token was created without `users.read`, `GET /api/users` returns 401/403 and the current `fetch_api_endpoint` helper returns `None` with no retry; it also returns `None` for transport/server/JSON failures. The command must treat `users` fetch → `None` as an error, not an empty board, and show a clear message such as "the bot can't read RomM users right now — check RomM connectivity and make sure `ROMM_CLIENT_TOKEN` includes `users.read`." The README already lists `users.read` in the `ROMM_CLIENT_TOKEN` scope list; keep that requirement intact.

### Verifications carried into the plan

1. **`rom_ra_id` ↔ `ra_id` identity space.** The per-game board matches a user's `RAUserGameProgression.rom_ra_id` against the resolved ROM's `ra_id`. RomM's RA sync is expected to populate both from the same RA game id, but verify with a known game during implementation (confirm `rom.ra_id == progression.rom_ra_id`). This is an implementation/manual-validation note only; an empty board for a tracked game is still treated as a normal "no one has played yet" result. **Observability (to avoid a silent failure mode):** the per-game path logs a `warning` when the resolved `ra_id` is non-null yet matches zero progression rows *while the global participant count is > 0* — a signature of a systematic `ra_id`/`rom_ra_id` mismatch rather than a genuinely unplayed game. The user-facing message stays "no one has played yet"; the warning log is the diagnostic surface, so a mismatch is observable instead of indistinguishable from an empty board.
2. **`highest_award_kind` mastery tiers.** Source types it `str | None` with no enum, so the exact strings are deferred. RA's real tiers are typically `beaten-softcore`, `beaten-hardcore`, `completed`, `mastered`. Count "mastered" as `highest_award_kind` matching (case-insensitive) `"mastered"` or `"completed"` — note that `"beaten-*"` intentionally does NOT count as mastery. Confirm the actual values RomM emits during implementation.
3. **List-population fallback (contingency).** Should `GET /api/users` ever omit `ra_progression` in practice, fall back to per-user `GET /api/users/{id}` with caching + light concurrency. Not expected, given it's a stored column.

## Command UX

```
/ra-leaderboard [game]
  • no arg     → global board
  • game given → per-game board (game name resolved via a new platform-agnostic search call)
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

When the set size is unknown (neither `merged_ra_metadata.achievements` nor a `max_possible` is available), the header omits the "· N achievements in this set" clause and rows show bare `num_awarded` 🏆 with no `/max` denominator.

## Architecture & components

**New cog: `cogs/achievements.py`** — follows the existing cog pattern (`commands.Cog` subclass + module-level `setup(bot)`, `@discord.slash_command`, `discord.ApplicationContext`, `discord.Option`). Holds the `/ra-leaderboard` slash command with an optional `game: str` parameter, plus embed building. The command must `await ctx.defer()` immediately before RomM/API work, then send/edit the final response, because both modes may perform multiple network calls and `/api/users` can be large.

**Pure ranking functions (module-level functions in `cogs/achievements.py`, importable by tests — matching the existing style `from cogs.search import ...`):**

- `compute_global_leaderboard(users, links) -> list[LeaderboardRow]`
  For each user, read `results = (user.get("ra_progression") or {}).get("results", [])`. If non-empty: `earned = sum(r.get("num_awarded") or 0)`, `hardcore = sum(r.get("num_awarded_hardcore") or 0)`, `mastered = count(r where highest_award_kind matches mastery tiers)`. Attach identity from `links`. Include only users with `earned >= 1`. Sort by `earned` desc, then global tiebreak chain.

- `compute_game_leaderboard(users, links, rom_ra_id) -> list[LeaderboardRow]`
  For each user, select the `results` entry where `rom_ra_id` matches; skip users with no such entry. Rank by that entry's `num_awarded` (per-game tiebreak: hardcore → name). Rows carry `earned`, `max_possible`, `num_awarded_hardcore`, `highest_award_kind`.

A `LeaderboardRow` is a small dataclass/dict: `display_name`, `is_linked`, plus `earned`/`hardcore`/`mastered` (global) or `earned`/`max_possible`/`hardcore`/`award_kind` (per-game). Standalone pure functions isolate all logic from Discord so it is unit-testable with plain dicts.

**Identity resolution:** read `user_links` via `db_manager`, build `{romm_id → discord_id}` (and a username map as fallback). For each ranked RomM user, if linked, resolve the guild member's display name (fallback to RomM username if the member left the guild); else use the RomM username. Linked rows get the 🔗 marker. **Bot-account exclusion is best-effort:** when username/password auth is used, exclude the configured `ROMM_USER`/`ROMM_USERNAME` if it appears in the roster; when `ROMM_CLIENT_TOKEN` is used, do not infer the token owner from the opaque token (client tokens are often created by a real admin user who should remain eligible). If a dedicated bot account needs exclusion under token auth, make that a follow-up explicit config rather than a hidden assumption.

**Game resolution (per-game mode) — NEW code, not a reused helper.** The existing `/search` resolution is inline inside the `search()` command and *requires* a `platform` argument, so it cannot be called here. The per-game path issues its own **platform-agnostic** call — `roms?search_term={quote_plus(game)}&limit={n}` (the RomM API does not require `platform_id`; the bot adds it only in `/search`) — takes the **top match** (no disambiguation UI in v1), and reads `ra_id` + `merged_ra_metadata` (set size) + `platform_id`/`url_cover` (for the cover thumbnail). RomM list/search responses are paginated dicts with `items`; parse `response["items"]` when present, and defensively handle a bare list the way existing search code does. Match users' `results` on `rom_ra_id == ra_id`. (Optionally extract a small shared resolver, but treat it as new code.)

## Data flow

**Global:**
1. `await ctx.defer()`.
2. `fetch_api_endpoint('users')` (cached). If it returns `None` → show the read/connectivity error (see Auth requirement).
3. Load `user_links` → identity map; exclude the configured username-auth RomM account if present.
4. `compute_global_leaderboard(users, links)` → rows.
5. Build embed (top 10; footer = total participants).

**Per-game:**
1. `await ctx.defer()`.
2. Resolve `game` → top search match from a URL-encoded, platform-agnostic `roms` query → `ra_id` + optional set size + `platform_id`/`url_cover` (cover).
3. `fetch_api_endpoint('users')` (cached; same `None` → read/connectivity error) + identity map.
4. `compute_game_leaderboard(users, links, ra_id)` → rows.
5. Build embed (top 10; footer = players who have played).

## Error & edge handling

- **`/api/users` returns `None`** (auth failure, network/server failure, timeout, or invalid JSON are collapsed by the current helper) → explicit "can't read RomM users; check token scope and connectivity" message, not an empty board.
- **No RA data anywhere** (users fetched, but none have progression) → friendly empty-state embed ("No RetroAchievements progress is tracked yet").
- **Per-game, game not found** → "couldn't find that game" response.
- **Per-game, game has no `ra_id`** → "That game isn't tracked on RetroAchievements."
- **Per-game, nobody has played** → "No one has RA progress on this game yet."
- **Users with `ra_username` but empty/zero progression** → excluded.
- **The configured username-auth RomM account** → excluded from the roster when available; client-token owner exclusion is not inferred.
- **Linked member left the guild** → fall back to RomM username (unlinked styling).
- **Large `/api/users` payload** → the global call pulls *all* users' *full* per-game progression (incl. `earned_achievements` lists) even though only aggregates are shown; rely on the bot's existing `APICache` TTL and the top-10 display cap.
- **Ties** → global: hardcore → mastered → alphabetical; per-game: hardcore → alphabetical.

## Privacy note (deliberate choice)

The hybrid roster ranks **all** RomM users with RA progression, including accounts never linked to a Discord member and shown by bare RomM username, in a board posted to a Discord channel. This is an intentional decision for v1. A `RA_LEADERBOARD_LINKED_ONLY` config flag (default off) to restrict the board to Discord-linked users is recorded as an out-of-scope follow-up for admins who object to listing unlinked accounts.

## Testing

Unit tests (stdlib `unittest`, `tests/` conventions, `IsolatedAsyncioTestCase` where needed) for the pure ranking/resolution helpers, importing them directly from `cogs.achievements`:
- Global: ranking order by earned; tiebreak chain (hardcore, then mastered, then name); mastery counting from `highest_award_kind` (case-insensitive; `beaten-*` excluded); identity decoration (linked vs unlinked); username-auth account exclusion; exclusion of zero/empty users; defensive handling of missing `ra_progression`/`results` keys; empty-input → empty result.
- Per-game: filtering/selection by `rom_ra_id`; ranking by that game's `num_awarded`; per-game tiebreak (hardcore → name); users without an entry excluded; empty/edge inputs.
- Resolution/auth helpers: URL-encode game queries; parse paginated `{"items": [...]}` and fallback list responses; handle `fetch_api_endpoint('users') is None` as an explicit read/connectivity error.

Embed building, command wiring, the read/connectivity error path, and game resolution are verified manually at runtime against a live RomM 4.9 instance (consistent with how the bot tests Discord I/O — logic is unit-tested, Discord/HTTP side is manual).

## Out of scope (possible follow-ups)

- `RA_LEADERBOARD_LINKED_ONLY` config flag (linked-users-only board).
- Auto-posted / periodically refreshed leaderboard message in a channel.
- Points-based (RA score) ranking.
- Per-game disambiguation UI when a name matches multiple ROMs.
- Per-user RA profile command, mastery-notification feed (separate items from the API review).
- Triggering RomM's RA refresh from the bot.
