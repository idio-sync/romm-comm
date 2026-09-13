# Per-User RomM Authentication — Design Spec

**Date:** 2026-09-13
**Status:** Approved (brainstorming), revised 2026-09-13 after code review — ready for implementation plan once the live-instance verifications are done
**Feature area:** Credential custody. Unblocks any RomM feature where the server binds a resource to the acting user; the first is the streaming session queue.

## Overview

The bot holds exactly one RomM credential, its own. Every API call is attributed to the bot user. That is fine for reads and for admin actions, and fatal for anything RomM binds to an owner — an emulator streaming session is claimed by a user, and only that user or an admin can control it. If the bot claims, the human cannot touch their own session in RomM's web UI.

This design adds a second, per-user credential path: a Discord user pairs their RomM account with the bot once, via RomM's OAuth 2.0 device authorization grant, and the bot afterwards acts *as that user* for the narrow set of calls that need it. Everything else keeps using the bot token, unchanged.

The bot becomes custodian of credentials for every paired user. The design is shaped by that first and by convenience second.

## Decisions (locked during brainstorming)

| Decision | Choice |
|----------|--------|
| Enrollment | **Device authorization grant** (`/api/auth/device/*`). The only surface the bot can initiate. |
| `/api/client-tokens` role | **Audit and revocation**, not enrollment — see "The two surfaces are one credential". |
| Per-user scopes | **`me.read roms.user.write`** and nothing else. |
| Revocation | Bot **does** revoke, via `DELETE /api/client-tokens/{id}/admin` on the *bot's* token. The user's token gains no scope for this. |
| Encryption at rest | AES-256-GCM, key from file or env, AAD bound to `discord_id`. Fail closed. |
| Storage | New `romm_user_tokens` table, not columns on `user_links`. |
| Relationship to `user_links` | **Independent, but not credulous.** Pairing does not require a link and never rewrites one. No link → store and flag. A link that *contradicts* the approver's identity → refuse (see `/pair` step 7; revised after code review). |
| Vocabulary | **Pairing** (`/pair`, `/unpair`, `/pair-status`, `/pairings`), distinct from `user_manager`'s "linking". |
| Code placement | Root package `romm_tokens/`; Discord surface in `cogs/pair/`. |
| Client seam | `RommClient.acting_as(grant) -> ActingClient`; existing callers unchanged. |
| Unpaired users | Refused with a pointer to `/pair`. **Never** a silent fallback to the bot token. |

## RomM 5.2 API surface (verified against the live instance's `/openapi.json`)

### The two surfaces are one credential

The original framing treated `/api/auth/device/*` and `/api/client-tokens` as alternatives to choose between. They are not. `DeviceAuthTokenResponse` returns `{access_token, device_id, scopes, expires_at}` and `ClientTokenSchema` carries a `device_id`: **the device grant mints a client token.** The two paths differ only in who initiates.

- `POST /api/client-tokens` requires `me.write` *as the user*. The bot can never call it.
- `POST /api/client-tokens/{token_id}/pair` is the mirror direction — the user creates a token in RomM's UI, gets a code, and a client redeems it via the unauthenticated `POST /api/client-tokens/exchange`. Workable, but it makes the user start in RomM and paste a code into Discord.
- `POST /api/auth/device/init` is unauthenticated and bot-initiable. The bot starts the flow, the user approves in a browser, the bot polls. This is the one that fits a Discord command.

So: device grant for enrollment, `/api/client-tokens` for the things enrollment cannot do — reading `token_id` back for audit, listing every token server-side, and revoking.

### Device grant endpoints

| Endpoint | Auth | Notes |
|----------|------|-------|
| `POST /api/auth/device/init` | **none** | `{client_device_identifier, name, client, platform, client_version, requested_scopes}` → `{device_code, user_code, verification_path, verification_path_complete, expires_in, interval}` |
| `POST /api/auth/device/token` | **none** | `{device_code}` → `{access_token, device_id, scopes, expires_at}` |
| `POST /api/auth/device/approve` | `me.write` | The user's browser calls this, not the bot. |
| `POST /api/auth/device/deny` | `me.write` | Likewise. |
| `GET /api/auth/device/pending/{user_code}` | `me.read` | RomM's own approve screen reads this. Not used by the bot. |

Three properties of this grant shape the whole design:

1. **No refresh token.** The response has no refresh credential. Renewal is re-pairing. The design therefore needs proactive expiry warnings, and `get_grant()` returning `None` must be an ordinary outcome rather than an error.
2. **`expires_at` is nullable, and the lifetime is chosen by the approver.** `DeviceAuthApprovePayload.expires_in` is set in RomM's UI; `init` cannot request a bounded lifetime. The bot must handle both a token that expires next week and one that never expires.
3. **`verification_path` is relative, by design.** The spec's own description: *"Relative web-UI path (/pair/device). The client joins it with the origin it was configured to reach; the server is origin-agnostic."* A URL built from `API_BASE_URL` is frequently a LAN address (`http://bishop.lan:8087`) and useless in the browser the user will actually open. It must be built from the public origin.

### Client-token endpoints used

| Endpoint | Auth | Used for |
|----------|------|----------|
| `GET /api/client-tokens` | `me.read` (user token) | Recording `token_id` for the freshly minted token, matched by `device_id`. |
| `GET /api/client-tokens/all` | `users.read` (bot token) | Admin reconciliation: what RomM knows that we do not, and vice versa. |
| `DELETE /api/client-tokens/{token_id}/admin` | `users.write` (bot token) | Real revocation on `/unpair` and on member departure. |

### Scope minimization

RomM's scope list is fine-grained enough that the per-user token is genuinely small. Verified requirements:

| Call | Scope | Carried by |
|------|-------|-----------|
| `GET /api/streaming/config` | `roms.read` | **bot token** |
| `GET /api/streaming/sessions` | `roms.read` | **bot token** |
| `POST /api/streaming/sessions` (claim) | `roms.user.write` | **user token** |
| `DELETE /api/streaming/sessions/{platform}` | `roms.user.write` | user token (polite reclaim) or bot token (force) |
| `DELETE /api/streaming/sessions` (force all) | `roms.user.write` | **bot token** |
| `.../save-and-exit`, `.../save-state`, `.../load-state`, `.../mute`, `.../volume` | `roms.user.write` | **user token** |
| `GET /api/users/me` | `me.read` | **user token** (identity + liveness) |

This yields the governing invariant:

> **Reads stay on the bot token. Only the acting write carries a user token.**

`me.read` earns its place for one reason: `DeviceAuthTokenResponse` does not say *who* approved. Without `GET /api/users/me` the bot would hold a credential it cannot attribute. It doubles as the revalidation probe.

**The bot's own scope string must grow `roms.user.write`.** The force-reclaim paths in the table above are assigned to the bot token, and `romm_client.py:157` currently requests:

```python
'roms.read platforms.read firmware.read users.read users.write me.write'
```

No `roms.user.write`. Without adding it, every admin force-release 403s on a password-grant deployment, and on a `ROMM_CLIENT_TOKEN` deployment it depends on a scope nobody was told to tick. Add it to the grant and to the README's documented token scopes.

**Blast radius of one stolen row, stated accurately.** The earlier draft said "start and stop emulator sessions, and read that user's own profile", which undersold it. `roms.user.write` authorizes, verified against the OpenAPI document:

- every streaming session endpoint, including `DELETE /api/streaming/sessions` (all sessions)
- `POST/PUT/DELETE /api/roms/{id}/notes/...` — write, edit and **delete** that user's ROM notes
- `PUT /api/roms/{id}/props` — overwrite rating, difficulty, completion, status, backlogged, hidden
- `POST /api/play-sessions`, `DELETE /api/play-sessions/{session_id}` — forge and delete playtime history
- `POST/DELETE /api/activity/heartbeat`

And `me.read` via `GET /api/users/me` returns `UserSchema`, which includes **`email`**, `ra_username` and `oauth_scopes` — so the exposure includes PII, not merely a username.

The decision stands: there is no narrower scope that can claim a session, so this *is* the minimum that works. But the README must carry this list rather than the flattering summary, because it is what an operator reads when deciding whether to enable the feature.

**Residual risk, accepted:** `DELETE /api/streaming/sessions` ends *every* session, and a stolen user token can call it. That user could already do so from RomM's web UI, so it is not a privilege escalation.

## Architecture

```
romm_client.py                 # + identity seam, + ActingClient
romm_tokens/
  __init__.py                  # TokenStore facade, Grant dataclass
  device_flow.py               # init / poll / URL construction. Borrows the client's session + limiter.
  crypto.py                    # seal / open. AES-256-GCM, AAD = discord_id.
  repo.py                      # all SQL against romm_user_tokens
  store.py                     # orchestration: begin_pair, complete_pair, get_grant, invalidate
cogs/pair/
  __init__.py  cog.py  views.py  embeds.py
qr.py                          # lifted from cogs/search.py, filename parameterized
integrations/romm_streaming.py # _send grows a `grant` parameter
```

`romm_tokens/` is a root package rather than a feature package under `cogs/` because `integrations/romm_streaming.py` must resolve a Discord ID to a token. Under `cogs/`, an integration would import from a cog — the wrong layering, and exactly what a boundary test should forbid. It is also not placeable in `integrations/`, where `load_integration_cogs` treats every non-underscore module as a loadable extension.

`RommClient` deliberately does **not** depend on `romm_tokens`. It keeps taking a `Config` and nothing else, which is what makes it constructible in a test. The bot wires the store, the way it already wires `self.db` and `self.romm`.

### The client seam

`romm_client.py` builds `Authorization: Bearer {self.access_token}` in **three** places, not two: `romm_client.py:329` (`make_authenticated_request`), `romm_client.py:451` (`_get_json`), and `romm_client.py:358`.

Line 358 is the one that matters most, and it is the trap in this whole design:

```python
if response.status == 401:
    ...
    if await self.refresh_oauth_token():
        headers["Authorization"] = f"Bearer {self.access_token}"
        async with session.request(method, url, **request_kwargs) as retry_response:
```

A user token revoked in RomM returns 401. This branch refreshes the **bot's** OAuth token and replays the request as the bot — on a `POST /api/streaming/sessions` that means claiming the session as the bot, which is the exact bug this feature exists to eliminate, reintroduced at the moment a user's credential lapses. An implementer who converts only the two obvious header sites ships it.

So the rule is explicit: **on the acting path a 401 or 403 is terminal.** No refresh, no replay. It is raised as `RommAuthError`, which the store turns into `invalid_since`.

All three sites become `await self._auth_header(grant)`, where `grant is None` means the bot. On top of that seam:

```python
client.acting_as(grant, on_auth_failure=None) -> ActingClient

class ActingClient:
    async def request(
        self, method: str, path: str, *,
        json: Optional[dict] = None,
        timeout: Optional[int] = None,
    ) -> tuple[int, Optional[dict]]:
        """Raises RommAuthError on 401/403. Never retries. Never caches."""
```

**Callers do not build these; the store does.** `store.acting_client(discord_id)` reads the grant and returns an `ActingClient` with `on_auth_failure` already wired to the store's own generation-guarded invalidation. `RommClient` stays dependent on `Config` alone — it never imports `romm_tokens`, and the callback is how the policy reaches it. The alternative, leaving each caller to invalidate after catching `RommAuthError`, is the same forgettable-step failure mode that `ActingClient` exists to remove; a revoked credential would stay live in the database until the hourly loop noticed.

`ActingClient` shares the session, connector and rate limiter, has the grant pre-bound, and exposes only this one method. Per-user code holds an object that *cannot* act as the bot; bot code changes in zero places. The alternative — an `identity=` kwarg threaded through every call site — was rejected because forgetting it fails silently as the bot.

The `(status, body)` return is not a stylistic choice. `integrations/romm_streaming.py` deliberately avoids `make_authenticated_request` and says why in its module docstring: the difference between 409 (occupied, queue them), 404 (no container, never going to work) and 502/503 (broker down, tell an admin) "is the entire behaviour of the queue". Returning `Optional[Dict]` in the house style would collapse `ClaimOutcome`'s taxonomy into `ERROR`. The `timeout` parameter exists for the same consumer — `CLAIM_TIMEOUT_SECONDS` is 60 and `SAVE_AND_EXIT_TIMEOUT_SECONDS` is 45, both far above the client's default.

Note that `RommApiError` / `RommAuthError` are today reachable only *inside* `_get_json`; `fetch_api_endpoint` catches both and returns `None`, and `make_authenticated_request` swallows everything. The revalidation loop's three-way branch depends on `ActingClient.request` being the first public method that lets them out, so raising from it is load-bearing rather than tidy.

**Hard invariant: the acting path never touches `APICache`.** `fetch_api_endpoint` keys the cache by endpoint alone, so a per-user response landing there would be served to a different user. `ActingClient` therefore has no cache-backed read method at all — the cache is unreachable from it by construction, not by discipline. A test pins this.

### Storage

```sql
CREATE TABLE IF NOT EXISTS romm_user_tokens (
    discord_id        INTEGER PRIMARY KEY,
    romm_user_id      INTEGER NOT NULL,    -- verified via GET /api/users/me
    romm_username     TEXT    NOT NULL,    -- cached for the audit view
    device_id         TEXT    NOT NULL,
    token_id          INTEGER,             -- from GET /api/client-tokens; enables revocation
    generation        INTEGER NOT NULL DEFAULT 1,  -- bumped on every re-pair; see below
    scopes            TEXT    NOT NULL,    -- JSON array, as granted (may be less than requested)
    expires_at        TIMESTAMP,           -- nullable: RomM permits non-expiring tokens
    key_fingerprint   TEXT    NOT NULL,
    sealed            BLOB    NOT NULL,    -- nonce || ciphertext || tag
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_used_at      TIMESTAMP,
    last_verified_at  TIMESTAMP,
    expiry_warned_at  TIMESTAMP,
    invalid_since     TIMESTAMP
);
```

A separate table rather than columns on `user_links`, for three reasons. The lifecycles differ — a link is an admin-asserted mapping, a pairing is a user-granted credential, and either can exist without the other. The sensitivities differ. And `get_all_user_links()` splats rows into dicts that views render directly; ciphertext must never be reachable from a code path that ends in an embed.

`repo.py` therefore returns two different shapes: an **audit row** (everything except `sealed` and `key_fingerprint`) for every caller, and the sealed blob only to `store.get_grant()`. Exactly one function in the tree can produce a bearer token.

**`generation` exists so a slow failure cannot kill a fresh credential.** Every background write is conditional on it:

```sql
UPDATE romm_user_tokens SET invalid_since = ? WHERE discord_id = ? AND generation = ?
```

Without it there is a live race. The revalidation loop probes token A; while that request is in flight the user runs `/pair` and stores token B; the probe returns 401 for the *already-replaced* token A, and the loop writes `invalid_since` against a row that now holds a working credential. The user is told to re-pair immediately after re-pairing, and the streaming queue skips them. The window is exactly as long as one HTTP timeout, which is to say it is reachable on any instance that restarts while people are pairing.

`generation` is incremented on every successful store, `Grant` carries the value it was read at, and **every** deferred write keyed by `discord_id` — `invalid_since`, `last_verified_at`, `last_used_at`, `expiry_warned_at` — carries the `AND generation = ?` guard. A write that affects zero rows is not an error; it means the credential moved on and the result is stale, which is logged at debug and otherwise ignored. This is also what makes step 8's "still the current attempt" check enforceable at the database rather than only in memory.

**`romm_user_id` is not unique, and that is a reportable condition rather than a constraint.** `discord_id` is the primary key, so two Discord users can pair the same RomM account. A `UNIQUE` constraint would be wrong — a re-pair after a botched `/unpair`, or a genuinely shared household account, would fail at the database layer with nothing useful to say. Instead `/pairings` surfaces a shared `romm_user_id` as a first-class warning, and `/pair` refuses a second pairing of an already-paired RomM account unless an admin overrides. With step 7's identity check this is defence in depth rather than the primary barrier.

**DDL lives in `database_manager.py`, queries live in `repo.py`.** The table is created in `_create_user_tables` alongside `user_links`, because `MasterDatabase` owns the schema and `initialize()` runs it on every start — which means existing databases get the table from the same `CREATE TABLE IF NOT EXISTS` that new ones do. No `migrate_romm_user_tokens` function: the earlier draft proposed one by analogy with `migrate_user_link_schema`, but that exists because `user_links` gained a column *after* shipping, which is not this situation. A migration guard becomes necessary the first time this table gains a column, and not before.

### Encryption

AES-256-GCM via `cryptography` (a new pinned runtime dependency). A fresh 12-byte nonce per write. **AAD is the `discord_id`**, so a row copied between users in the database file fails to open rather than handing user A's token to user B.

Key material comes from `ROMM_TOKEN_KEY_FILE` (preferred — a Docker secret or read-only mount, absent from `docker inspect`) or base64 `ROMM_TOKEN_KEY`, both read in `Config`. `key_fingerprint` is a truncated hash of the key, stored per row, so "wrong key" is distinguishable from "corrupt data".

**Rotation** needs the old key material, not just its fingerprint — a fingerprint identifies a key, it does not recover one. So rotation is: set `ROMM_TOKEN_KEY_OLD` alongside the new key, and on startup every row whose fingerprint matches the old key is opened with it and re-sealed under the new one, after which `ROMM_TOKEN_KEY_OLD` can be dropped. A row whose fingerprint matches no configured key is marked invalid and its user is asked to re-pair; rotating without `ROMM_TOKEN_KEY_OLD` is therefore a supported but lossy path, and the README says so.

**What this buys, stated honestly:** it protects a copied `data/` directory, a backup, a volume snapshot, an accidentally committed database. It does **not** protect against host compromise — anything that can read the process environment or memory has the key. The README says this rather than implying more.

**Fail closed.** With no key configured, the pairing feature refuses to load and logs an error; it never stores plaintext. A row that fails to open is treated as revoked — `invalid_since` is set and the user is prompted to re-pair — rather than raising.

### Lines credential material never crosses

These rules are scattered through the sections above by necessity; collected here because the security story should be auditable in one read, and because each needs a test.

1. **Never into the response cache.** `ActingClient` has no cache-backed method; `APICache` is keyed by endpoint alone, so a per-user response in it would be served to another user.
2. **Never into a log record.** Not the `access_token`, not the `device_code`, not the sealed blob. This is a codebase that debug-logs response bodies and logs full bodies on failure (`romm_client.py:_read_response`), so two paths need closing by hand: `device_flow.py` must not log the parsed `device/token` response, which *contains* `access_token`; and `Grant` must declare its token field `repr=False`, or a dataclass's generated `__repr__` prints the credential in every f-string, `logger.exception`, pytest assertion diff and py-cord traceback. `repr(grant)` not containing the token is a test.
3. **Never into an embed or a Discord message.** `repo.py`'s audit row omits `sealed` and `key_fingerprint`, so no view can render what it cannot fetch. `/pair-status` and `/pairings` show metadata only.
4. **Never persisted, in the case of `device_code`.** One in-memory dict, one pairing attempt, cancelled on `/unpair` or a second `/pair`.
5. **Never held across actions.** A `Grant` is fetched from the store per action and never stored in view state, cog attributes, or a queue entry. The queue's premise is acting minutes after the user last spoke, and the way that stays safe is re-reading at claim time — which is also what makes revocation take effect promptly.

### Configuration

All read in `bot.py`'s `Config`, per house style. No `os.getenv` outside it.

| Variable | Default | Purpose |
|----------|---------|---------|
| `ROMM_USER_AUTH_ENABLED` | `false` | Master switch for the pairing feature. |
| `ROMM_TOKEN_KEY_FILE` | — | Path to a file holding the 32-byte key. Preferred. |
| `ROMM_TOKEN_KEY` | — | Base64 32-byte key. Fallback. |
| `ROMM_PAIR_BASE_URL` | see below | Origin the relative `verification_path` is joined to. |
| `ROMM_PAIR_ROLE_ID` | `AUTO_REGISTER_ROLE_ID` | Gates who may run `/pair`. With both unset, any guild member may pair — the rate limits, not the role, are what stop abuse. |

**`ROMM_PAIR_BASE_URL` precedence, stated precisely, because the obvious spelling is broken.** `bot.py:309` reads:

```python
self.DOMAIN = os.getenv('DOMAIN', 'No website configured').rstrip('/')
```

`DOMAIN` is therefore **never empty** — it defaults to a human-readable sentinel. "Default to `DOMAIN`, else `API_BASE_URL`" would never fall back and would never warn; an instance with no `DOMAIN` set would DM its users `No website configured/pair/device?user_code=…`. Since this spec names the pairing URL as the most likely day-one failure, that is the day-one failure.

The precedence is: `ROMM_PAIR_BASE_URL` if set → `DOMAIN` **only if it parses as an `http`/`https` origin** → `API_BASE_URL`, with a startup warning that the pairing URL may be unreachable from a phone. `validate()` rejects a `ROMM_PAIR_BASE_URL` that is not a parseable http(s) origin.

This is not hypothetical. The probe run on 2026-09-13 reported `DOMAIN unusable, fell back to API_URL` on the development instance — so the naive spelling would have DM'd that instance's users a URL beginning `No website configured`, and the guard is what turns it into a startup warning instead.

**Missing key material disables the feature; it does not kill the bot.** The earlier draft said `validate()` raises, which contradicts the fail-closed paragraph below and would take down a working bot over an optional feature. `Config.validate()` raises only for genuinely fatal values (`TOKEN`, `GUILD`, `API_URL`), and the house pattern for an optional feature is to degrade — `tests/test_igdb_token.py` pins exactly that for IGDB. So: with `ROMM_USER_AUTH_ENABLED` true and no usable key, `Config` logs an error and forces the feature off, `cogs/pair` does not register its commands, and the rest of the bot starts normally.

`ROMM_TOKEN_KEY` is validated at startup, not at first use: a base64 value that does not decode to exactly 32 bytes is an error naming the problem, rather than an `InvalidKey` surfacing inside someone's first `/pair`.

## Flows

### `/pair`

1. **Gate.** Feature enabled, key present, role check, **one in-flight pairing per Discord user and a global concurrency cap**. `device/init` is unauthenticated, so an ungated `/pair` turns the bot into a spam relay against the RomM instance.
   - For the same reason `device_flow.py` takes the client's session and `RateLimit` rather than constructing its own. A poll loop running at the server's `interval`, times the concurrency cap, is the highest-volume thing this feature does; leaving the one high-volume path outside the limiter that governs every other call would be an odd place to make an exception.
2. **Already paired?** Report the existing pairing (RomM username, scopes, expiry) and offer a re-pair button rather than silently minting a second credential.
3. **`POST /api/auth/device/init`.**
   - `client_device_identifier` — `romm-comm:<discord_id>`. Stable per Discord user, so re-pairing reuses one RomM device row instead of accumulating them. It puts the Discord ID into RomM's device table, which is deliberate: it is not secret, it makes the admin audit view joinable, and the RomM instance is already trusted with this user's library.
   - `name` — **bot-composed, never user-supplied**: `romm-comm · @<sanitized display name> · <4-char code>`. Display names are attacker-controlled and this string is rendered on RomM's approve screen, so it is stripped of markdown and non-printable characters and truncated, falling back to the Discord ID if it sanitizes to empty.
   - `client` `"romm-comm"`, `platform` `"discord"`, `client_version` from a module constant.
   - `requested_scopes` `["me.read", "roms.user.write"]`.
4. **DM the user**: the URL (`ROMM_PAIR_BASE_URL` + `verification_path_complete`), the `user_code` as a text fallback, a QR of the URL, and the 4-char code. The interaction reply is ephemeral and says to check DMs; a blocked DM falls back to an ephemeral message carrying the same content, reported through an outcome enum in the style of `InviteOutcome`. `init` returns **201**, not 200.
   - **`device_code` never appears in Discord and is never persisted.** It alone bears the grant. It lives in one in-memory dict for the life of the attempt. A second `/pair` or an `/unpair` cancels the in-flight poll task.
5. **Poll `POST /api/auth/device/token`** at the returned `interval` (5s observed), giving up at `expires_in` (600s observed — so at most ~120 polls per attempt). Every outcome arrives as **HTTP 400 with an RFC 8628 error in `detail`**, so the loop branches on that string and not on the status: `authorization_pending` continues, `slow_down` continues with a widened interval, `access_denied` and `expired_token` are terminal, and an unrecognised `detail` is treated as terminal and logged. A loop keyed on the status code alone would spin until expiry on a denial.
6. **Three checks before storing anything.**
   - `GET /api/users/me` on the new token → `romm_user_id`, `romm_username`. Without this the bot holds a credential it cannot attribute.
   - `GET /api/client-tokens` → `token_id`. Matched on `device_id`, but that match is **not** unique: step 3 deliberately reuses one device row across re-pairings, so several client tokens can share a `device_id`, and `ClientTokenSchema.device_id` is itself nullable. The tiebreak is the newest `created_at` among rows whose `name` carries the bot's prefix; a null `device_id` or no matching row leaves `token_id` null, which `/unpair` handles. Recording the wrong `token_id` would mean revoking the wrong credential, so this is stated rather than left to taste.
   - Granted `scopes` vs requested. **If `roms.user.write` was not granted, discard the token** and tell the user which scope to approve. Storing a credential that cannot do its job is worse than storing none.
7. **Identity check — refuse on contradiction, and never guess.** Three cases, deliberately different:
   - **No `user_links` row** → store, and flag the unverified pairing in the admin audit view. This is the ordinary case for a server where the bot did not create accounts.
   - **A `user_links` row naming a *different* `romm_user_id`** → **discard the token**, tell the user which account the bot expected, and alert the admin channel.
   - **The lookup failed** → **discard the token** and tell the user to try again. Not "store and flag".

   That third case needs its own accessor, because the existing one cannot express it. `MasterDatabase.get_user_link` ends (`database_manager.py:663-665`):

   ```python
   except Exception as e:
       logger.error(f"Error getting user link for {discord_id}: {e}")
       return None
   ```

   A locked database, a corrupt row or a closed pool is therefore indistinguishable from "this user has no link" — and under the first bullet above, "no link" means *store the pairing*. The identity check would fail open precisely when the database is unhealthy, which is also when an attacker would most like it to. So the store uses a strict accessor, `get_user_link_strict(discord_id)`, which returns `Optional[dict]` for the genuine present/absent answer and **lets the exception propagate** for everything else; pairing aborts on it. The existing swallowing accessor keeps its current behaviour for the existing callers, where a degraded read is cosmetic rather than load-bearing.

   The second case is the phishing defence, and it is the one the earlier draft got wrong by storing and logging a warning. The attack is not exotic: `device/init` is unauthenticated, so an attacker runs `/pair`, forwards their own DM — URL, QR and matching confirmation code — to a victim, and the victim approves. `GET /api/users/me` then returns the *victim's* identity, and storing it would file the victim's credential under the attacker's Discord ID. `user_links` is admin-asserted and is the one authenticated cross-check the bot holds; spending it on a log line was the single place this design inverted its own stated priority. `ROMM_PAIR_ALLOW_IDENTITY_DRIFT` exists for operators who genuinely need the old behaviour, and defaults to false.
8. **Re-check the gate, then seal and store**, then edit the DM to confirm.

   Step 1's gate ran when the user typed `/pair`. Approval can arrive up to `expires_in` later, so everything the gate checked may since have become false. Before the row is written the store re-checks: the in-flight attempt is **still the current one** for this Discord ID (a second `/pair` supersedes the first, and the loser must not overwrite the winner), the member is **still in the guild**, still holds `ROMM_PAIR_ROLE_ID`, and the feature is still enabled. Any of these failing discards the token and attempts the admin revoke, because a credential that was minted for someone who is no longer eligible should not outlive the check that would have refused it.

   Without this, departure is not final: a member can start `/pair`, leave the guild, approve in their browser, and the completion path writes a fresh row for someone who is gone — recreating exactly the credential `on_member_remove` just deleted.

**On the 4-char confirmation code.** It is hygiene, not a control, and the spec should not lean on it. It defends only against an unsolicited bare URL — in the realistic attack above the attacker holds the code, shows the victim a matching one, and the check passes. The controls that actually do work are: the URL only ever reaching the initiator's DM, the `name` field rendered on RomM's approve screen, and the identity check in step 7. That makes verification item 4 — *does RomM's approve screen actually render `name`?* — load-bearing rather than cosmetic. `DeviceAuthPendingSchema` does expose `name`, `client`, `platform` and `requested_scopes` to that screen, so the data is there; only the rendering is unconfirmed.

### `/unpair`

Cancel any in-flight attempt, delete the row, and attempt the server-side revoke via `DELETE /api/client-tokens/{token_id}/admin` on the bot token. The revoke is guarded: the bot only ever admin-deletes a token whose `device_id` matches the row it stored for that Discord user and whose name carries the bot's prefix. It never touches a token it did not mint.

If the bot's token lacks `users.write`, or `token_id` is null because step 6 of pairing could not read it back, or the revoke fails, the DM says plainly that the bot has forgotten the credential but it still exists in RomM, and links to the user's token list with the device name so they can find the right one.

### `/pair-status`

Ephemeral. RomM username, granted scopes, expiry, last used, last verified, and whether the pairing is currently valid. No token material.

### Admin `/pairings`

Gated by `admin_checks.is_admin`. A table of Discord user, RomM user, scopes, expiry, last used, last verified, `invalid_since`, and any `user_links` drift. Never token material.

Plus a **reconcile** action using `GET /api/client-tokens/all` on the bot token, diffing RomM's view against ours in both directions:
- Tokens RomM holds with our prefix that we have no row for — orphans from a wiped database or a failed `/unpair`. Offered for admin deletion.
- Rows we hold that RomM no longer knows — revoked by the user in RomM's UI. Marked invalid, user DM'd once.

### Revalidation loop

A `tasks.loop` at roughly 60 minutes, alongside `invite_reconcile_loop`. Each pass selects rows not verified in 24 hours or expiring within 7 days, staggered, and probes `GET /api/users/me`.

- Success → update `last_verified_at`.
- `RommAuthError` → set `invalid_since`, DM a re-pair prompt once.
- `RommApiError` or timeout → **leave the row untouched.**

Every one of those writes carries `AND generation = ?` against the value read when the probe started, so a slow result cannot land on a credential that has since been replaced. A DM is sent only if the write actually affected a row.

That last distinction matters more than it looks: marking every token dead during a RomM restart would be the worst bug this loop could have. The existing `RommApiError` / `RommAuthError` split already encodes it.

Expiry warnings are DM'd once at 7 days and once at 1 day. `expiry_warned_at` records when the last warning was sent, and a threshold fires only when it is crossed *and* `expiry_warned_at` predates that crossing — so a restart mid-window does not re-send, and the 1-day warning still fires after the 7-day one. Non-expiring tokens are revalidated but never warned.

### Departure and role loss

`on_member_remove` **cancels any in-flight pairing attempt** for that Discord ID, then deletes the row and attempts the admin revoke, logging to `CHANNEL_ID`. A departed member's live credential sitting in the bot's database is the case where leaving one behind is worst — and cancelling the attempt is half the fix, step 8's re-check being the other half. Cancellation alone would not be enough, because the poll task could already be between "token received" and "row written"; the re-check alone would not be enough either, because the attempt would otherwise keep polling a RomM instance on behalf of someone who has gone.

Loss of `ROMM_PAIR_ROLE_ID` also unpairs, but **`cogs/pair` owns its own `on_member_update` listener** for this. The earlier draft said it would reuse `user_manager`'s `handle_role_removal`, which was wrong twice over: that function is about the *RomM account* — it looks up `user_links` and disables or deletes the account unless `created_by_bot` is false — and it is wired to `AUTO_REGISTER_ROLE_ID`, so it would never fire for a distinct `ROMM_PAIR_ROLE_ID`. Reusing it would also mean one of the two cogs importing the other, which is the layering this design otherwise avoids. The two listeners watch different roles for different reasons and share no code; `cogs/pair` never imports `user_manager`, and reads `user_links` through the database manager.

## The first consumer: streaming session queue

Not built here. The contract it needs:

```python
store.get_grant(discord_id) -> Grant | None      # token, romm_user_id, scopes, expires_at
```

`None` covers absent, expired, invalid and undecryptable, and is an **ordinary outcome**. A queue turn arriving for someone whose grant has lapsed skips them, DMs a re-pair prompt, and advances — it is not an error path.

`integrations/romm_streaming.py` changes narrowly: `_send` grows a `grant` parameter and routes through the store-vended acting client — whose `(status, body)` return is exactly what `_send` already produces, so `ClaimOutcome` survives unchanged.

**One line of that file has to change or the whole error taxonomy collapses.** `_send` ends (`integrations/romm_streaming.py:175-177`):

```python
except Exception as e:
    logger.error(f"Streaming request {method} {path} failed: {e}")
    return 0, None
```

`RommAuthError` is an `Exception`, so a revoked credential would be caught here and returned as status `0`. `0` is absent from `_CLAIM_OUTCOMES`, so line 208 maps it to `ClaimOutcome.ERROR` — "the broker is down, tell an admin" — when the truth is `DENIED`, "this user's credential is dead, tell *them* to re-pair". The queue would report an outage and retry against a credential that will never work again.

So `_send` catches `RommAuthError` **before** the catch-all and returns `(401, None)`, which `_CLAIM_OUTCOMES` already maps to `DENIED`. The layering is: `ActingClient` detects and raises; the store's `on_auth_failure` callback invalidates, guarded by `generation`; `_send` translates the exception back into the status code its own taxonomy is built on. Each layer does one thing, and `ClaimOutcome` keeps meaning what it says. `ClaimOutcome.DENIED`'s comment — currently "our token lacks roms.user.write" — becomes "the acting user's credential is revoked, expired or under-scoped". `get_config` and `list_sessions` keep passing `None` and stay on the bot token. `claim` and the in-session controls take the acting user's grant. Polite reclaim (`save_and_exit`) uses the owner's grant; `force_release_all` stays on the bot token as an admin action, subject to verification 7.

(The scope table lists `mute` and `volume` for completeness of the endpoint survey; the sketched client implements `save_state` and `load_state` only. No action needed — it is a list of endpoints, not of methods.)

A queue that must claim a session minutes after the user typed anything is exactly why storage is unavoidable — a design that only held credentials during a single command could not serve it.

## Testing

New tests, `tests/`, pytest:

- **`test_romm_tokens_crypto.py`** — seal/open round-trip; a wrong key fails; **a row moved between `discord_id`s fails the AAD check**; the fingerprint is recorded and drives re-seal.
- **`test_device_flow.py`** — `init` payload shape and scope list; a `DOMAIN` left at its `'No website configured'` default does **not** become the pairing origin; the URL is built from `ROMM_PAIR_BASE_URL` when set; the poll honours `interval` and gives up at `expires_in`. Crucially, the poll is driven by fixtures of the **real** responses: `400 {"detail": "authorization_pending"}` continues, `400 {"detail": "access_denied"}` stops, `400 {"detail": "expired_token"}` stops, `400 {"detail": "slow_down"}` widens the interval, and an unknown `detail` stops rather than spinning. A test that treats 400 as one outcome would pass against a loop that hangs until expiry on every denial.
- **`test_romm_tokens_store.py`** — a granted-scope shortfall discards the token; a `user_links` row naming a different RomM user makes the pairing refuse; a missing `user_links` row stores and flags; **a failing `user_links` lookup discards rather than storing** (the fail-open case: raise from the strict accessor, assert nothing is written); an invalid row makes `get_grant` return `None`; the audit row has no `sealed` or `key_fingerprint` key.
- **`test_romm_tokens_lifecycle.py`** — the four ordering hazards, each as a named scenario:
  - a member who leaves mid-pairing has their attempt cancelled, and a completion that arrives anyway writes no row;
  - a second `/pair` supersedes the first, and the loser's completion does not overwrite the winner;
  - an `invalid_since` write for generation *n* affects zero rows once a re-pair has stored generation *n+1*, and sends no DM;
  - `last_verified_at`, `last_used_at` and `expiry_warned_at` carry the same guard.
- **`test_romm_client_identity.py`** — bot-path headers are byte-identical to today (regression); the acting path uses the grant's token; **a 401 on the acting path raises rather than refreshing, and leaves the bot's `access_token` untouched** (this is the `romm_client.py:358` trap); `ActingClient` exposes no cache-backed method and the acting path writes nothing to `APICache`.
- **`test_romm_tokens_secrecy.py`** — no token material in any emitted log record (assert over `caplog` across a full pair-and-use cycle); `repr(grant)` does not contain the token.
- **`test_streaming_auth_outcome.py`** — a `RommAuthError` raised inside `_send` surfaces as `ClaimOutcome.DENIED`, not `ERROR`; the same failure invalidates the stored credential exactly once. This is a regression test for `integrations/romm_streaming.py:175`'s catch-all, which would otherwise swallow it into status `0`.

Extensions to the structural suite. **Two of these need new scan roots, not new assertions** — `test_sql_boundaries.py`'s `modules()` collects `Path(".").glob("*.py")`, which is non-recursive, and `test_import_boundaries.py` walks only `Path("cogs")`. Adding `romm_tokens/repo.py` to `REPOSITORIES` without widening the scan would be a no-op that also leaves `store.py`, `crypto.py` and `device_flow.py` with zero SQL-boundary enforcement while appearing covered:

- `test_sql_boundaries.py` — extend `modules()` to include `romm_tokens/*.py`, **then** add `romm_tokens/repo.py` to `REPOSITORIES`. A test that the scan actually reaches `romm_tokens/store.py`, in the spirit of the existing `test_the_scan_reaches_inside_the_requests_package`.
- `test_import_boundaries.py` — widen to `integrations/` and `romm_tokens/`, then assert neither imports from `cogs/`, that `cogs/pair` does not import `user_manager`, and that no module outside `romm_tokens/` names the `sealed` column.
- `test_extension_loading.py` — covers `cogs/pair`. The cog joins `core_cogs` in `bot.py` unconditionally and self-disables when `ROMM_USER_AUTH_ENABLED` is false, following `REQUESTS_ENABLED`; `core_cogs` is a flat list read by AST, so a conditional append would defeat that test.

## Targeted refactor

`cogs/search.py:386`'s `generate_qr` hardcodes the attachment filename `download_qr.png`. Lift it to a root `qr.py` with the filename as a parameter; `search.py` calls it. The pairing DM is its second consumer.

This has two ends, not one: `cogs/search.py:795` does `embed.set_image(url="attachment://download_qr.png")`, which must match the filename passed in. Parameterizing one without the other makes the existing QR silently stop rendering.

## To verify against the live instance before implementing

All five are undeclared in the OpenAPI spec, so they cannot be settled by reading it.

These are run **before** the implementation plan is written, not during implementation. Items 1 and 2 can each change the plan's shape rather than one of its steps.

1. **What `POST /api/auth/device/token` returns while pending, on denial, and after expiry.** The spec declares only 201/200 and 422. This is the polling loop's entire control flow.

   **Pending: answered** (`tools/verify_romm_device_auth.py --pending-only`, bishop.lan, 2026-09-13). `init` returns **201** with `expires_in: 600` and `interval: 5` — so a full attempt is at most 120 polls. Pending is:

   ```
   HTTP 400  {"detail": "authorization_pending"}
   ```

   This is RFC 8628's error vocabulary carried in FastAPI's `detail` field, and it settles the loop's design: **branch on the `detail` string, never on the status code.** A 400 is pending, denied and expired all at once, so a loop keyed on status would either spin forever on a denial or abandon a live attempt. The loop must also honour `slow_down` by widening its interval. Denial and expiry are still to capture, but both are now expected at 400 with `access_denied` / `expired_token`; the probe classifies whatever it sees and prints it verbatim.
2. **That `GET /api/client-tokens` lists the just-minted token with a matching `device_id`**, so `token_id` can be recorded. Without it, revocation and reconciliation both lose their handle — see the fallback below.
3. **Whether `GET /api/streaming/sessions` identifies the holding user.** Its response schema is `{}`. The queue needs it to map a session back to a Discord member.
4. **That RomM's approve screen renders the `name` field as sent.** Load-bearing, not cosmetic: with the confirmation code demoted to hygiene, this is one of only three real anti-phishing controls.
5. **The RomM UI path to a user's own token list**, for the `/unpair` instructions.
6. **The format and timezone of `expires_at`.** Both `DeviceAuthTokenResponse` and `ClientTokenSchema` type it as a bare `string`, not `format: date-time`, and nullable. The whole expiry ladder — the 7-day and 1-day thresholds, `expiry_warned_at` crossing logic, "expiring within 7 days" in the revalidation query — depends on parsing it and knowing whether it is aware. A naive/aware mix here yields either no warnings or hourly ones. `python-dateutil` is already pinned.
7. **Whether a token holding `roms.user.write` can release a session it does not own.** The force-reclaim paths assume an admin token overrides ownership, but `roms.user.write` is not an admin-override scope and nothing in the OpenAPI document establishes this. If it cannot, force-reclaim needs a different mechanism and the residual-risk paragraph needs revising.
8. **Whether re-pairing with a stable `client_device_identifier` reuses the device row, and whether it replaces or accumulates client tokens.** Determines whether step 6's `created_at` tiebreak is sufficient or whether stale tokens pile up per user.

**Fallback if verification 2 fails.** `POST /api/client-tokens/exchange` returns `ClientTokenCreateSchema`, which carries both `raw_token` *and* `id` — the `token_id` the device grant never returns. That is the one genuine advantage of the `pair`/`exchange` path this design set aside, and it is the reason revocation currently depends on a lookup that might not resolve. The device grant is still the right primary choice, because it is the only one the bot can initiate and it keeps the user starting in Discord. But if `GET /api/client-tokens` will not yield a usable `token_id`, the answer is to offer `pair`/`exchange` as a second enrollment path — the user creates the token in RomM, pastes the code into `/pair code:<...>` — rather than shipping a design whose `/unpair` cannot revoke.

## Out of scope

The streaming session queue cog itself. `/api/devices` management beyond the `device_id` the grant returns. Sync (`/api/sync/devices/...`). Migrating the bot's own credential off `ROMM_CLIENT_TOKEN`.
