# RomM Netplay Coordination — Design

**Date:** 2026-09-13
**Status:** Approved for implementation planning
**Scope:** A `/netplay` command that announces a netplay session in Discord and
keeps the announcement current while the session lives.

---

## 1. Summary

RomM stores and serves ROMs. Since 5.x it also runs netplay: EmulatorJS in the
browser, rooms coordinated over socket.io on the RomM server, players connected
peer-to-peer over WebRTC. What RomM does not do is tell anyone a session is
happening. Someone opens a room and it exists, unlisted, until another person
happens to open the same game's player and look.

That is the gap this cog fills. `/netplay <platform> <game>` posts a Discord
embed for a game in the library, then keeps it current — no room yet, room open
with two of four seats filled, session over — by polling RomM.

The bot never hosts, joins, or relays anything. It reads one endpoint and edits
one message.

---

## 2. What was verified

Everything below was checked against the live server (`roms.idiosynchronic.com`,
RomM 5.2.0) on 2026-09-13, not inferred from documentation.

### 2.1 The endpoint

`GET /api/netplay/list?game_id=<rom_id>`, scope `assets.read`, returns a JSON
object keyed by room session id:

```json
{
  "84c4be1e-f024-7f3d-ee1a-97322c07697c": {
    "room_name": "Claude netplay test",
    "current": 1,
    "max": 4,
    "player_name": "idiosync",
    "hasPassword": false
  }
}
```

- `{}` when no room is open for that game.
- **HTTP 422 when `game_id` is omitted.** There is no enumerate-all form.
- `player_name` is the *owner* only. The full player list exists in Redis but
  is not exposed over REST.

### 2.2 End-to-end behaviour

Driving a real browser against the live server:

| Check | Result |
|---|---|
| Room opened from the EmulatorJS player | appears in `/api/netplay/list` immediately |
| Second player joined | `current` went 1 → 2, reflected in the API |
| WebRTC | `connectionState: connected`, ICE connected, signaling stable |
| Both players left | room disappears, endpoint returns `{}` |

The room lifecycle the embed depends on is real and observable.

### 2.3 `game_id` is the RomM rom id

In the player, `window.EJS_gameID === 50265`, the same id as
`/api/roms/50265`. The browser's own room lookup is literally:

```js
fetch(`/api/netplay/list?game_id=${window.EJS_gameID}`)
```

The cog and the RomM frontend read the same endpoint with the same key. No hash
matching, no fuzzy title matching, no CRC index.

### 2.4 Server configuration

`GET /api/config` exposes `EJS_NETPLAY_ENABLED` and `EJS_NETPLAY_ICE_SERVERS`.

**`EJS_NETPLAY_ICE_SERVERS` is redacted to `[]` for unauthenticated callers**
(the entries can carry TURN credentials), as is `CONFIG_FILE_PARSE_ERROR`. A
health check that forgets to authenticate reports a correctly-configured server
as unconfigured. The cog authenticates, so it sees the real values — but the
startup check must not be "downgraded" to an anonymous request later.

Netplay is configured only in `config.yml` (`emulatorjs.netplay.enabled`,
`emulatorjs.netplay.ice_servers`). There is no settings UI and no write
endpoint for it.

### 2.5 Rejected: the RetroArch public lobby

The original idea was built on `lobby.libretro.com`. Investigated and rejected:

- **Read-only in practice.** `POST /add` exists, but the lobby takes the host IP
  from the request's source address and expires sessions after 60s without a
  keepalive. A bot announcing would register itself as the host.
- **Unmatchable content.** A live sample of 32 worldwide rooms was dominated by
  ROM hacks, translations, arcade romset shortnames and renamed files
  (`Kirby Super Star (USA) (patched)`, `PokemonQuetzalEnglishAlpha8v4`,
  `mslugx`). Those CRCs will not match a No-Intro-organised library.
- **Wrong population.** 32 rooms globally, none of them people in this Discord.
- HTTP only; the lobby refuses TLS on 443.

NAT was *not* the reason to reject it — RetroArch's MITM relays were carrying
17 of those 32 rooms. The reason is that the bot cannot announce, and the
handful of rooms it could read are strangers playing unmatched ROM hacks.

---

## 3. Scope

### v1

- `/netplay <platform> <game>` — resolve a library ROM, post an announcement
  embed.
- The embed tracks the session: waiting → open → ended.

The host is rendered as the **raw `player_name` string RomM reports**, with no
Discord identity attached. See §6.3 for why.

### v2 (designed for, not built)

- **Discord identity enrichment.** Mapping `player_name` to a Discord user is
  deferred, not merely unbuilt — it is unsafe as stated. §6.3 records the
  constraint any future implementation must satisfy, and §7.1 the database
  helper it needs.
- **Interest registry.** "Ping me when someone hosts Bomberman." Needs a table
  via `database_manager.py` and a subscribe/unsubscribe UI. Deferred because it
  is a larger surface than the announce loop, and pointless until the announce
  loop is proven to get used.
- **Role or channel ping on announce.** Config-driven, cheap, but blunt — the
  same ping regardless of game.

### Non-goals

- The bot never opens, joins, or proxies a room. It cannot: the socket.io
  handshake requires a `sessionId`/`playerId` and would make the bot a
  participant.
- No enumeration of all live rooms. The API cannot do it (§2.1).
- No RetroArch lobby integration (§2.5).
- No persistence of watchers across restarts (§5.5).

---

## 4. Architecture

Netplay is RomM's own API, not a third-party service, so nothing goes in
`integrations/` — that folder is for external services with their own
credentials, like ggrequestz. The API calls belong on the existing RomM client
and the user-facing surface is an ordinary cog.

| File | Responsibility |
|---|---|
| `romm_client.py` | Two new methods (§7). Existing session, token, retry. |
| `cogs/netplay/__init__.py` | Re-exports + `setup()`, mirroring [cogs/requests/__init__.py](../../../cogs/requests/__init__.py). |
| `cogs/netplay/cog.py` | The `/netplay` command, ROM resolution, watcher registry. |
| `cogs/netplay/watcher.py` | The poll loop and the state machine (§5). |
| `cogs/netplay/embeds.py` | One formatter per state. |
| `cogs/netplay/views.py` | The ROM select (§6.1) — new, not reused. |

Registered in `bot.py`'s `core_cogs` list ([bot.py:677-687](../../../bot.py#L677-L687)),
with a matching entry in the parallel `cog_dependencies` dict
([bot.py:689-699](../../../bot.py#L689-L699)) — `['aiohttp']`. Every core cog
has one; omitting it is easy to miss.

### 4.1 What is genuinely reusable, and what is not

An earlier draft of this spec claimed the request flow's game picker could be
reused unchanged. **That is false, and the correction is load-bearing** — it
concealed the command's entire input surface.

| Component | Reusable? |
|---|---|
| [cogs/platform_emoji.py](../../../cogs/platform_emoji.py) — slug normalisation, emoji | Yes |
| `platforms_repo.search_for_autocomplete` ([cogs/requests/repo.py:449](../../../cogs/requests/repo.py#L449)) | **No** — returns three-column `aiosqlite.Row`s, offers platforms RomM does not have, and would couple this cog to `cogs.requests`. Use `bot.fetch_api_endpoint('platforms')` as [cogs/search.py:1341](../../../cogs/search.py#L1341) does. |
| [cogs/requests/views_game.py](../../../cogs/requests/views_game.py) `GameSelect` / `GameSelectView` | **No** |

`GameSelect` is not reusable and not paginated:

- **Not paginated.** It truncates with `matches[:25]`
  ([views_game.py:312](../../../cogs/requests/views_game.py#L312),
  [:334](../../../cogs/requests/views_game.py#L334),
  [:559](../../../cogs/requests/views_game.py#L559)). There is no page state
  anywhere in the file.
- **Not generic.** `GameSelectView` hard-wires a "Submit Request" button and a
  "Not Listed" button that sets `selected_game = "manual"`, and
  `GameSelect.callback` calls `self.view.update_view_for_selection`
  ([views_game.py:83-150](../../../cogs/requests/views_game.py#L83-L150)).
- **Wrong data shape.** Its options are built from `match['release_date']` and
  `match['platforms']` — IGDB matches. Netplay needs a **RomM rom id** (§2.3).

There is also **no ROM-name autocomplete anywhere in the repo**; every
`autocomplete=` in the codebase is platform-only.

`cogs/netplay/views.py` therefore contains a new, small select built from RomM
ROM rows. It is deliberately *not* a generalisation of `GameSelect`; extracting
a shared abstraction from two dissimilar callers is a change to the request
flow and out of scope here.

### 4.2 Why not a socket.io client

RomM emits real-time room events, and subscribing would give live updates and
full enumeration. Rejected: `open-room`/`join-room` require a `sessionId` and
`playerId`, so the bot must masquerade as a participant and would appear in the
room; it adds a `python-socketio` dependency; and it couples the bot to
undocumented internal events. Too much fragility for a player counter.

---

## 5. The watcher

### 5.1 States

The important case is that **a room usually does not exist when the command is
run** — the user is announcing that they are *about to* host. The endpoint
returns `{}` until they actually open the room in their browser.

| State | Entered when | Embed shows |
|---|---|---|
| `PENDING` | command runs | "@user wants to play **X**" + Play link |
| `LIVE` | poll finds ≥1 room | room name, host, `2/4`, 🔒 if password, Join link |
| `ENDED` | rooms vanish after `LIVE` | "Session ended", dimmed |
| `EXPIRED` | still `PENDING` after the grace period | "No session started" |

`ENDED` and `EXPIRED` are terminal: the watcher unregisters and polling stops.
Every watcher therefore self-limits, which is what keeps the poll set bounded.

`PENDING → LIVE` is the whole point. The announcement is useful *before* the
room exists, which is exactly the coordination gap.

### 5.2 Accepted gaps in the state machine

Both of these are known and accepted for v1. They are recorded so an
implementer can tell they were considered rather than missed.

- **A session shorter than one poll interval is invisible.** Host opens a room,
  nobody joins, they close it within `NETPLAY_POLL_INTERVAL`. The watcher never
  leaves `PENDING` and eventually says "No session started" — a false statement
  about something that did happen. Accepted: the alternative is a much shorter
  interval, which costs far more than the case is worth.
- **`ENDED` does not re-arm.** A host who drops and reopens 40 seconds later
  has a dead embed. Given §11's point that host upload bandwidth makes rooms
  fragile, this is expected rather than exotic. **Mitigation:** the `ENDED`
  embed must carry an explicit "session over — run `/netplay` to announce a new
  one" line, so the dead state is self-explaining.

### 5.3 The poll set, and the tick

`/api/netplay/list` requires a `game_id`, so there is no cheap "what's live
right now" query. The poll set is therefore **only the rom ids with a live
Discord post**, which means every request maps to a message someone is
watching, and the set drains on its own.

A single `discord.ext.tasks` loop iterates all active watchers per tick, not
one task per post. Three mechanics are mandatory, not optional:

1. **Iterate a snapshot.** The loop awaits an HTTP call per watcher, and a
   `/netplay` invocation during any of those awaits mutates the registry.
   Iterate `list(watchers.items())` and remove terminal watchers *after* the
   pass. Iterating the live dict raises `RuntimeError: dictionary changed size
   during iteration`, intermittently and under load.
2. **Edit only when the rendered payload changed.** At a 20s interval and 25
   watchers, editing every tick is 25 edits per 20s, mostly into one or two
   channels — straight into Discord's per-channel edit throttling. Render the
   embed, compare to the last rendered value, and skip the API call when equal.
   This is the single most likely production failure of the design.
3. **Set the interval at runtime.** `tasks.loop` fixes its interval at
   decoration time, so `NETPLAY_POLL_INTERVAL` must be applied via
   `change_interval` in `before_loop`. Precedent: [bot.py:817](../../../bot.py#L817).

### 5.4 Multiple rooms

The endpoint returns a dict, so a game can have several concurrent rooms. The
embed renders 0, 1, or N. The `LIVE → ENDED` transition is "the dict became
empty", not "a particular room vanished".

### 5.5 Restart

Watchers live in memory and die with the cog. A netplay room rarely outlives a
bot restart, and reconciling orphaned embeds would cost a table and a boot-time
sweep to solve a problem that mostly resolves itself. On restart, existing posts
simply stop updating; because every non-terminal embed states its own staleness
rule (§5.2), a stale one reads as stale rather than as a live session.

---

## 6. Commands and flow

### 6.1 Resolving a ROM

The command mirrors `/search`'s proven shape rather than inventing one:

```
/netplay platform:<autocomplete> game:<text>
```

`platform` uses the existing `search_for_autocomplete`
([cogs/requests/repo.py:449](../../../cogs/requests/repo.py#L449)); `game` is
free text, resolved against RomM's ROM search.

- 0 matches → refuse, with the search term echoed.
- 1 match → proceed directly, no select.
- 2-25 matches → a new select (§4.1) of ROM rows; the value is the rom id.
- \>25 matches → show the first 25 and say so explicitly, asking the user to
  narrow the term. **This is truncation, not pagination**, and the embed must
  admit it rather than silently dropping results.

### 6.2 Lifecycle

```
/netplay platform game
  └─ resolve to one rom_id
       └─ bot posts PENDING embed, registers a watcher
            └─ user clicks Play, opens a room in their browser
                 └─ next poll flips the embed to LIVE
                      └─ others click Join, current climbs
                           └─ everyone leaves → ENDED
```

**Embed contents.** Game name, cover art, platform emoji, and the core RomM
will use (`EJS_DEFAULT_CORES` for the platform, when set — this is the "here's
the exact core you need" line from the original idea, now free). Plus, per
state, the host and seat count.

**The Join link** is `{DOMAIN}/rom/{rom_id}/ejs`. There is no room-id query
parameter, so the link lands the user in the right game's player and they pick
the room from the netplay menu there. One click, not zero.

`DOMAIN` defaults to the literal string `No website configured`
([bot.py:309](../../../bot.py#L309)). Unset, the embed's primary
call-to-action would render as `No website configured/rom/50265/ejs`. See §9.

### 6.3 Identity: why the host is a raw string

`player_name` defaults to the RomM username, so it is tempting to map it to a
Discord user via the `user_links` table and render "hosted by Alice".

**Do not, in v1.** The value is client-supplied and was trivially overridden to
an arbitrary string during testing. Rendering a Discord identity from it means
a host can set `player_name` to another member's RomM username and have the bot
vouch for them in an embed — and if the enrichment renders a mention, it pings
the impersonated user too.

v1 therefore prints the raw string RomM reports and attributes nothing.

Any future enrichment (§3, v2) must satisfy all three:

1. Display name only — never a `<@id>` mention.
2. An explicit unverified marker, so the embed never asserts identity.
3. Treated as a rendering convenience for a self-reported string, never as
   authentication or authorization.

---

## 7. RommClient additions

Two methods, both using the existing `make_authenticated_request`:

```python
async def get_server_config(self) -> Optional[Dict]:
    """GET /api/config - includes EJS_NETPLAY_ENABLED and ICE servers.

    Must be authenticated: ICE servers and the config parse error are
    redacted to [] / None for anonymous callers.
    """

async def list_netplay_rooms(self, rom_id: int) -> Optional[Dict]:
    """GET /api/netplay/list?game_id=<rom_id> - open rooms for one ROM.

    Returns {} when none are open. game_id is mandatory; omitting it is a
    422, so there is no way to list every room.
    """
```

**Why not `fetch_api_endpoint`.** It takes `bypass_cache`
([romm_client.py:398](../../../romm_client.py#L398)), so "it caches" alone does
not settle it. The decisive reason is that `_get_json` calls
`self.cache.set(endpoint, data)` at
[romm_client.py:468](../../../romm_client.py#L468) **even on the bypass path**.
Polling through it would write a fresh room-list entry into the shared
`APICache` every interval per watched game, for a value that is stale within
seconds. (Secondary: `APICache.get`'s result is consumed via `if cached_data:`,
so a cached `{}` is falsy and re-fetches anyway — the cache cannot help this
endpoint's most common response.)

**The cost of that choice:** `make_authenticated_request` has **no retry loop**;
the exponential backoff lives only in `fetch_api_endpoint`
([romm_client.py:406-436](../../../romm_client.py#L406-L436)). So the
"mark it stale after several consecutive failures" rule in §9 is the *only*
resilience in this design. That is acceptable for a poller that runs again in
20 seconds, but it must be a deliberate choice rather than an assumption that a
retry exists.

**The `None` / `{}` contract.** `None` means the call failed; `{}` means no
rooms. The watcher must distinguish them: a failed poll is not an ended session
and must not flip an embed to `ENDED`. One nuance to be aware of —
`_read_response` ([romm_client.py:371-395](../../../romm_client.py#L371-L395))
also returns `{}` for HTTP 204 and for any 2xx whose body will not parse, so
`{}` strictly means "succeeded, no rooms readable". Benign here, but the
contract is not quite two-valued.

### 7.1 Database helper (v2 only)

Should identity enrichment ever be built, note that **no reverse lookup
exists**. `database_manager.py` has `get_user_link(discord_id)`
([:659](../../../database_manager.py#L659), forward only) and
`get_all_user_links()` ([:728](../../../database_manager.py#L728)).
`user_manager.py`'s `discord_user_links` dict is built inside a View, not a
reusable API. A naive implementation becomes an O(all links) scan per room per
tick, or a cross-cog import of the kind `tests/test_import_boundaries.py`
discourages.

The fix is a new `database_manager.get_user_link_by_romm_username(name)`, which
is cheap — `idx_romm_username ... COLLATE NOCASE` already exists at
[database_manager.py:583](../../../database_manager.py#L583).

---

## 8. Configuration

Per the project rule that `Config` in `bot.py` owns all environment reading:

| Variable | Default | Purpose |
|---|---|---|
| `NETPLAY_ENABLED` | `true` | Enable the command. |
| `NETPLAY_POLL_INTERVAL` | `20` | Seconds between ticks. |
| `NETPLAY_PENDING_TIMEOUT` | `900` | Seconds before `PENDING` → `EXPIRED`. |
| `NETPLAY_MAX_WATCHERS` | `25` | Cap on concurrent live posts. |

**Where `NETPLAY_ENABLED` is enforced.** Not at load time. `core_cogs`
([bot.py:677-687](../../../bot.py#L677-L687)) is an unconditional list — no core
cog is env-gated, and the `{STEM}_ENABLED` convention belongs to
`load_integration_cogs` ([bot.py:743-746](../../../bot.py#L743-L746)), which
this cog deliberately does not use (§4). The cog therefore always loads and
self-disables from a task started in `__init__`, guarding commands on
`self.enabled and self.server_enabled` the way `romm_streaming.py` does. No
change to `load_all_cogs`.

Two py-cord constraints force that placement, both verified against 2.6.1:
`cog_load` **is not a py-cord hook** (only `cog_unload` exists — `cog_load` is
discord.py, and `cogs/user_manager.py:1199` has a dead one today, which is why
its `invite_reconcile_loop` never starts); and an `on_ready` listener on this
cog would never fire, because `bot.py:779` calls `load_all_cogs()` from inside
`on_ready` and `Client.dispatch` snapshots its listener list before running
it.

**Worst-case request rate.** At the defaults, 25 watchers × 3 polls/min = **75
requests/min** against RomM. That is a new load profile for this bot — by
comparison `recent_roms` runs `@tasks.loop(hours=1)`. Note also that
`RommClient` has **no working client-side throttle**: `RateLimit` is defined at
[romm_client.py:70](../../../romm_client.py#L70) and instantiated at
[:90](../../../romm_client.py#L90), but `acquire()` is never called anywhere in
the module. Operators lowering `NETPLAY_POLL_INTERVAL` or raising
`NETPLAY_MAX_WATCHERS` have nothing catching them.

**Token scope.** `/api/netplay/list` requires `assets.read`, which is **not** in
the scope list the README documents for `ROMM_CLIENT_TOKEN`
([README.md:152](../../../README.md#L152)) nor in the OAuth grant
([romm_client.py:158](../../../romm_client.py#L158)) — both currently read
`roms.read platforms.read firmware.read users.read users.write me.write`.
Existing tokens will 403. See §13 before editing that string.

**Server prerequisites**, checked once at startup and reported clearly:

- `EJS_NETPLAY_ENABLED` must be true, or every room list is permanently empty.
- `EJS_NETPLAY_ICE_SERVERS` empty means host-candidate-only WebRTC — LAN works,
  remote players generally do not. Warn, don't disable.

---

## 9. Failure modes

| Condition | Behaviour |
|---|---|
| Server has netplay disabled | Cog loads, `/netplay` refuses with an explanation. |
| ICE servers empty | Warn at startup; commands still work (LAN play is legitimate). |
| Token lacks `assets.read` | Detected at startup by a **status-aware** probe (`RommClient.netplay_scope_ok`), because `_read_response` returns `None` for every non-2xx alike and cannot tell a 403 from a timeout. A confirmed 401/403 makes `/netplay` refuse, naming the scope: posting an announcement that can never update is worse than not offering the command. An undetermined probe leaves the feature enabled. |
| Startup probe itself fails (server restarting) | **Fail open**: log a warning, leave the command enabled. A transient boot-order failure must not silently disable the feature until the next restart. |
| `DOMAIN` unset (`No website configured`) | `/netplay` refuses at command time with a config error. Never post an embed whose primary link is broken. |
| Poll returns `None` | Leave the state alone — **never** flip to `ENDED`, however long it persists. After several consecutive failures mark the watcher **stale**, say so in the embed, and keep polling; a successful poll clears it. RomM being unreachable is not evidence a session ended, and there is no retry beneath this (§7). |
| **Room fills up** | `_is_room_open` in RomM's `backend/endpoints/netplay.py` returns `False` once `len(players) >= max_players`, so **a full room is omitted from `/netplay/list` entirely**. `current == max` can therefore never appear in a response, and a filling room looks identical to a closing one: both simply vanish. A grace period cannot separate them — for a 2-player game *full is the steady state for the whole session*, so the room stays absent for its entire duration. Behaviour: a LIVE session whose rooms disappear **while one seat was free** stays LIVE, records `unlisted_since`, keeps its last-known rooms, and keeps polling; it returns to a listed LIVE the moment a room reappears, which is how a freed seat gets re-advertised. Rooms that disappear with **more than one seat free** did not fill inside a 20s poll — they closed, and end immediately as before. |
| Session never reappears | An unlisted LIVE session cannot be distinguished from a finished one, so it is ended by a cap (`NETPLAY_SESSION_TIMEOUT`, default 3600s) rather than by observation. `ended_at` is set to `unlisted_since`, not to the moment the cap fired. The embed says "last seen", never "ran for": duration is unknowable once a session spends most of its life invisible. |
| Watcher cap reached | Refuse politely; suggest waiting for a session to end. |
| Message deleted | Drop the watcher on the resulting 404 rather than retrying forever. |
| Bot restart | Watchers lost; posts stop updating (§5.5). |

---

## 10. Testing

Following the suite's existing approach — build subjects with
`object.__new__`, no live Discord, no live RomM:

- **State machine** (`watcher.py`) — every transition, driven by fabricated API
  payloads: `None` mid-session *not* ending it; the pending
  timeout; multiple concurrent rooms; and the snapshot-iteration guarantee
  (registering a watcher mid-tick must not raise). Note that `{}` → room →
  `{}` is **not** a single transition: the trailing `{}` ends the session only
  when the last-known rooms had more than one free seat. One free seat means
  the room may have filled (§9), and the session goes unlisted rather than
  ending — with its own transitions to cover: unlisted → listed (a seat
  opened), and unlisted → ENDED at the session cap, stamping `ended_at` from
  `unlisted_since`.
- **Edit suppression** — an unchanged payload across two ticks issues no edit.
  This protects §5.3's rate-limit rule, which is otherwise invisible.
- **Embed formatters** (`embeds.py`) — one test per state; a password-protected
  room; missing cover art; the `ENDED` re-run hint (§5.2).
- **ROM resolution** (§6.1) — 0 / 1 / many / >25 matches, including that the
  \>25 case tells the user it truncated.
- **Client methods** — `list_netplay_rooms` builds the right URL with
  `game_id`; distinguishes `None` from `{}`.
- **Extension loading** — adding `cogs.netplay` to `core_cogs` gets it the
  baseline import/`setup()` check at
  [tests/test_extension_loading.py:70](../../../tests/test_extension_loading.py#L70)
  free. It does **not** get the substantive checks free: the per-cog tests
  (cog registered, commands registered, option signature pinned) are
  hand-written, and `fake_bot` will need `NETPLAY_*` attributes on its config
  `SimpleNamespace`. A netplay analogue must be written.
- **Cog lookups** — if the cog reaches other cogs by string name,
  `tests/test_cog_lookups.py` covers it.

---

## 11. Risks

- **EmulatorJS `nightly`.** Enabling netplay switches in-browser play from
  pinned `4.2.3` to `nightly` from CDN, for *all* users, not just netplay
  sessions. Upstream's decision, in `Base.vue`. Regular players may see
  regressions; reverting means turning netplay off.
- **Host upload bandwidth is the real ceiling.** This is not lockstep netplay.
  The host renders the game and streams video to each guest over WebRTC
  (`initWebRTCStream`, `drawVideoToCanvas`, `adjustVideoBitrate`,
  `_preferH264`); guests send inputs back. A 4-player room means the host is
  uploading three video streams. The cog cannot fix this, but it affects how
  large a session is realistically worth announcing, and it means the best
  host is whoever has the best upstream — not whoever asked first.
- **Remote NAT traversal is unproven.** Testing used two peers on one machine,
  so ICE never left host candidates and the configured STUN server was never
  exercised. Two players behind symmetric NAT will need TURN, which means
  running coturn. Worth confirming with two real remote people before
  building.
- **`netplay:` is undocumented upstream.** It is a real key read by
  `config_manager.py`, but absent from `config.example.yml`, which suggests it
  is not yet a stable interface.
- **Room shape may change.** `RoomsResponse` is young. `embeds.py` should be
  the only place that reads its fields.

---

## 12. Open question

Netplay and the planned streaming queue are adjacent enough to confuse people:
both are "play a game from Discord", but streaming is
one-platform/one-user/server-side while netplay is
many-users/browser/peer-to-peer. Worth settling the naming and the "which one
do I want?" story while `cogs/streaming` is still unwritten.

Note that `integrations/romm_streaming.py`, referenced in §8 as a pattern, is
**untracked and unmerged** at the time of writing — an implementer who clones
the repo will not find it. It is not a dependency of this design.

---

## 13. Interactions with other in-flight specs

Three specs share the date 2026-09-13 and at least two amend the same line of
code. This section exists so the second one to land does not silently revert
the first.

| Spec | Touches | Conflict |
|---|---|---|
| This one | `romm_client.py:158`, `README.md:152` | adds `assets.read` |
| [per-user-romm-auth](2026-09-13-per-user-romm-auth-design.md) | `romm_client.py:158`, `README.md:152` | adds `roms.user.write` |
| [feed-clients](2026-09-13-feed-clients-design.md) | — | none known |

**Intended union**, whichever lands first:

```
roms.read platforms.read firmware.read users.read users.write me.write assets.read roms.user.write
```

Both specs independently require operators to **reissue every existing
`ROMM_CLIENT_TOKEN`**. That should be asked of them once, in whichever release
carries the second change — not twice.
