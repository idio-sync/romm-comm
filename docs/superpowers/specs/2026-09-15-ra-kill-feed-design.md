# RetroAchievements Kill Feed — Design Spec

**Date:** 2026-09-15
**Feature area:** RetroAchievements depth — a live unlock feed, building on the existing `/ra-leaderboard`.

## Summary

Post a Discord message the moment a linked member unlocks a RetroAchievements achievement: badge art, title, description, points, and the game it came from. One embed per unlock.

The bot already reads RomM's RA data for `/ra-leaderboard` ([cogs/achievements.py](../../../cogs/achievements.py)), which consumes per-game aggregate counts from a single `GET /api/users`. This feature is the first use of the per-achievement detail inside that same payload, and the first RA surface that posts on its own rather than on command.

Everything needed comes from RomM. No RetroAchievements API key, and no new HTTP plumbing.

## Decisions (locked during brainstorming)

| Decision | Choice |
|----------|--------|
| Event granularity | **Every achievement unlock** — a true kill feed, not milestones or roll-ups |
| Roster | **Discord-linked users only** (`user_links`); consent comes from having linked, so no opt-out table or command |
| Presentation | **One embed per unlock**, with flood control rather than a format change |
| Freshness | **Tiered**: active players refreshed every ~60s, idle every ~10min |
| Refresh lever | Bot calls `POST /api/users/{id}/ra/refresh` to drive RomM's own RA sync |
| Cold start | **Seed silently** — record existing unlocks, post nothing |
| Module layout | Grow `cogs/achievements.py` into a `cogs/achievements/` package |
| Metadata strategy | **Lazy per-game resolution with permanent cache** — never a full library walk |

## Verified API surface

Everything below was confirmed against the live RomM instance on 2026-09-15, not inferred from source. Where an earlier spec's assumption proved wrong, that is called out.

### Per-user — `GET /api/users` (scope `users.read`)

Returns every user with a flattened `ra_progression` dict. Observed user keys include `id`, `username`, `ra_username`, `ra_progression`.

`ra_progression` → `{total, results}`. Each entry in `results` has **exactly** these keys:

```
rom_ra_id: int                    # the game's RetroAchievements id
max_possible: int                 # achievements in the set
num_awarded: int
num_awarded_hardcore: int
most_recent_awarded_date: str     # ISO 8601, e.g. '2025-05-29T00:55:48+00:00'
earned_achievements: list[dict]
```

Each `earned_achievements` entry is **only**:

```json
{ "id": "89302", "date": "2025-04-19 01:56:36" }
```

**This is the single most important finding.** The list carries no title, description, points, badge, or per-unlock hardcore flag. Two consequences:

1. **Dedup is clean.** `id` is stable and `date` is the true unlock time, so `(romm_user_id, achievement_id)` is a sound primary key and events can be ordered correctly.
2. **Rendering requires a second source.** A named, illustrated embed cannot be built from this payload alone.

**Correction to the prior spec:** `highest_award_kind` is documented in [2026-06-14-ra-leaderboard-design.md](2026-06-14-ra-leaderboard-design.md) but is **absent** from result rows on the live instance. See *Out of scope* below.

### Per-game metadata — `GET /api/roms/{id}` (scope `roms.read`)

`merged_ra_metadata.achievements[]` supplies everything the embed needs. A verified entry:

```json
{
  "ra_id": 181424,
  "title": "The Girl",
  "description": "Complete Dream mode with the Christmas setting as Claris.",
  "points": 3,
  "num_awarded": 375,
  "num_awarded_hardcore": 323,
  "badge_id": "201886",
  "badge_url": "https://media.retroachievements.org/Badge/201886.png",
  "badge_url_lock": "https://media.retroachievements.org/Badge/201886_lock.png",
  "badge_path": "/assets/romm/resources/roms/37/33618/retroachievements/badges/201886.png",
  "display_order": 1,
  "type": "progression"
}
```

`badge_url` points at RA's CDN and is directly usable as an embed thumbnail. `num_awarded` / `num_awarded_hardcore` here are **global across all RA users**, which makes a rarity line possible.

**Join hazard — type mismatch.** The unlock's `id` is a **string** (`"89302"`); metadata's `ra_id` is an **int** (`181424`). Normalize both to `str` at the boundary. A silent type mismatch here yields a feed where every single unlock renders degraded, so it must be covered by a unit test.

### Finding a ROM by its RA id

There is **no `ra_id` filter**, and RomM **silently ignores unknown query parameters** — `GET /api/roms?ra_id=14531` returned the full unfiltered library of 137,922 items with `total=137922`. Any code that assumes such a filter works will appear to succeed while operating on the wrong data.

`order_by=ra_id&order_dir=desc` **does** work, sorting RA-tracked ROMs to the front with a clean terminator at the first null `ra_id`.

Measured cost of walking that ordered prefix:

| Metric | Value |
|---|---|
| RA-tracked ROMs | 6,320 |
| Requests at `limit=250` | 26 |
| Elapsed | 27.8s |
| **Payload transferred** | **133.0 MB** |

The payload is large because list items embed full `merged_ra_metadata`. **A full walk is rejected as a routine operation.**

## Architecture

### Module layout

`cogs/achievements.py` becomes `cogs/achievements/`. The extension name `cogs.achievements` is unchanged, so [bot.py](../../../bot.py)'s load list needs no edit.

| Module | Responsibility | Depends on |
|---|---|---|
| `__init__.py` | `setup()`, schedules the poller | discord |
| `cog.py` | `/ra-leaderboard` (moved verbatim), `/ra-feed` admin command | discord, bot |
| `leaderboard.py` | Existing `compute_*_leaderboard` pure functions, moved | nothing |
| `diff.py` | Pure: stored state + fresh payload + links → `list[UnlockEvent]` | nothing |
| `catalog.py` | `rom_ra_id` → achievement metadata; lazy resolution + caching | repo, fetcher callable |
| `poller.py` | The tiered loop, flood control, posting | all of the above |
| `embeds.py` | `build_unlock_embed` | discord |
| `repo.py` | Every query against the three new tables | db |

`diff.py`, `leaderboard.py` and `embeds.py` take plain data and need neither network nor a live bot, which is what makes the logic testable — the same property [tests/test_achievements.py](../../../tests/test_achievements.py) already relies on.

**`repo.py` is not optional.** [tests/test_sql_boundaries.py](../../../tests/test_sql_boundaries.py) is an enforced architecture test: a non-repository module may not call `.execute()` at all. New modules must not be added to its `ALLOWED` debt list, which "is meant to shrink."

### Schema

Table creation follows the existing convention — `CREATE TABLE` statements live in [database_manager.py](../../../database_manager.py) as a new `_create_ra_feed_tables`, called from the init sequence alongside the others, with the three names added to `verify_tables_exist`. Queries live in `cogs/achievements/repo.py`, mirroring [cogs/requests/repo.py](../../../cogs/requests/repo.py).

```sql
CREATE TABLE IF NOT EXISTS ra_seen_unlocks (
    romm_user_id   INTEGER NOT NULL,
    achievement_id TEXT    NOT NULL,
    rom_ra_id      INTEGER,
    earned_at      TEXT,
    posted_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (romm_user_id, achievement_id)
);

CREATE TABLE IF NOT EXISTS ra_game_index (
    rom_ra_id   INTEGER PRIMARY KEY,
    rom_id      INTEGER,
    platform_id INTEGER,
    rom_name    TEXT,
    resolved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ra_achievement_meta (
    rom_ra_id           INTEGER NOT NULL,
    achievement_id      TEXT    NOT NULL,
    title               TEXT,
    description         TEXT,
    points              INTEGER,
    badge_url           TEXT,
    type                TEXT,
    num_awarded         INTEGER,
    num_awarded_hardcore INTEGER,
    PRIMARY KEY (rom_ra_id, achievement_id)
);
```

`achievement_id` is `TEXT` in both tables because the wire format is a string; normalizing to one type at the storage boundary is what prevents the int/str join hazard from recurring.

### Data flow, one tick

1. **Select hot users.** A linked user is *hot* if any progression row's `most_recent_awarded_date` falls within the last 30 minutes. Hot users get `POST users/{id}/ra/refresh`; cold users get one only if their last refresh was over `RA_FEED_COLD_RATE` ago.
2. **Read.** `GET /api/users` with `bypass_cache=True`.
3. **Restrict** to users present in `user_links`.
4. **Diff** (`diff.py`, pure) against `ra_seen_unlocks` → `UnlockEvent(romm_user_id, achievement_id, rom_ra_id, earned_at)`.
5. **Cold-start guard.** A user with zero rows in `ra_seen_unlocks` is *seeded*: every current unlock is recorded and nothing is posted.

   **Accepted edge case:** a member whose very first unlock lands in the same tick that first sees them is seeded rather than announced, so their debut is missed. Distinguishing "new to the bot" from "new to RA" is not possible from the payload — there is no account-creation signal — and silently swallowing one unlock is strictly better than the alternative failure, which dumps every historical achievement into the channel.
6. **Resolve metadata** for each distinct `rom_ra_id` (see below).
7. **Order** events by `earned_at` ascending, so the channel reads chronologically.
8. **Post** one embed per event, paced, capped.
9. **Record** each row in `ra_seen_unlocks` only after its embed sends.

**Why tiering is only about refreshes.** `GET /api/users` returns *all* users' progression in a single call, so there is no per-user read cost and therefore no "cold lap" to save. The tier governs only how often the bot asks RomM to re-pull from RA. This is a simplification the spike produced; the original framing assumed a per-user read.

### Metadata resolution (`catalog.py`)

On an unseen `rom_ra_id`:

1. Check `ra_achievement_meta`. Hit → done, no network.
2. Check `ra_game_index` for `rom_ra_id → rom_id`.
3. On a miss, **binary-search** the `ra_id`-ordered list: `GET /api/roms?limit=1&offset=N&order_by=ra_id&order_dir=desc`. The prefix is sorted descending over ~6,320 items, so ~13 requests of one item each locate the ROM. Bound the search and give up gracefully rather than looping.
4. `GET /api/roms/{rom_id}` once; store every `merged_ra_metadata.achievements[]` entry into `ra_achievement_meta`.

Achievement metadata is static, so a game costs this once, ever. After warmup the feed makes one `GET /api/users` per tick and nothing else.

This deliberately avoids the 133 MB walk. It also trades a large fixed cost for a small variable one, which suits a feed that only ever touches the handful of games linked members actually play.

### Embed

- **Author:** Discord display name + avatar of the linked member (display name, not a mention — the leaderboard made the same call to avoid ping noise).
- **Thumbnail:** `badge_url`.
- **Title:** achievement `title`. **Description:** achievement `description`.
- **Fields:** points; game name linked to `https://retroachievements.org/game/{rom_ra_id}`; set progress `N/M` from the progression row.
- **Rarity line:** `num_awarded` from metadata, phrased as a global count (e.g. "375 players have this") — honest about what the number is, since the payload gives no total-player denominator.
- **Colour:** gold for low-`num_awarded` achievements, standard otherwise.
- **Timestamp:** the unlock's `date`, not post time.

**Not renderable:** per-unlock hardcore status. The payload exposes hardcore only as a per-game aggregate, so no embed may claim an individual unlock was hardcore.

### Flood control

One embed per unlock is kept. Protection is pacing and a cap: events are sent ~1.2s apart, up to `RA_FEED_MAX_PER_TICK` per tick; the remainder collapses into a single "…and N more" summary embed, and every skipped row is still recorded as seen so it never re-posts.

Marking a row seen *after* its embed sends means a crash mid-batch resumes where it stopped rather than silently swallowing the rest.

### Configuration

| Variable | Default | Meaning |
|---|---|---|
| `RA_FEED_CHANNEL_ID` | *unset* | Target channel. **Unset disables the feature entirely.** |
| `RA_FEED_POLL_RATE` | `60` | Seconds between ticks |
| `RA_FEED_COLD_RATE` | `600` | Seconds between refreshes for idle users |
| `RA_FEED_MAX_PER_TICK` | `25` | Embed cap before collapsing to a summary |
| `RA_FEED_ENABLE_REFRESH` | `true` | Whether to send refresh POSTs at all |

Opt-in by default: with no channel configured, the poller never starts.

### Error handling

| Condition | Behaviour |
|---|---|
| `GET /api/users` → `None` | Skip the tick entirely, **no state change**. A failed read must never be read as "everyone lost their achievements". |
| Refresh POST fails | Log at debug, continue read-only. Freshness degrades; the feed does not break. |
| Metadata unresolvable | Post a degraded embed (`Achievement #89302`) and **still mark seen**, so it cannot retry forever. |
| Channel missing or forbidden | Log one error, disable the poller for the process. |
| Unlock for an unlinked user | Ignored, and not recorded — if they link later, the cold-start seed covers them. |

`fetch_api_endpoint` caches by endpoint, so the poller **must** pass `bypass_cache=True`; without it the loop would poll its own stale cache indefinitely and the feed would appear permanently empty.

## Testing

Unit tests, no network and no Discord, matching the existing style:

- `diff.py`: new unlock detected; already-seen suppressed; cold-start seeds without emitting; unlinked user ignored; out-of-order dates sorted; **`"89302"` (str) matches `89302` (int)**; malformed/absent `earned_achievements` tolerated.
- `catalog.py`: cache hit makes no calls; miss resolves then caches; unresolvable game degrades rather than raising; binary search terminates on a bounded fake.
- `embeds.py`: renders with full metadata; renders degraded without it; never asserts hardcore.
- `repo.py`: round-trips against a temp SQLite file.
- The existing `tests/test_achievements.py` must keep passing with an updated import path.

**Manual validation** (no test can cover these): end-to-end latency against a real unlock; that `ME_WRITE` permits refreshing *other* users' records rather than only the caller's; that the binary search finds a known ROM; that badge URLs render in Discord; and that a first run on a populated instance posts **nothing**.

## Risks

- **`ME_WRITE` may be self-only.** If `POST /users/{id}/ra/refresh` refuses other users, tiering silently buys nothing and freshness falls back to RomM's own sync schedule. The feed still works; it is just slower. This is the single largest unverified assumption and is the first thing the plan should check.
- **`GET /api/users` payload growth.** It returns every user's full progression including every `earned_achievements` entry. At 12 users this is trivial; a large instance polling every 60s may need the poll rate raised.
- **Offset-based binary search** assumes a stable ordering during the search. A library being written concurrently could misplace a lookup; the failure mode is one degraded embed, and the next tick re-resolves.
- **A busy weekend** can still produce sustained output at one embed per unlock. The cap bounds each tick, not the day.

## Out of scope

- **`highest_award_kind` is absent** from live progression rows, so `/ra-leaderboard`'s "mastered" column is silently always `0` on this instance. Real bug, unrelated to the feed, and deliberately **not** fixed here — it needs its own investigation into whether RomM ever populates the field.
- Per-unlock hardcore marking — not derivable from the payload.
- Opt-out commands, milestone-only modes, roll-up digests, and a full library metadata index: all considered and rejected during brainstorming.
