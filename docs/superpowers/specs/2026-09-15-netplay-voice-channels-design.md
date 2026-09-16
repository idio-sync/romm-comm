# Netplay Temporary Voice Channels — Design

**Date:** 2026-09-15
**Status:** Approved for implementation planning
**Scope:** A temporary Discord voice channel created alongside each `/netplay`
announcement, reaped automatically once it falls out of use.

---

## 1. Summary

`/netplay` announces a session and keeps the post current, but the people it
gathers have nowhere to talk. Netplay over WebRTC carries no voice, and
EmulatorJS has no chat, so the coordination that makes a session work — who is
hosting, who is ready, who just lost — happens nowhere or in the text channel
the announcement is sitting in.

This adds one temporary voice channel per announcement. It is created with the
post, surfaced on the post and on the Join reply, and deleted on its own once
nobody is using it.

The bot never joins the channel. It creates it, links to it, and later deletes
it.

---

## 2. Lifecycle

The decision that shapes everything else: **the voice channel's life is not the
announcement's life.** A watcher is garbage-collected the tick it goes terminal;
a conversation that is still going when the game ends should not be.

So voice channels get their own registry, keyed by channel id, independent of
`Netplay.watchers`.

### 2.1 Creation

At `/netplay`, inside `announce()`, **before the embed is built**.

Eager creation means people can gather in voice while waiting for the host to
open the room — the state `watcher.py` already identifies as the one carrying
the real value, because it is when someone is deciding whether to join.

The ordering within `announce()` is not incidental. That function already
captures `render_key(watcher)` *before* sending, because a poll can advance the
watcher while the message is in flight. If `voice_channel_id` is not on the
watcher before the first render, the voice field appears only on a later edit,
and `render_key` has to churn to produce one. Creating first removes the problem
rather than handling it.

### 2.2 The reap rule

`reap()` is pure in the sense `watcher.advance()` is pure: it takes state and a
clock, mutates the record, and returns a decision. No Discord, no IO, no clock
of its own.

```
reap(room, *, occupied, watcher_alive, now, empty_timeout, max_lifetime) -> bool
```

Rows are evaluated **in the order listed**. The first match wins.

| # | Situation | Outcome |
|---|---|---|
| 1 | Age exceeds `max_lifetime` | Delete, whatever else is true |
| 2 | Someone is in it | Keep. Stamp `last_occupied_at = now`, `ever_occupied = True` |
| 3 | `ever_occupied`, now empty | Delete once `now - last_occupied_at >= empty_timeout` |
| 4 | Never occupied, `rom_id is None` | Delete — adopted room that nobody ever entered |
| 5 | Never occupied, watcher alive | Keep |
| 6 | Never occupied, watcher gone | Delete |

Row 1 is first on purpose: it is a leak guard, not a policy, and it has to
outrank row 2 or an occupied-but-wedged channel is immortal. Discord does not
guarantee an AFK disconnect, so this is the only backstop. Its default is
generous because it does delete an occupied channel.

Rows 4–6 handle a channel nobody has ever entered. **Why they exist:** creating
eagerly means most channels are never entered — the announcement expires, nobody
wanted voice. Governed by a flat empty-timer measured from creation, those
channels would be deleted five minutes after the post goes up, before the host
had opened the room. So a channel nobody has ever entered lives exactly as long
as its announcement. The moment anyone joins, it detaches permanently and is
governed only by the empty-timer. Independence begins when the channel is
actually used.

Row 4 exists because an adopted room (§5) has no watcher to be alive. It cannot
be reached by a room that is in use — row 2 catches that first — so it only ever
fires on a channel that was occupied when the bot started and emptied before the
first reaper tick.

### 2.3 `watcher_alive` is an identity test, not a key lookup

```python
watcher_alive = room.watcher is not None and cog.watchers.get(room.rom_id) is room.watcher
```

`VoiceRoom` holds a reference to the watcher it was born for, and the test is
`is`, not `in`. `cog.py` does this in all four of its own teardown paths, with a
comment explaining why: a fresh `/netplay` for the same ROM can install a new
watcher under the same key during any await. Keying on `rom_id` alone would make
an orphaned channel read `watcher_alive=True` against a *different*
announcement's watcher and survive to `max_lifetime`.

Two cases make that reachable, not theoretical:

- **The `announce()` failure path.** When the message fails to post, `cog.py`
  deletes the watcher to release the capacity slot. The channel created moments
  earlier must be deleted in that same handler (§2.5). If it were left to the
  reaper and a retry `/netplay` registered a new watcher first, a key lookup
  would keep the orphan alive for the full 12 hours beside the new channel.
- **Re-announcement.** A terminal watcher is only GC'd once its final edit is
  *delivered*, and `live_watcher_for` already returns `None` for a terminal
  watcher still in the registry — so a second `/netplay` for that ROM is
  accepted and overwrites `self.watchers[rom_id]`. Under a key lookup the old
  channel would be kept alive by the new watcher.

### 2.4 Deletion clears the post

On successful deletion, if the watcher is still live, **clear
`watcher.voice_channel_id`.**

This is not optional bookkeeping. `NETPLAY_VOICE_EMPTY_TIMEOUT` is 300s;
`NETPLAY_PENDING_TIMEOUT` is 900s and a LIVE session runs far longer. So the
ordinary case is that people use voice, leave, and the channel is reaped five
minutes later while the announcement is still up. Without this the embed goes on
rendering a `<#id>` that Discord displays as `#deleted-channel`, and the §8 link
button points at nothing.

Because §3.2 puts `voice_channel_id` in `render_key`, clearing it self-heals for
free: the next `refresh_message` notices the key changed and edits the field
away. This is also the only thing that makes `voice_channel_id` a value that
ever *changes* in production — set once before the first render, it could
otherwise never trigger an edit at all.

### 2.5 Deletion mechanics

`channel.delete()`, and drop the registry entry on any outcome that cannot be
retried into success:

- `bot.get_channel(id)` returns `None` — uncached or already gone. Drop the
  entry. `refresh_message` has an explicit, commented branch for exactly this;
  without one the entry leaks until `max_lifetime`.
- `NotFound` — deleted by hand. Success.
- `Forbidden` — permissions changed under us. Log once, drop the entry rather
  than retrying every tick forever.
- Other `HTTPException` — leave it in the registry and retry next tick.

---

## 3. Components

### 3.1 `cogs/netplay/voice.py`

```python
@dataclass
class VoiceRoom:
    channel_id: int
    guild_id: int
    rom_id: Optional[int]                     # None for a room adopted by the sweep
    watcher: Optional["NetplayWatcher"]       # identity for §2.3; None when adopted
    created_at: float
    ever_occupied: bool = False
    last_occupied_at: Optional[float] = None
```

`reap()` as above.

`NetplayVoice` holds the bot, the registry, and every Discord call:

- `ensure_category(guild)` — find or create, the way `cogs/info.py` does.
- `create_for(watcher, guild)` — returns the channel or `None`.
- `sweep_all()` — startup adoption, §5.
- `tick(now, watchers)` — one reaper pass over the registry.

### 3.2 Changes to existing files

| File | Change |
|---|---|
| `bot.py` | Four config lines beside the existing `NETPLAY_*` block |
| `cogs/netplay/watcher.py` | `voice_channel_id: Optional[int] = None` |
| `cogs/netplay/embeds.py` | Voice field, and the same id in `render_key` |
| `cogs/netplay/cog.py` | Instantiate; create in `announce`; delete on the announce failure path; reap in `tick`; sweep via `create_task`; voice button on the Join reply |
| `README.md` | Netplay section: the four settings, the category warning, the reserved marker |

`voice_channel_id` goes into **both** the embed and `render_key`. The
`embeds.py` module docstring states the invariant directly: a field the embed
shows but the key omits means the post silently stops updating.

---

## 4. Configuration

| Variable | Default | Meaning |
|---|---|---|
| `NETPLAY_VOICE_ENABLED` | `false` | Master switch |
| `NETPLAY_VOICE_CATEGORY` | `Netplay` | Category name, auto-created |
| `NETPLAY_VOICE_EMPTY_TIMEOUT` | `300` | Seconds empty before deletion |
| `NETPLAY_VOICE_MAX_LIFETIME` | `43200` | Hard cap, 12 hours |

`NETPLAY_VOICE_ENABLED` defaults to **false**, unlike `NETPLAY_ENABLED`.
Automatically creating and deleting channels in someone's server is invasive
enough that existing deployments should opt in deliberately rather than
discover it after an upgrade.

**Voice requires `NETPLAY_ENABLED=true`.** The reaper runs inside
`poll_sessions`, which only starts when the cog is enabled, so voice with
netplay off would sweep on boot and then never reap. Treat
`NETPLAY_VOICE_ENABLED` as ignored when `NETPLAY_ENABLED` is false, and log it.

---

## 5. Startup sweep

Watchers are in-memory; channels are not. A restart forgets every channel the
bot made, and without recovery a nightly-restarting bot fills the category with
dead channels.

Rather than persisting disposable state to SQLite, **the category is the
state.** `sweep_all()` walks the category's `voice_channels` in each guild —
`category.voice_channels`, explicitly, which filters to `VoiceChannel` and so
excludes text and stage channels; `category.channels` would not — considering
only names carrying the reserved marker (§5.1) and ignoring everything else:

- **Empty channel** → delete. An orphan from a previous process.
- **Occupied channel** → adopt with `ever_occupied=True`,
  `last_occupied_at=now`, `rom_id=None`, `watcher=None`. The ordinary
  empty-timer then governs it, so a conversation that outlived a restart gets
  its full timeout after the last person leaves instead of being cut off.

### 5.1 When the sweep runs

**Not an `on_ready` listener.** `bot.py:795` calls `load_all_cogs()` from inside
`RommBot.on_ready`, and py-cord's `Client.dispatch` (client.py:436) schedules
the main `on_ready` handler as a task and then iterates `_event_handlers` — so a
cog listener registered *during* that task was never in the list for that
dispatch and does not see the event. `check_server` works around this with
`bot.loop.create_task(...)` + `await self.bot.wait_until_ready()`, and the
comment at `cogs/netplay/cog.py:145` records exactly this reasoning. It is
precedent *against* a listener, not for one.

So: `bot.loop.create_task(self.voice.sweep_all())`, guarded by
`await self.bot.wait_until_ready()`, alongside the existing `check_server` task.
If a reconnect listener is also kept, it must be idempotent — the registry
already holds the adopted rooms, so a second sweep must not re-adopt or
re-delete them.

Note `cogs/info.py:135`'s `on_ready` listener has the same latent defect. It is
not a pattern to copy.

### 5.2 Occupancy is read from `voice_states`, never `members`

**Use `bool(channel.voice_states)`.** Everywhere — the sweep and the reaper
both.

`VocalGuildChannel.members` (channel.py:1423) builds its list from
`guild._voice_states` but drops any entry whose `guild.get_member(user_id)`
returns `None`. The member cache is not the voice-state cache: on a chunk
timeout, in a large guild, or generally during the startup window the sweep runs
in, `members` can return `[]` for a channel that has people in it. The sweep
would then classify it "empty" and **disconnect a live conversation** — the one
outcome the adoption branch exists to prevent.

`voice_states` (channel.py:1434) reads `guild._voice_states` directly with no
member-cache dependency, and its docstring says it is "intentionally low level
to replace `members` when the member cache is unavailable." That is this case
precisely.

### 5.3 The exclusivity requirement

**The bot treats the netplay category as exclusively its own. Anything in it
carrying the marker is a candidate for deletion.**

Pointing `NETPLAY_VOICE_CATEGORY` at an existing "Voice Chat" category would
delete matching channels in it. Three mitigations:

1. Deletion requires the reserved marker prefix **`🕹️`** as well as category
   membership.
2. The sweep refuses to run when the configured category is `Rom Server Stats`,
   logging an error. See below.
3. The README and the config comment state the requirement and the reserved
   marker explicitly.

The marker is `🕹️`, **not `🎮`**. `cogs/info.py:11` maps `"Platforms": "🎮"`
and creates a voice channel named `🎮 Platforms: 1,234`. A stat channel is empty
by definition, so with `NETPLAY_VOICE_CATEGORY="Rom Server Stats"` a `🎮` marker
would have this bot deleting its own stat channel on every start while `Info`
recreated it — a delete/create loop between two features of the same bot. `🎮`
is also the likeliest prefix on a hand-made gaming voice channel, i.e. the exact
population mitigation 1 is supposed to protect.

This is the sharpest edge in the design and is documented rather than hidden.

### 5.4 Category lookup is exact and first-match

`discord.utils.get(guild.categories, name=…)` matches case-sensitively and
returns the first hit. Discord does not normalise category names the way it does
text-channel names, so a guild may legitimately hold two categories named
`Netplay`, and `netplay` is a different category from `Netplay`. Consequences to
accept and document: a duplicate-named guild gets an arbitrary pick that may
differ across restarts, stranding one category's orphans; a case-mismatched
guild gets a second category created. Not worth solving; worth stating.

---

## 6. Scheduling

The reaper runs at the end of the existing `poll_sessions` tick, **after** the
watcher GC, so a watcher that went terminal this tick already reads as gone
rather than surviving one tick too long.

Occupancy comes from `channel.voice_states` (§5.2), which `bot.py:403`'s
`intents.voice_states = True` keeps gateway-maintained — no HTTP, so a
20-second cadence costs nothing.

Level-triggered on purpose. Re-deriving occupancy every tick cannot be stranded
by a dropped gateway event, the way an `on_voice_state_update` listener can be
across a restart or a resume. This is the same argument `refresh_message`
already makes for comparing against the last render Discord accepted rather than
trusting a change flag.

---

## 7. Naming and channel properties

Name: `🕹️ {rom_name}`, run through the existing `sanitize_name`, which already
strips markdown-dangerous characters and caps at 64 — comfortably inside
Discord's 100-character channel-name limit, so no further truncation is needed.

**The name is set once and never changed.** Discord throttles channel *name* and
*topic* edits to roughly two per ten minutes per channel, and a temporary
channel showing live seat counts would exhaust that bucket immediately and then
silently stop updating. No seat counts, no state, no renames. `cogs/info.py`
renames safely only because it is a handful of channels on a slow cadence.

`user_limit=0`, set at creation and never edited. **Not** because of the rename
bucket — that bucket is name/topic-specific and `user_limit` goes through the
ordinary per-route limit — but because at creation the watcher is PENDING and
the room's `max_players` is unknown, and once it is known, capping the channel
would lock out spectators, who are welcome. A cap would also have to track a
`max` that can change between rooms.

Permissions: none set. The channel inherits the category, which is open to the
guild. This matches the announcement being public and the Join button being open
to everyone.

**`bot.rate_limiter` is deliberately not used here**, though `cogs/info.py` —
the cited pattern — calls it before every channel operation. That limiter is
`romm.rate_limiter` (30/min, sized for RomM's API), py-cord handles Discord's
own 429s internally, and routing Discord channel operations through the RomM
budget would couple two unrelated rate limits.

---

## 8. Surfacing

1. **Embed field** — `Voice`, containing the channel mention `<#id>`, which
   Discord renders as a one-click channel link. Added **non-inline, after** the
   existing fields: `build_netplay_embed` deliberately pairs Platform with the
   roster as two inline fields, and a third inline field would reflow that row.
   Omitted entirely in `ENDED` and `EXPIRED`, where the channel is very likely
   already reaped.
2. **Join reply** — a second link button,
   `https://discord.com/channels/{guild_id}/{channel_id}`, beside the existing
   "Open the player". That ephemeral reply is already the single step between
   pressing Join and being in the game; voice belongs in the same breath. Shown
   only when `watcher.voice_channel_id` is still set (§2.4).

---

## 9. Failure handling

Every voice failure degrades; none blocks.

The create path catches **`discord.HTTPException`** as a whole, not just
`Forbidden`. `Forbidden` covers a missing `Manage Channels`, but the reachable
failures are wider: **a Discord category holds at most 50 channels** and a guild
at most 500, and with `NETPLAY_MAX_WATCHERS=25` plus adopted orphans plus
whatever else is in the category, `create_voice_channel` starts returning 400s,
which are `HTTPException` and not `Forbidden`. `announce()`'s existing message
failure path already uses exactly that breadth.

Any such failure logs once at WARNING and returns `None`. The announcement posts
exactly as it does today, without the voice field. An admin misconfiguring voice
must not be able to break `/netplay` — the fail-open posture `check_server` and
`check_scope` already take, where only a positive, confirmed answer disables
anything.

The voice registry is capped at `NETPLAY_MAX_WATCHERS` for the same reason the
watcher registry is: it bounds what one guild can accumulate before the category
limit is hit.

Guild-only by construction: `ctx.guild` is `None` in a DM, so voice is skipped
there with no special case.

---

## 10. Testing

`tests/test_netplay_voice.py`, stdlib `unittest` with `SimpleNamespace` fakes
and `object.__new__(Cog)`, matching the existing netplay tests. No pytest
fixtures; the suite has no conftest.

**`reap()` as a policy table** — pure, no fakes:
- occupied → kept, stamps updated
- empty past `empty_timeout` → deleted
- empty within `empty_timeout` → kept
- never occupied, watcher alive → kept
- never occupied, watcher gone → deleted
- never occupied, adopted (`rom_id is None`) → deleted
- past `max_lifetime` **while occupied** → deleted (row 1 outranks row 2)

**`watcher_alive` identity:**
- a new watcher registered under the same `rom_id` does **not** keep the old
  room alive

**Occupancy:**
- derived from `voice_states`, and a channel whose `members` is empty while
  `voice_states` is populated reads as occupied — the §5.2 regression, pinned

**Sweep:**
- empty orphan deleted
- occupied orphan adopted with `ever_occupied=True` and `last_occupied_at` set
- a channel in the category without the marker left alone
- a channel outside the category never examined
- refuses to run against `Rom Server Stats`
- running twice does not double-adopt

**Degradation:**
- `HTTPException` on create leaves the announcement intact and
  `voice_channel_id` as `None`
- the `announce()` message-failure path deletes the channel it just created
- `ctx.guild is None` skips voice entirely

**Reap clears the post:**
- deleting a channel whose watcher is still live clears `voice_channel_id`

**In `tests/test_netplay_embeds.py`:**
- `render_key` changes when `voice_channel_id` is set and again when it is
  cleared — the invariant that keeps the voice field from going stale

---

## 11. Deliberately excluded

- **Roster-gated channels.** Per-member permission overwrites on every Join
  press, and late arrivals blocked until the overwrite lands. The announcement
  is public; the voice channel matches it.
- **Seat counts in the channel name.** Rate limit, §7.
- **`user_limit` mirroring `max_players`.** Unknown at creation, and capping it
  later locks out spectators (§7).
- **Persisting rooms to SQLite.** A schema and a migration for state that is
  disposable by definition. §5 recovers without one.
- **Text channels.** The announcement already lives in a text channel.
- **The bot joining voice.** No audio is being moved.
