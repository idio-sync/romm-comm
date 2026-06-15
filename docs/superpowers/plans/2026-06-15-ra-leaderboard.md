# RetroAchievements Leaderboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an on-demand `/ra-leaderboard [game]` slash command that ranks users by RetroAchievements progress (global by default, per-game when a game is given), sourced from RomM 4.9's RA data.

**Architecture:** A new cog `cogs/achievements.py` holds the slash command and embed building. Two **pure, Discord-free** module-level functions (`compute_global_leaderboard`, `compute_game_leaderboard`) do all ranking from plain dicts so they are unit-testable. The cog fetches users via the existing `fetch_api_endpoint('users')` (one call returns every user's stored `ra_progression`), builds a Discord-identity map from the `user_links` table, calls the pure function, and renders the embed. Username/password auth remains the only auth assumption that changes nothing; the feature only needs `users.read` (already requested by the OAuth grant and documented for client tokens).

**Tech Stack:** Python 3, py-cord, aiohttp (via the bot's shared session), aiosqlite (via `db_manager`); stdlib `unittest` for tests.

**Spec:** `docs/superpowers/specs/2026-06-14-ra-leaderboard-design.md` (read it for the full design, decisions, and verified API surface).

**Key codebase facts (verified):**
- Cog pattern: `import discord; from discord.ext import commands`; `class X(commands.Cog): def __init__(self, bot): self.bot = bot`; commands via `@discord.slash_command(name=..., description=...)` + `async def f(self, ctx: discord.ApplicationContext): await ctx.defer(); ... await ctx.respond(embed=...)`; file ends with `def setup(bot): bot.add_cog(X(bot))`. (See `cogs/info.py`.)
- Optional param: `game: discord.Option(str, "Game name", required=False, default=None)` (see `cogs/search.py:1921-1924`).
- DB handle in a cog: `self.db_manager = bot.db`. Methods: `await self.db_manager.get_all_user_links()` → `list[dict]` with keys `discord_id, romm_username, romm_id, discord_username, discord_avatar, created_by_bot, created_at, updated_at` (database_manager.py:709). 
- Users: `await self.bot.fetch_api_endpoint('users')` → `list[dict]` (RomM `UserSchema`s) or `None` on any failure (401/403/timeout/JSON). Each user dict has `id`, `username`, `ra_username`, `ra_progression` (a dict with optional `total`/`results`; each result has `rom_ra_id, max_possible, num_awarded, num_awarded_hardcore, highest_award_kind, earned_achievements`).
- Cog registration: add `'cogs.achievements'` to BOTH the `core_cogs` list (bot.py:816-824) and the `cog_dependencies` dict (bot.py:828-835).
- Config: `self.bot.config.USER` is the RomM username under password auth (or `None` under client-token-only). `self.bot.config.API_BASE_URL` is the RomM base URL.
- Logo URL (used as embed thumbnail elsewhere): `https://raw.githubusercontent.com/idio-sync/romm-comm/refs/heads/main/.backend/isotipo-small.png`
- Covers are served unauthenticated at `{API_BASE_URL}/assets/romm/resources/roms/{platform_id}/{rom_id}/cover/big.png` (verified via the 4.9 nginx config during the client-token work), so the per-game board can set the cover as a thumbnail **URL directly** — no file-attachment needed. (This is a deliberate, simpler refinement of the spec's "reuse cover-fetch path.")

**Run tests** from the repo root using the project venv interpreter:
`.venv\Scripts\python -m unittest tests.test_achievements -v`
> If implementing in a worktree with no local `.venv`, invoke the main checkout's interpreter by absolute path instead, e.g. `C:/Users/Jake/Git/romm-comm/romm-comm/.venv/Scripts/python.exe -m unittest tests.test_achievements -v`, run from the worktree root. The bare `python` on PATH lacks `discord`/`aiohttp` and will error with `ModuleNotFoundError`.

---

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| `cogs/achievements.py` | New cog: `/ra-leaderboard` command, two pure ranking functions, embed building | Create |
| `tests/test_achievements.py` | Unit tests for the two pure ranking functions | Create |
| `bot.py` | Register the new cog in `core_cogs` + `cog_dependencies` | Modify (bot.py:816-835) |
| `README.md` | Document the new command | Modify |

The two pure functions are module-level (not methods) so tests import them directly: `from cogs.achievements import compute_global_leaderboard, compute_game_leaderboard`.

---

## Task 1: `compute_global_leaderboard` (pure function) + tests

**Files:**
- Create: `cogs/achievements.py` (module-level functions + a `MASTERY_KINDS`/`_is_mastery` helper; no cog class yet)
- Create: `tests/test_achievements.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_achievements.py`:

```python
import unittest

from cogs.achievements import compute_global_leaderboard


def _user(uid, username, results, ra_username="ra"):
    return {
        "id": uid,
        "username": username,
        "ra_username": ra_username,
        "ra_progression": {"total": len(results), "results": results},
    }


def _result(num_awarded, hardcore=0, kind=None, rom_ra_id=1, max_possible=10):
    return {
        "rom_ra_id": rom_ra_id,
        "max_possible": max_possible,
        "num_awarded": num_awarded,
        "num_awarded_hardcore": hardcore,
        "highest_award_kind": kind,
    }


class GlobalLeaderboardTests(unittest.TestCase):
    def test_ranks_by_total_earned_desc(self):
        users = [
            _user(1, "alice", [_result(10), _result(5, rom_ra_id=2)]),   # 15
            _user(2, "bob", [_result(20)]),                              # 20
        ]
        rows = compute_global_leaderboard(users, {})
        self.assertEqual(["bob", "alice"], [r["name"] for r in rows])
        self.assertEqual(20, rows[0]["earned"])
        self.assertEqual(15, rows[1]["earned"])

    def test_tiebreak_hardcore_then_mastered_then_name(self):
        users = [
            _user(1, "zed", [_result(10, hardcore=2)]),
            _user(2, "amy", [_result(10, hardcore=2)]),  # same earned+hardcore → name asc
            _user(3, "kim", [_result(10, hardcore=9)]),  # higher hardcore → first
        ]
        rows = compute_global_leaderboard(users, {})
        self.assertEqual(["kim", "amy", "zed"], [r["name"] for r in rows])

    def test_counts_mastery_case_insensitive_excluding_beaten(self):
        users = [_user(1, "alice", [
            _result(10, kind="Mastered"),
            _result(10, kind="completed", rom_ra_id=2),
            _result(10, kind="beaten-hardcore", rom_ra_id=3),
            _result(10, kind=None, rom_ra_id=4),
        ])]
        rows = compute_global_leaderboard(users, {})
        self.assertEqual(2, rows[0]["mastered"])

    def test_linked_users_decorated_others_use_username(self):
        users = [_user(1, "alice", [_result(5)]), _user(2, "bob", [_result(3)])]
        links = {1: "AliceOnDiscord"}
        rows = compute_global_leaderboard(users, links)
        by_id = {r["romm_id"]: r for r in rows}
        self.assertEqual("AliceOnDiscord", by_id[1]["name"])
        self.assertTrue(by_id[1]["is_linked"])
        self.assertEqual("bob", by_id[2]["name"])
        self.assertFalse(by_id[2]["is_linked"])

    def test_excludes_bot_account_and_zero_and_empty_users(self):
        users = [
            _user(1, "alice", [_result(5)]),
            _user(2, "rommbot", [_result(99)]),          # bot account → excluded
            _user(3, "noprog", []),                        # empty results → excluded
            _user(4, "zero", [_result(0)]),                # zero earned → excluded
            {"id": 5, "username": "nullprog", "ra_progression": None},  # None → excluded
        ]
        rows = compute_global_leaderboard(users, {}, bot_username="RommBot")
        self.assertEqual(["alice"], [r["name"] for r in rows])

    def test_empty_input_returns_empty(self):
        self.assertEqual([], compute_global_leaderboard([], {}))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python -m unittest tests.test_achievements -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'cogs.achievements'` (or `ImportError` for `compute_global_leaderboard`).

- [ ] **Step 3: Create `cogs/achievements.py` with the helper and global function**

Create `cogs/achievements.py`:

```python
import logging

logger = logging.getLogger(__name__)

# RA award tiers that count as "mastered" for the leaderboard (case-insensitive).
# RA's real tiers include beaten-softcore / beaten-hardcore / completed / mastered;
# "beaten-*" intentionally does NOT count as mastery.
MASTERY_KINDS = {"mastered", "completed"}


def _is_mastery(kind) -> bool:
    return bool(kind) and str(kind).strip().lower() in MASTERY_KINDS


def _results_of(user) -> list:
    """Defensively read a user's RA progression results (flattened dict, keys may be absent)."""
    prog = user.get("ra_progression") or {}
    return prog.get("results") or []


def compute_global_leaderboard(users, links, bot_username=None):
    """Rank users by total RA achievements earned across all games.

    users: list of RomM UserSchema dicts (id, username, ra_progression).
    links: dict mapping romm user id -> resolved Discord display name (linked users only).
    bot_username: RomM username of the bot's own account to exclude (or None/empty to skip).
    Returns: list of row dicts sorted best-first. Each row:
        {romm_id, name, is_linked, earned, hardcore, mastered}
    """
    bot_name = (bot_username or "").strip().lower()
    rows = []
    for user in users:
        username = user.get("username") or ""
        if bot_name and username.strip().lower() == bot_name:
            continue
        results = _results_of(user)
        if not results:
            continue
        earned = sum((r.get("num_awarded") or 0) for r in results)
        if earned < 1:
            continue
        hardcore = sum((r.get("num_awarded_hardcore") or 0) for r in results)
        mastered = sum(1 for r in results if _is_mastery(r.get("highest_award_kind")))
        romm_id = user.get("id")
        linked_name = links.get(romm_id)
        rows.append({
            "romm_id": romm_id,
            "name": linked_name or username,
            "is_linked": linked_name is not None,
            "earned": earned,
            "hardcore": hardcore,
            "mastered": mastered,
        })
    rows.sort(key=lambda r: (-r["earned"], -r["hardcore"], -r["mastered"], r["name"].lower()))
    return rows
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python -m unittest tests.test_achievements -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add cogs/achievements.py tests/test_achievements.py
git commit -m "feat(ra): add compute_global_leaderboard ranking function"
```

---

## Task 2: `compute_game_leaderboard` (pure function) + tests

**Files:**
- Modify: `cogs/achievements.py` (add the per-game function)
- Modify: `tests/test_achievements.py` (add a test class; reuse the `_user`/`_result` helpers already defined)

- [ ] **Step 1: Write the failing tests**

Append this class to `tests/test_achievements.py` (and add the import at the top: change the import line to `from cogs.achievements import compute_global_leaderboard, compute_game_leaderboard`):

```python
class GameLeaderboardTests(unittest.TestCase):
    def test_filters_to_matching_game_and_ranks_by_earned(self):
        users = [
            _user(1, "alice", [_result(40, rom_ra_id=99, max_possible=50),
                               _result(5, rom_ra_id=7)]),
            _user(2, "bob", [_result(48, rom_ra_id=99, max_possible=50)]),
            _user(3, "carol", [_result(10, rom_ra_id=7)]),  # never played game 99
        ]
        rows = compute_game_leaderboard(users, {}, rom_ra_id=99)
        self.assertEqual(["bob", "alice"], [r["name"] for r in rows])
        self.assertEqual(48, rows[0]["earned"])
        self.assertEqual(50, rows[0]["max_possible"])

    def test_tiebreak_hardcore_then_name(self):
        users = [
            _user(1, "zed", [_result(10, hardcore=1, rom_ra_id=99)]),
            _user(2, "amy", [_result(10, hardcore=1, rom_ra_id=99)]),
            _user(3, "kim", [_result(10, hardcore=8, rom_ra_id=99)]),
        ]
        rows = compute_game_leaderboard(users, {}, rom_ra_id=99)
        self.assertEqual(["kim", "amy", "zed"], [r["name"] for r in rows])

    def test_carries_award_kind_and_excludes_zero_and_bot(self):
        users = [
            _user(1, "alice", [_result(50, kind="mastered", rom_ra_id=99, max_possible=50)]),
            _user(2, "zero", [_result(0, rom_ra_id=99)]),     # 0 earned → excluded
            _user(3, "rommbot", [_result(50, rom_ra_id=99)]),  # bot → excluded
        ]
        rows = compute_game_leaderboard(users, {1: "AliceD"}, rom_ra_id=99, bot_username="rommbot")
        self.assertEqual(1, len(rows))
        self.assertEqual("AliceD", rows[0]["name"])
        self.assertTrue(rows[0]["is_linked"])
        self.assertEqual("mastered", rows[0]["award_kind"])

    def test_no_matching_players_returns_empty(self):
        users = [_user(1, "alice", [_result(5, rom_ra_id=7)])]
        self.assertEqual([], compute_game_leaderboard(users, {}, rom_ra_id=99))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python -m unittest tests.test_achievements -v`
Expected: FAIL — `ImportError: cannot import name 'compute_game_leaderboard'`.

- [ ] **Step 3: Add the per-game function to `cogs/achievements.py`**

Append to `cogs/achievements.py` (after `compute_global_leaderboard`):

```python
def compute_game_leaderboard(users, links, rom_ra_id, bot_username=None):
    """Rank users by achievements earned for one specific game (matched on rom_ra_id).

    Returns rows sorted best-first; each row:
        {romm_id, name, is_linked, earned, max_possible, hardcore, award_kind}
    Per-game tiebreak is hardcore -> name (mastery is implied by earned == max_possible).
    """
    bot_name = (bot_username or "").strip().lower()
    rows = []
    for user in users:
        username = user.get("username") or ""
        if bot_name and username.strip().lower() == bot_name:
            continue
        entry = next((r for r in _results_of(user) if r.get("rom_ra_id") == rom_ra_id), None)
        if entry is None:
            continue
        earned = entry.get("num_awarded") or 0
        if earned < 1:
            continue
        romm_id = user.get("id")
        linked_name = links.get(romm_id)
        rows.append({
            "romm_id": romm_id,
            "name": linked_name or username,
            "is_linked": linked_name is not None,
            "earned": earned,
            "max_possible": entry.get("max_possible"),
            "hardcore": entry.get("num_awarded_hardcore") or 0,
            "award_kind": entry.get("highest_award_kind"),
        })
    rows.sort(key=lambda r: (-r["earned"], -r["hardcore"], r["name"].lower()))
    return rows
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python -m unittest tests.test_achievements -v`
Expected: PASS (10 tests total).

- [ ] **Step 5: Commit**

```bash
git add cogs/achievements.py tests/test_achievements.py
git commit -m "feat(ra): add compute_game_leaderboard ranking function"
```

---

## Task 3: Cog + global `/ra-leaderboard` command + registration

**Files:**
- Modify: `cogs/achievements.py` (add imports, the `RetroAchievements` cog class, shared helpers, global embed, `setup`)
- Modify: `bot.py:816-835` (register the cog)

No unit test (Discord/HTTP I/O); verified in Task 6. Verify the module imports cleanly by running the existing tests (they import `cogs.achievements`).

- [ ] **Step 1: Add imports and the cog class to `cogs/achievements.py`**

At the TOP of `cogs/achievements.py`, replace the current first two lines (`import logging` / `logger = ...`) with:

```python
import logging
import discord
from discord.ext import commands

logger = logging.getLogger(__name__)

RA_LOGO_URL = "https://raw.githubusercontent.com/idio-sync/romm-comm/refs/heads/main/.backend/isotipo-small.png"
TOP_N = 10
```

At the END of `cogs/achievements.py` (after the two pure functions), add the cog class and `setup`:

```python
class RetroAchievements(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db_manager = bot.db

    async def _build_links_map(self, ctx):
        """Return {romm_id: discord_display_name} for all linked users.

        Resolves the live guild member display name when possible, falling back to
        the stored Discord username, then the RomM username.
        """
        links = {}
        try:
            for link in await self.db_manager.get_all_user_links():
                romm_id = link.get("romm_id")
                if romm_id is None:
                    continue
                name = None
                member = ctx.guild.get_member(link["discord_id"]) if ctx.guild else None
                if member:
                    name = member.display_name
                links[romm_id] = name or link.get("discord_username") or link.get("romm_username")
        except Exception as e:
            logger.error(f"Failed to build user-links map: {e}")
        return links

    def _format_global_line(self, rank, row):
        marker = "🔗 " if row["is_linked"] else ""
        return (f"**#{rank}** {marker}**{row['name']}** — "
                f"{row['earned']:,} 🏆 · {row['hardcore']:,} hardcore · {row['mastered']} mastered")

    def _build_global_embed(self, rows):
        embed = discord.Embed(
            title="🏆 RetroAchievements Leaderboard",
            description="Ranked by total achievements earned",
            color=discord.Color.gold(),
        )
        lines = [self._format_global_line(i + 1, r) for i, r in enumerate(rows[:TOP_N])]
        embed.add_field(name="​", value="\n".join(lines), inline=False)
        embed.set_thumbnail(url=RA_LOGO_URL)
        embed.set_footer(text=f"Top {min(TOP_N, len(rows))} of {len(rows)} players with RA progress · 🔗 = linked Discord member")
        return embed

    @discord.slash_command(
        name="ra-leaderboard",
        description="RetroAchievements leaderboard (global, or per-game if you name a game)",
    )
    async def ra_leaderboard(self, ctx: discord.ApplicationContext,
                             game: discord.Option(str, "Game name for a per-game board", required=False, default=None) = None):
        await ctx.defer()
        users = await self.bot.fetch_api_endpoint('users')
        if users is None:
            await ctx.respond("❌ Can't read RomM users right now — check RomM connectivity and make sure the bot's token includes the `users.read` scope.")
            return

        bot_username = getattr(self.bot.config, "USER", None)
        links = await self._build_links_map(ctx)

        if game:
            await self._respond_game_leaderboard(ctx, users, links, bot_username, game)
            return

        rows = compute_global_leaderboard(users, links, bot_username=bot_username)
        if not rows:
            await ctx.respond("No RetroAchievements progress is tracked yet.")
            return
        await ctx.respond(embed=self._build_global_embed(rows))

    async def _respond_game_leaderboard(self, ctx, users, links, bot_username, game):
        # Implemented in Task 4.
        await ctx.respond("Per-game leaderboard coming soon.")


def setup(bot):
    bot.add_cog(RetroAchievements(bot))
```

- [ ] **Step 2: Register the cog in `bot.py`**

In `bot.py`, add `'cogs.achievements'` to the `core_cogs` list (bot.py:816-824) — add it after `'cogs.recent_roms'`:

```python
            'cogs.recent_roms',
            'cogs.achievements'
```

And add an entry to the `cog_dependencies` dict (bot.py:828-835) after the `recent_roms` line:

```python
            'cogs.recent_roms': ['aiosqlite'],
            'cogs.achievements': []
```

- [ ] **Step 3: Verify the module imports cleanly (existing tests still pass)**

Run: `.venv\Scripts\python -m unittest tests.test_achievements -v`
Expected: PASS (10 tests) — confirms `cogs/achievements.py` parses/imports with the new `discord`/cog code present.

- [ ] **Step 4: Commit**

```bash
git add cogs/achievements.py bot.py
git commit -m "feat(ra): add RetroAchievements cog with global /ra-leaderboard"
```

---

## Task 4: Per-game leaderboard path

**Files:**
- Modify: `cogs/achievements.py` (implement `_respond_game_leaderboard`, add `_build_game_embed`, add `urllib.parse` import)

No unit test (Discord/HTTP); verified in Task 6.

- [ ] **Step 1: Add the `urllib.parse` import**

In `cogs/achievements.py`, add to the top imports:

```python
from urllib.parse import quote_plus
```

- [ ] **Step 2: Implement game resolution + per-game embed**

In `cogs/achievements.py`, replace the placeholder `_respond_game_leaderboard` body with the following, and add `_build_game_embed` and `_format_game_line` methods to the cog:

```python
    def _format_game_line(self, rank, row):
        marker = "🔗 " if row["is_linked"] else ""
        mp = row.get("max_possible")
        count = f"{row['earned']}/{mp}" if mp else f"{row['earned']}"
        award = f" ✦ {row['award_kind'].replace('-', ' ').title()}" if row.get("award_kind") else ""
        return (f"**#{rank}** {marker}**{row['name']}** — "
                f"{count} 🏆{award} · {row['hardcore']:,} hardcore")

    def _build_game_embed(self, rom, rows):
        title = rom.get("name") or "Unknown game"
        platform = rom.get("platform_name")
        header_title = f"🏆 RetroAchievements — {title}" + (f" ({platform})" if platform else "")
        set_size = next((r["max_possible"] for r in rows if r.get("max_possible")), None)
        desc = "Ranked by achievements earned"
        if set_size:
            desc += f" · {set_size} achievements in this set"
        embed = discord.Embed(title=header_title, description=desc, color=discord.Color.gold())
        lines = [self._format_game_line(i + 1, r) for i, r in enumerate(rows[:TOP_N])]
        embed.add_field(name="​", value="\n".join(lines), inline=False)
        # Cover served unauthenticated at /assets/...; set directly as thumbnail when present.
        platform_id, rom_id = rom.get("platform_id"), rom.get("id")
        if rom.get("url_cover") and platform_id and rom_id:
            embed.set_thumbnail(url=f"{self.bot.config.API_BASE_URL}/assets/romm/resources/roms/{platform_id}/{rom_id}/cover/big.png")
        else:
            embed.set_thumbnail(url=RA_LOGO_URL)
        embed.set_footer(text=f"Top {min(TOP_N, len(rows))} of {len(rows)} players who have played this game · 🔗 = linked Discord member")
        return embed

    async def _respond_game_leaderboard(self, ctx, users, links, bot_username, game):
        search = await self.bot.fetch_api_endpoint(f"roms?search_term={quote_plus(game)}&limit=25")
        if isinstance(search, dict):
            items = search.get("items", [])
        elif isinstance(search, list):
            items = search
        else:
            items = []
        if not items:
            await ctx.respond(f"Couldn't find a game matching “{game}”.")
            return
        rom = items[0]
        ra_id = rom.get("ra_id")
        if not ra_id:
            await ctx.respond(f"**{rom.get('name', game)}** isn't tracked on RetroAchievements.")
            return

        rows = compute_game_leaderboard(users, links, ra_id, bot_username=bot_username)
        if not rows:
            # Diagnostic: distinguish a systematic ra_id/rom_ra_id mismatch from a genuinely unplayed game.
            any_progression = any((u.get("ra_progression") or {}).get("results") for u in users)
            if any_progression:
                logger.warning(
                    f"/ra-leaderboard '{game}' resolved ra_id={ra_id} but matched 0 progression rows "
                    f"while users have RA progress — possible ra_id/rom_ra_id id-space mismatch."
                )
            await ctx.respond(f"No one has RetroAchievements progress on **{rom.get('name', game)}** yet.")
            return
        await ctx.respond(embed=self._build_game_embed(rom, rows))
```

- [ ] **Step 3: Verify the module imports cleanly (existing tests still pass)**

Run: `.venv\Scripts\python -m unittest tests.test_achievements -v`
Expected: PASS (10 tests) — confirms the new methods parse/import.

- [ ] **Step 4: Commit**

```bash
git add cogs/achievements.py
git commit -m "feat(ra): add per-game /ra-leaderboard path with cover and id-mismatch diagnostic"
```

---

## Task 5: Document the command in the README

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add a feature/command note**

In `README.md`, find the "Configuration details" / features area and add a short bullet describing the command. Locate the section that lists bot features or commands (e.g. near where other cogs/commands are described) and add:

```markdown
- **`/ra-leaderboard [game]`** — RetroAchievements leaderboard. With no argument it ranks users across the server by total achievements earned (hardcore and games-mastered shown as columns); pass a game name for a per-game board. Discord-linked users show their server name with a 🔗 marker. Requires the bot's RomM token to have the `users.read` scope (already in the default `ROMM_CLIENT_TOKEN` scope list and the OAuth grant).
```

If the README has no command list, add a short "### RetroAchievements leaderboard" subsection under the features area with the same content.

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: document /ra-leaderboard command"
```

---

## Task 6: Manual integration verification (requires a running RomM 4.9+ instance with RA data)

No automated test can cover the live wire protocol, Discord rendering, game resolution, or the `ra_id`/`rom_ra_id` identity-space question. Run the bot against a real RomM instance that has at least one user with RetroAchievements progression and confirm each item.

- [ ] **Step 1: Global board** — Run `/ra-leaderboard` (no arg). Confirm the embed lists users ranked by total achievements, with hardcore/mastered columns, the RomM logo thumbnail, and the footer count. Linked users show their Discord display name + 🔗; unlinked show RomM username.
- [ ] **Step 2: Per-game board** — Run `/ra-leaderboard <a game that has RA achievements and that someone has played>`. Confirm the title shows the game (and platform), the "N achievements in this set" line, per-row `earned/max`, mastery `✦` markers, and the cover thumbnail renders.
- [ ] **Step 3: `ra_id` ↔ `rom_ra_id` identity check** — Pick a game a known user has progress on. Confirm that user appears on the per-game board. If the per-game board is empty for a game people have actually played, check the logs for the "possible ra_id/rom_ra_id id-space mismatch" warning (Task 4) — that indicates the two ids are not the same space and the matching key needs adjustment.
- [ ] **Step 4: Auth/permission path** — If feasible, point the bot at a `ROMM_CLIENT_TOKEN` minted **without** `users.read` (or otherwise force a 401 on `/api/users`) and confirm `/ra-leaderboard` shows the "can't read RomM users… `users.read` scope" message, not an empty board.
- [ ] **Step 5: Empty states** — Confirm the global empty-state message on an instance with no RA progression, the "couldn't find that game" message for a nonsense game name, and the "isn't tracked on RetroAchievements" message for a game with no `ra_id`.
- [ ] **Step 6: Bot-account exclusion** — Confirm the bot's own RomM account (under `ROMM_USER`/`ROMM_PASS` auth) does not appear on the board. (Under client-token-only auth this exclusion is intentionally not applied — note the behavior.)
- [ ] **Step 7: Record results** in the PR description.

---

## Self-Review Notes

- **Spec coverage:** Global board (Task 1, 3) · per-game board (Task 2, 4) · rank by total earned with other stats as columns (Tasks 1-2 row shape, Tasks 3-4 embeds) · on-demand command (Task 3) · hybrid roster with 🔗 decoration (`_build_links_map` + pure-function `links`) · top-10 (`TOP_N`) · tiebreaks (both functions) · auth `users.read` + error path (Task 3, README Task 5) · platform-agnostic NEW game resolution (Task 4) · `rom_ra_id`↔`ra_id` diagnostic log (Task 4) · degraded set-size display (`_build_game_embed`) · cover-as-URL (Task 4) · bot-account exclusion (`bot_username`) · privacy (all-users roster, by design) · payload reliance on `APICache`. All spec sections map to a task.
- **No placeholders:** every code step shows complete code; Task 3's `_respond_game_leaderboard` placeholder is explicitly replaced in Task 4.
- **Type/name consistency:** row dict keys (`romm_id`, `name`, `is_linked`, `earned`, `hardcore`, `mastered` / `max_possible`, `award_kind`) are identical between the functions (Tasks 1-2) and the embed formatters (Tasks 3-4). `compute_global_leaderboard`/`compute_game_leaderboard` signatures match their imports and call sites. `links` is `{romm_id: display_name}` consistently in `_build_links_map`, both functions, and tests. `bot_username` threaded consistently.
