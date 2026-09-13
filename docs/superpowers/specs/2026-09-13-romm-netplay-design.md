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

That is the gap this cog fills. `/netplay <game>` posts a Discord embed for a
game in the library, then keeps it current — no room yet, room open with two of
four seats filled, session over — by polling RomM.

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

- `/netplay <game>` — pick a game from the library, post an announcement embed.
- The embed tracks the session: waiting → open → ended.
- Enrich the owner's name with their Discord identity when the RomM account is
  linked.

### v2 (designed for, not built)

- **Interest registry.** "Ping me when someone hosts Bomberman." Needs a table
  via `database_manager.py` and a subscribe/unsubscribe UI. Deferred because it
  is a larger surface than the announce loop, and pointless until the announce
  loop is proven to get used.
- **Role or channel ping on announce.** Config-driven, cheap, but blunt — the
  same ping regardless of game. Wants the registry's targeting to be worth much.

### Non-goals

- The bot never opens, joins, or proxies a room. It cannot: the socket.io
  handshake requires a `sessionId`/`playerId` and would make the bot a
  participant.
- No enumeration of all live rooms. The API cannot do it (§2.1).
- No RetroArch lobby integration (§2.5).
- No persistence of watchers across restarts (§5.4).

---

## 4. Architecture

Netplay is RomM's own API, not a third-party service, so nothing goes in
`integrations/` — that folder is for external services with their own
credentials, like ggrequestz. The API calls belong on the existing RomM client
and the user-facing surface is an ordinary cog.

| File | Responsibility |
|---|---|
| `romm_client.py` | Two new methods (§7). Existing session, token, retry. |
| `cogs/netplay/__init__.py` | Re-exports + `setup()`, mirroring `cogs/requests/__init__.py`. |
| `cogs/netplay/cog.py` | The `/netplay` command, game picker, watcher registry. |
| `cogs/netplay/watcher.py` | The poll loop and the state machine (§5). |
| `cogs/netplay/embeds.py` | One formatter per state. |

Registered in `bot.py`'s `core_cogs` list, which `tests/test_extension_loading.py`
reads directly — so adding it there is what puts it under test.

The game picker reuses the paginated select already built for requests
(`cogs/requests/views_game.py`); platform emoji and slug normalisation come from
`cogs/platform_emoji.py`. Neither needs changes.

### Why not a socket.io client

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

### 5.2 The poll set

`/api/netplay/list` requires a `game_id`, so there is no cheap "what's live
right now" query. The poll set is therefore **only the rom ids with a live
Discord post**, which means every request maps to a message someone is
watching, and the set drains on its own.

A single `discord.ext.tasks` loop iterates all active watchers per tick rather
than one task per post.

### 5.3 Multiple rooms

The endpoint returns a dict, so a game can have several concurrent rooms. The
embed renders 0, 1, or N. The `LIVE → ENDED` transition is "the dict became
empty", not "a particular room vanished".

### 5.4 Restart

Watchers live in memory and die with the cog. A netplay room rarely outlives a
bot restart, and reconciling orphaned embeds would cost a table and a boot-time
sweep to solve a problem that mostly resolves itself. On restart, existing posts
simply stop updating; they should be written so a stale one reads as stale
rather than as a live session.

---

## 6. Commands and flow

```
/netplay <game>
  └─ paginated select of matching library ROMs (existing component)
       └─ user picks one
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

**Identity.** `player_name` defaults to the RomM username, so it can usually be
mapped to a Discord user via `cogs/user_manager.py`. It is **not** trustworthy —
it is client-supplied and was trivially overridden to an arbitrary string during
testing. Render it as a display hint; never use it for authorization or to
attribute an action to a Discord account.

---

## 7. RommClient additions

Two methods, both using the existing `make_authenticated_request` (no caching —
room state must be fresh, and `fetch_api_endpoint`'s cache would serve stale
counts):

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

`None` means the call failed, `{}` means no rooms. The watcher must distinguish
them: a failed poll is not an ended session, and must not flip an embed to
`ENDED`.

---

## 8. Configuration

Per the project rule that `Config` in `bot.py` owns all environment reading:

| Variable | Default | Purpose |
|---|---|---|
| `NETPLAY_ENABLED` | `true` | Load the cog at all. |
| `NETPLAY_POLL_INTERVAL` | `20` | Seconds between ticks. |
| `NETPLAY_PENDING_TIMEOUT` | `900` | Seconds before `PENDING` → `EXPIRED`. |
| `NETPLAY_MAX_WATCHERS` | `25` | Cap on concurrent live posts. |

**Token scope.** `/api/netplay/list` requires `assets.read`, which is **not** in
the scope list the README currently documents for `ROMM_CLIENT_TOKEN`
(`roms.read platforms.read firmware.read users.read users.write me.write`).
Existing tokens will get 403. Both the README and the OAuth grant in
`romm_client.py` need `assets.read` added, and operators must reissue their
client token.

**Server prerequisites**, checked once at startup and reported clearly:

- `EJS_NETPLAY_ENABLED` must be true, or every room list is permanently empty.
- `EJS_NETPLAY_ICE_SERVERS` empty means host-candidate-only WebRTC — LAN works,
  remote players generally do not. Warn, don't disable.

---

## 9. Failure modes

| Condition | Behaviour |
|---|---|
| Server has netplay disabled | Cog loads, `/netplay` refuses with an explanation. Mirrors `romm_streaming.py`'s `server_enabled` gate. |
| ICE servers empty | Warn at startup; commands still work (LAN play is legitimate). |
| Token lacks `assets.read` | Detected at startup by a probe call; refuse with the scope named, since the 403 is otherwise silent. |
| Poll returns `None` | Leave the embed alone. Do not flip to `ENDED`. Escalate to `ENDED` only after several consecutive failures. |
| Watcher cap reached | Refuse politely; suggest waiting for a session to end. |
| Message deleted | Drop the watcher on the resulting 404 rather than retrying forever. |
| Bot restart | Watchers lost; posts stop updating (§5.4). |

---

## 10. Testing

Following the suite's existing approach — build subjects with
`object.__new__`, no live Discord, no live RomM:

- **State machine** (`watcher.py`) — every transition, driven by fabricated API
  payloads: `{}` → room → `{}`; `None` mid-session not ending it; the pending
  timeout; multiple concurrent rooms.
- **Embed formatters** (`embeds.py`) — one test per state; a room with a
  password; a room with no linked Discord user; missing cover art.
- **Client methods** — `list_netplay_rooms` builds the right URL with
  `game_id`; distinguishes `None` from `{}`.
- **Extension loading** — adding `cogs.netplay` to `core_cogs` brings it under
  `tests/test_extension_loading.py` automatically.
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

Netplay and the planned streaming queue (`cogs/streaming`, on top of the
existing `romm_streaming.py`) are adjacent enough to confuse people: both are
"play a game from Discord", but streaming is one-platform/one-user/server-side
while netplay is many-users/browser/peer-to-peer. Worth settling the naming and
the "which one do I want?" story while `cogs/streaming` is still unwritten.
