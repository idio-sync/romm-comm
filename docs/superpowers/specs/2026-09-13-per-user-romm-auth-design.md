# Per-User RomM Authentication — Design Spec

**Date:** 2026-09-13
**Status:** Approved (brainstorming) — ready for implementation plan
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
| Relationship to `user_links` | **Independent.** Pairing does not require a link and never rewrites one; disagreement is reported, not resolved. |
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
| `.../save-and-exit`, `.../save-state`, `.../load-state`, `.../mute`, `.../volume` | `roms.user.write` | **user token** |
| `GET /api/users/me` | `me.read` | **user token** (identity + liveness) |

This yields the governing invariant:

> **Reads stay on the bot token. Only the acting write carries a user token.**

`me.read` earns its place for one reason: `DeviceAuthTokenResponse` does not say *who* approved. Without `GET /api/users/me` the bot would hold a credential it cannot attribute. It doubles as the revalidation probe.

Blast radius of one stolen row: start and stop emulator sessions, and read that user's own profile. Not ROM deletion, not password change, not other users' data.

**Residual risk, accepted:** `roms.user.write` also authorizes `DELETE /api/streaming/sessions`, which ends *every* session. That user can already do this from RomM's web UI, so it is not a privilege escalation — but it belongs in the README rather than being discovered later.

## Architecture

```
romm_client.py                 # + identity seam, + ActingClient
romm_tokens/
  __init__.py                  # TokenStore facade, Grant dataclass
  device_flow.py               # init / poll / URL construction. HTTP only, takes Config.
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

Two places in `romm_client.py` build `Authorization: Bearer {self.access_token}`. Both become `await self._auth_header(grant)`, where `grant is None` means the bot. On top of that seam:

```python
client.acting_as(grant) -> ActingClient
```

`ActingClient` shares the session, connector and rate limiter, has the grant pre-bound, and exposes only a raw request method. Per-user code holds an object that *cannot* act as the bot; bot code changes in zero places. The alternative — an `identity=` kwarg threaded through every call site — was rejected because forgetting it fails silently as the bot, which is the precise bug this feature exists to eliminate.

**Hard invariant: the acting path never touches `APICache`.** `fetch_api_endpoint` keys the cache by endpoint alone, so a per-user response landing there would be served to a different user. `ActingClient` therefore has no cache-backed read method at all — the cache is unreachable from it by construction, not by discipline. A test pins this.

### Storage

```sql
CREATE TABLE IF NOT EXISTS romm_user_tokens (
    discord_id        INTEGER PRIMARY KEY,
    romm_user_id      INTEGER NOT NULL,    -- verified via GET /api/users/me
    romm_username     TEXT    NOT NULL,    -- cached for the audit view
    device_id         TEXT    NOT NULL,
    token_id          INTEGER,             -- from GET /api/client-tokens; enables revocation
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

Created in `_create_user_tables` for new installs, with a `migrate_romm_user_tokens` guard for existing databases, following the established `migrate_user_link_schema` pattern.

### Encryption

AES-256-GCM via `cryptography` (a new pinned runtime dependency). A fresh 12-byte nonce per write. **AAD is the `discord_id`**, so a row copied between users in the database file fails to open rather than handing user A's token to user B.

Key material comes from `ROMM_TOKEN_KEY_FILE` (preferred — a Docker secret or read-only mount, absent from `docker inspect`) or base64 `ROMM_TOKEN_KEY`, both read in `Config`. `key_fingerprint` is a truncated hash of the key, stored per row, so "wrong key" is distinguishable from "corrupt data".

**Rotation** needs the old key material, not just its fingerprint — a fingerprint identifies a key, it does not recover one. So rotation is: set `ROMM_TOKEN_KEY_OLD` alongside the new key, and on startup every row whose fingerprint matches the old key is opened with it and re-sealed under the new one, after which `ROMM_TOKEN_KEY_OLD` can be dropped. A row whose fingerprint matches no configured key is marked invalid and its user is asked to re-pair; rotating without `ROMM_TOKEN_KEY_OLD` is therefore a supported but lossy path, and the README says so.

**What this buys, stated honestly:** it protects a copied `data/` directory, a backup, a volume snapshot, an accidentally committed database. It does **not** protect against host compromise — anything that can read the process environment or memory has the key. The README says this rather than implying more.

**Fail closed.** With no key configured, the pairing feature refuses to load and logs an error; it never stores plaintext. A row that fails to open is treated as revoked — `invalid_since` is set and the user is prompted to re-pair — rather than raising.

### Configuration

All read in `bot.py`'s `Config`, per house style. No `os.getenv` outside it.

| Variable | Default | Purpose |
|----------|---------|---------|
| `ROMM_USER_AUTH_ENABLED` | `false` | Master switch for the pairing feature. |
| `ROMM_TOKEN_KEY_FILE` | — | Path to a file holding the 32-byte key. Preferred. |
| `ROMM_TOKEN_KEY` | — | Base64 32-byte key. Fallback. |
| `ROMM_PAIR_BASE_URL` | `DOMAIN`, else `API_BASE_URL` | Origin the relative `verification_path` is joined to. Falling back to `API_BASE_URL` logs a startup warning: an unreachable pairing URL is the most likely first-day failure. |
| `ROMM_PAIR_ROLE_ID` | `AUTO_REGISTER_ROLE_ID` | Gates who may run `/pair`. With both unset, any guild member may pair — the rate limits, not the role, are what stop abuse. |

`validate()` raises when `ROMM_USER_AUTH_ENABLED` is true and neither key variable is set.

## Flows

### `/pair`

1. **Gate.** Feature enabled, key present, role check, **one in-flight pairing per Discord user and a global concurrency cap**. `device/init` is unauthenticated, so an ungated `/pair` turns the bot into a spam relay against the RomM instance.
2. **Already paired?** Report the existing pairing (RomM username, scopes, expiry) and offer a re-pair button rather than silently minting a second credential.
3. **`POST /api/auth/device/init`.**
   - `client_device_identifier` — `romm-comm:<discord_id>`. Stable per Discord user, so re-pairing reuses one RomM device row instead of accumulating them. It puts the Discord ID into RomM's device table, which is deliberate: it is not secret, it makes the admin audit view joinable, and the RomM instance is already trusted with this user's library.
   - `name` — **bot-composed, never user-supplied**: `romm-comm · @<sanitized display name> · <4-char code>`. Display names are attacker-controlled and this string is rendered on RomM's approve screen, so it is stripped of markdown and non-printable characters and truncated, falling back to the Discord ID if it sanitizes to empty.
   - `client` `"romm-comm"`, `platform` `"discord"`, `client_version` from a module constant.
   - `requested_scopes` `["me.read", "roms.user.write"]`.
4. **DM the user**: the URL (`ROMM_PAIR_BASE_URL` + `verification_path_complete`), the `user_code` as a text fallback, a QR of the URL, and the 4-char code with *"approve only a request showing this code."* The interaction reply is ephemeral and says to check DMs; a blocked DM falls back to an ephemeral message carrying the same content, reported through an outcome enum in the style of `InviteOutcome`.
   - **`device_code` never appears in Discord and is never persisted.** It alone bears the grant. It lives in one in-memory dict for the life of the attempt. A second `/pair` or an `/unpair` cancels the in-flight poll task.
5. **Poll `POST /api/auth/device/token`** at the returned `interval`, giving up at `expires_in`, backing off if the server signals it.
6. **Three checks before storing anything.**
   - `GET /api/users/me` on the new token → `romm_user_id`, `romm_username`. Without this the bot holds a credential it cannot attribute.
   - `GET /api/client-tokens` → `token_id`, matched by `device_id`. Recorded so revocation and audit are possible later.
   - Granted `scopes` vs requested. **If `roms.user.write` was not granted, discard the token** and tell the user which scope to approve. Storing a credential that cannot do its job is worse than storing none.
7. **Drift check.** If `romm_user_id` disagrees with `user_links`, store the pairing anyway, flag it in the admin audit view, and log a warning. Neither record is rewritten.
8. **Seal and store**, then edit the DM to confirm.

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

That last distinction matters more than it looks: marking every token dead during a RomM restart would be the worst bug this loop could have. The existing `RommApiError` / `RommAuthError` split already encodes it.

Expiry warnings are DM'd once at 7 days and once at 1 day. `expiry_warned_at` records when the last warning was sent, and a threshold fires only when it is crossed *and* `expiry_warned_at` predates that crossing — so a restart mid-window does not re-send, and the 1-day warning still fires after the 7-day one. Non-expiring tokens are revalidated but never warned.

### Departure and role loss

`on_member_remove` deletes the row and attempts the admin revoke, logging to `CHANNEL_ID`. A departed member's live credential sitting in the bot's database is the case where leaving one behind is worst. Loss of `ROMM_PAIR_ROLE_ID` is treated as an unpair, reusing `user_manager`'s `handle_role_removal` path.

## The first consumer: streaming session queue

Not built here. The contract it needs:

```python
store.get_grant(discord_id) -> Grant | None      # token, romm_user_id, scopes, expires_at
```

`None` covers absent, expired, invalid and undecryptable, and is an **ordinary outcome**. A queue turn arriving for someone whose grant has lapsed skips them, DMs a re-pair prompt, and advances — it is not an error path.

`integrations/romm_streaming.py` changes narrowly: `_send` grows a `grant` parameter and routes through `self.romm.acting_as(grant)`. `get_config` and `list_sessions` keep passing `None` and stay on the bot token. `claim` and the in-session controls take the acting user's grant. Polite reclaim (`save_and_exit`) uses the owner's grant; `force_release_all` stays on the bot token as an admin action.

A queue that must claim a session minutes after the user typed anything is exactly why storage is unavoidable — a design that only held credentials during a single command could not serve it.

## Testing

New tests, `tests/`, pytest:

- **`test_romm_tokens_crypto.py`** — seal/open round-trip; a wrong key fails; **a row moved between `discord_id`s fails the AAD check**; the fingerprint is recorded and drives re-seal.
- **`test_device_flow.py`** — `init` payload shape and scope list; the URL is built from `ROMM_PAIR_BASE_URL`, not `API_BASE_URL`; the poll honours `interval` and gives up at `expires_in`; `device_code` appears in nothing returned or logged.
- **`test_romm_tokens_store.py`** — a granted-scope shortfall discards the token; identity drift is stored and flagged; an invalid row makes `get_grant` return `None`; the audit row has no `sealed` or `key_fingerprint` key.
- **`test_romm_client_identity.py`** — bot-path headers are byte-identical to today (regression); the acting path uses the grant's token; `ActingClient` exposes no cache-backed method and the acting path writes nothing to `APICache`.

Extensions to the structural suite:

- `test_sql_boundaries.py` — add `romm_tokens/repo.py` to `REPOSITORIES`.
- `test_import_boundaries.py` — `integrations/` and `romm_tokens/` never import from `cogs/`; no module outside `romm_tokens/` names the `sealed` column.
- `test_extension_loading.py` — covers `cogs/pair`.

## Targeted refactor

`cogs/search.py:386`'s `generate_qr` hardcodes the attachment filename `download_qr.png`. Lift it to a root `qr.py` with the filename as a parameter; `search.py` calls it. Small, and the pairing DM is its second consumer.

## To verify against the live instance before implementing

All five are undeclared in the OpenAPI spec, so they cannot be settled by reading it.

1. **What `POST /api/auth/device/token` returns while pending, on denial, and after expiry.** The spec declares only 200 and 422. This is the polling loop's entire control flow.
2. **That `GET /api/client-tokens` lists the just-minted token with a matching `device_id`**, so `token_id` can be recorded. Without it, revocation and reconciliation both lose their handle.
3. **Whether `GET /api/streaming/sessions` identifies the holding user.** Its response schema is `{}`. The queue needs it to map a session back to a Discord member.
4. **That RomM's approve screen renders the `name` field as sent**, or the 4-char confirmation code buys nothing.
5. **The RomM UI path to a user's own token list**, for the `/unpair` instructions.

## Out of scope

The streaming session queue cog itself. `/api/devices` management beyond the `device_id` the grant returns. Sync (`/api/sync/devices/...`). Migrating the bot's own credential off `ROMM_CLIENT_TOKEN`.
