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

| Situation | Outcome |
|---|---|
| Someone is in it | Keep. Stamp `last_occupied_at = now`, `ever_occupied = True` |
| Was occupied, now empty | Delete once `now - last_occupied_at >= empty_timeout` |
| Never occupied, watcher alive | Keep |
| Never occupied, watcher gone | Delete |
| Age exceeds `max_lifetime` | Delete regardless |

**Why the never-occupied rows exist.** Creating eagerly means most channels are
never entered — the announcement expires, nobody wanted voice. Governed by a
flat empty-timer measured from creation, those channels would be deleted five
minutes after the post goes up, before the host had opened the room. So a
channel nobody has ever entered lives exactly as long as its announcement. The
moment anyone joins, it detaches permanently and is governed only by the
empty-timer. Independence begins when the channel is actually used.

`watcher_alive` is `room.rom_id in cog.watchers`. The existing GC drops a
watcher the tick it goes terminal, so that lookup *is* the "announcement is
finished" signal; no new bookkeeping carries it.

The `max_lifetime` row is a leak guard, not a policy. Discord does not
guarantee an AFK disconnect, and a client wedged in a voice channel would
otherwise hold it forever. It deletes even an occupied channel, which is why the
default is generous.

### 2.3 Deletion

`channel.delete()`. A `NotFound` means someone deleted it by hand and is treated
as success — drop the registry entry. A `Forbidden` means permissions changed
under us: log once and drop the entry rather than retrying every tick forever,
matching how `refresh_message` treats the same two errors.

---

## 3. Components

### 3.1 `cogs/netplay/voice.py`

```python
@dataclass
class VoiceRoom:
    channel_id: int
    guild_id: int
    rom_id: Optional[int]        # None for a room adopted by the sweep
    created_at: float
    ever_occupied: bool = False
    last_occupied_at: Optional[float] = None
```

`reap()` as above.

`NetplayVoice` holds the bot, the registry, and every Discord call:

- `ensure_category(guild)` — find or create, the way `cogs/info.py` does.
- `create_for(watcher, guild)` — returns the channel or `None`.
- `sweep()` — startup adoption, §5.
- `tick(now, watchers)` — one reaper pass over the registry.

### 3.2 Changes to existing files

| File | Change |
|---|---|
| `bot.py` | Four config lines beside the existing `NETPLAY_*` block |
| `cogs/netplay/watcher.py` | `voice_channel_id: Optional[int] = None` |
| `cogs/netplay/embeds.py` | Voice field, and the same id in `render_key` |
| `cogs/netplay/cog.py` | Instantiate; create in `announce`; reap in `tick`; sweep on ready; voice button on the Join reply |
| `README.md` | Netplay section: the four settings and the category warning |

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

---

## 5. Startup sweep

Watchers are in-memory; channels are not. A restart forgets every channel the
bot made, and without recovery a nightly-restarting bot fills the category with
dead channels.

Rather than persisting disposable state to SQLite, **the category is the
state.** On `on_ready`, `sweep()` walks it in each guild, considering only voice
channels whose names carry the `🎮` prefix (§5.1) and ignoring everything else
entirely:

- **Empty channel** → delete. An orphan from a previous process.
- **Occupied channel** → adopt with `ever_occupied=True`,
  `last_occupied_at=now`, `rom_id=None`. The ordinary empty-timer then governs
  it, so a conversation that outlived a restart gets its full timeout after the
  last person leaves instead of being cut off.

An adopted room has no watcher, which is why `rom_id` is optional — and harmless,
because `ever_occupied` short-circuits the `watcher_alive` rows entirely. Those
rows are unreachable for adopted rooms by construction.

`on_ready`, not `cog_load`: `bot.py` calls `load_all_cogs()` from inside
`on_ready`, and py-cord has no `cog_load` hook. This follows the reasoning
already recorded on `check_server`.

### 5.1 The exclusivity requirement

**The bot treats the netplay category as exclusively its own. Anything in it is
a candidate for deletion.**

Pointing `NETPLAY_VOICE_CATEGORY` at an existing "Voice Chat" category would
delete that category's channels. Two mitigations:

1. Deletion requires the `🎮` name prefix as well as category membership.
2. The README and the config comment state the requirement explicitly.

This is the sharpest edge in the design and is documented rather than hidden.

---

## 6. Scheduling and occupancy

The reaper runs at the end of the existing `poll_sessions` tick, **after** the
watcher GC, so a watcher that went terminal this tick already reads as gone
rather than surviving one tick too long.

Occupancy is `bool(channel.members)`. `bot.py:403` sets
`intents.voice_states = True`, so that list is gateway-maintained and costs no
HTTP — a 20-second cadence is free.

Level-triggered on purpose. Re-deriving occupancy every tick cannot be stranded
by a dropped gateway event, the way an `on_voice_state_update` listener can be
across a restart or a resume. This is the same argument `refresh_message`
already makes for comparing against the last render Discord accepted rather than
trusting a change flag.

---

## 7. Naming and channel properties

Name: `🎮 {rom_name}`, run through the existing `sanitize_name`, truncated to
Discord's 100 characters.

**The name is set once and never changed.** Discord throttles channel renames to
roughly two per ten minutes per channel; a temporary channel showing live seat
counts would exhaust that bucket immediately and then silently stop updating. No
seat counts, no state, no renames. `cogs/info.py` renames safely only because it
is a handful of channels on a slow cadence.

`user_limit=0`, set at creation and never edited — `channel.edit(user_limit=…)`
shares the rename bucket. At creation the watcher is PENDING and the room's
`max_players` is unknown anyway, and spectators are welcome regardless.

Permissions: none set. The channel inherits the category, which is open to the
guild. This matches the announcement being public and the Join button being open
to everyone.

---

## 8. Surfacing

1. **Embed field** — `Voice`, containing the channel mention `<#id>`, which
   Discord renders as a one-click channel link.
2. **Join reply** — a second link button,
   `https://discord.com/channels/{guild_id}/{channel_id}`, beside the existing
   "Open the player". That ephemeral reply is already the single step between
   pressing Join and being in the game; voice belongs in the same breath.

---

## 9. Failure handling

Every voice failure degrades; none blocks.

A missing `Manage Channels` permission, a category that cannot be created, a
`Forbidden` on creation — each logs once at WARNING and returns `None`. The
announcement posts exactly as it does today, without the voice field.

An admin misconfiguring voice must not be able to break `/netplay`. This is the
fail-open posture `check_server` and `check_scope` already take: only a positive,
confirmed answer disables anything.

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
- past `max_lifetime` while occupied → deleted

**Sweep:**
- empty orphan deleted
- occupied orphan adopted with `ever_occupied=True` and `last_occupied_at` set
- a channel in the category without the `🎮` prefix left alone
- a channel outside the category never examined

**Degradation:**
- `Forbidden` on create leaves the announcement intact and `voice_channel_id`
  as `None`

**In `tests/test_netplay_embeds.py`:**
- `render_key` changes when `voice_channel_id` is set — the invariant that keeps
  the voice field from going stale

---

## 11. Deliberately excluded

- **Roster-gated channels.** Per-member permission overwrites on every Join
  press, and late arrivals blocked until the overwrite lands. The announcement
  is public; the voice channel matches it.
- **Seat counts in the channel name.** Rate limit, §7.
- **`user_limit` mirroring `max_players`.** Unknown at creation, and editing it
  later shares the rename bucket.
- **Persisting rooms to SQLite.** A schema and a migration for state that is
  disposable by definition. §5 recovers without one.
- **Text channels.** The announcement already lives in a text channel.
- **The bot joining voice.** No audio is being moved.
