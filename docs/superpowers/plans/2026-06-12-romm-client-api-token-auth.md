# RomM Client API Token Auth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the bot authenticate to RomM with a static Client API Token (`ROMM_CLIENT_TOKEN`) instead of a username/password, while keeping password-grant OAuth as a fallback.

**Architecture:** RomM 4.8+ client tokens are sent in the *same* header the bot already uses for OAuth access tokens — `Authorization: Bearer <token>` — and are distinguished server-side only by their `rmm_` prefix ([`hybrid_auth.py`](https://github.com/rommapp/romm/blob/4.9.0/backend/handler/auth/hybrid_auth.py)). Because the bot already injects `Bearer {self.access_token}` into every request, the change is concentrated in the token-acquisition layer: when a client token is configured, short-circuit the OAuth grant/refresh machinery and use the token directly. Username/password remains the fallback when no client token is set.

**Tech Stack:** Python 3, py-cord, aiohttp, python-socketio; stdlib `unittest` for tests (no pytest dependency).

**Key reference facts (verified against the running codebase):**
- Config is built in `bot.py` `Config.__init__` (bot.py:233) and validated in `Config.validate` (bot.py:277).
- Token state lives on `RommBot`: `self.access_token`, `self.refresh_token`, `self.token_expiry`, `self.token_lock` (bot.py:327-330).
- `ensure_valid_token` (bot.py:448) is the single gate every API call passes through; `make_authenticated_request` (bot.py:505) and `fetch_api_endpoint` (bot.py:940) both build `Authorization: Bearer {self.access_token}`.
- Startup bootstraps the token in `setup_hook` at bot.py:736 by calling `get_oauth_token()` directly.
- `refresh_token_task` (bot.py:921) re-validates every 10 minutes.
- The Socket.IO connection sends a Basic-auth header built from USER/PASS (bot.py:106-113). The RomM Socket.IO server has **no connect-time auth** and the `scan`/`scan:stop` event handlers do not validate credentials, so this header is effectively cosmetic — switching it is low-risk but must be confirmed empirically.
- Tests live in `tests/test_bot_auth.py`; logger name is `romm_bot`; Config is tested with `patch.dict(os.environ, {...}, clear=True)`; bot methods are tested by calling the unbound method on a hand-rolled fake (e.g. `RommBot.make_authenticated_request(fake_bot, ...)`).

**Run tests** from the repo root (`c:\Users\Jake\Git\romm-comm\romm-comm`) using the project's venv interpreter:
`.venv\Scripts\python -m unittest tests.test_bot_auth -v`

> The repo's dependencies (`py-cord`, `aiohttp`, `python-socketio`) are installed only in `.venv`. The bare `python` on PATH lacks them, so every test errors with `ModuleNotFoundError: No module named 'discord'` (at `bot.py:1`) before any assertion runs. Use the `.venv\Scripts\python` interpreter shown in every `Run:` step below; activate the venv first if you prefer.

---

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| `bot.py` | Config parsing/validation + token lifecycle + Socket.IO auth | Modify |
| `tests/test_bot_auth.py` | Unit tests for config + token behavior | Modify |
| `README.md` | User-facing configuration docs | Modify |

No new files. All changes are additive and gated behind `ROMM_CLIENT_TOKEN`; with the variable unset, behavior is identical to today.

---

## Task 1: Config — parse `ROMM_CLIENT_TOKEN` and relax credential validation

**Files:**
- Modify: `bot.py:233-273` (`Config.__init__`)
- Modify: `bot.py:277-289` (`Config.validate`)
- Test: `tests/test_bot_auth.py` (add to `ConfigCredentialTests`)

- [ ] **Step 1: Write the failing tests**

Add these two methods to the `ConfigCredentialTests` class in `tests/test_bot_auth.py`:

```python
    def test_client_token_alone_satisfies_validation(self):
        bot_module = importlib.import_module("bot")

        with patch.dict(
            os.environ,
            {
                "TOKEN": "discord-token",
                "GUILD": "123",
                "API_URL": "https://romm.example",
                "ROMM_CLIENT_TOKEN": "rmm_clienttoken",
            },
            clear=True,
        ):
            config = bot_module.Config()

        self.assertEqual("rmm_clienttoken", config.ROMM_CLIENT_TOKEN)
        self.assertIsNone(config.USER)
        self.assertIsNone(config.PASS)

    def test_missing_all_romm_credentials_raises(self):
        bot_module = importlib.import_module("bot")

        with patch.dict(
            os.environ,
            {
                "TOKEN": "discord-token",
                "GUILD": "123",
                "API_URL": "https://romm.example",
            },
            clear=True,
        ):
            with self.assertRaises(ValueError) as exc:
                bot_module.Config()

        self.assertIn("ROMM_USER", str(exc.exception))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python -m unittest tests.test_bot_auth -v`
Expected: `test_client_token_alone_satisfies_validation` FAILS with `AttributeError: 'Config' object has no attribute 'ROMM_CLIENT_TOKEN'` (and likely a `ValueError` raised during construction because USER/PASS are absent). `test_missing_all_romm_credentials_raises` passes already (it documents current behavior).

- [ ] **Step 3: Add `ROMM_CLIENT_TOKEN` parsing in `Config.__init__`**

In `bot.py`, immediately after the credentials fallback block (after the line `logger.warning("PASS is deprecated for RomM credentials; use ROMM_PASS instead")` at bot.py:260, i.e. before `self.REQUESTS_ENABLED = ...`), insert:

```python
        # RomM client API token (preferred over USER/PASS when set).
        # Create one in the RomM web UI (user profile -> API tokens) or via
        # POST /api/client-tokens. Sent as `Authorization: Bearer <token>`.
        self.ROMM_CLIENT_TOKEN = os.getenv('ROMM_CLIENT_TOKEN')
```

- [ ] **Step 4: Make username/password conditional in `Config.validate`**

In `bot.py`, replace the `required` dict construction at the top of `validate` (bot.py:279-285):

```python
        required = {
            'TOKEN': self.TOKEN,
            'GUILD': self.GUILD_ID,
            'API_URL': self.API_BASE_URL,
            'ROMM_USER': self.USER,
            'ROMM_PASS': self.PASS,
        }
```

with:

```python
        required = {
            'TOKEN': self.TOKEN,
            'GUILD': self.GUILD_ID,
            'API_URL': self.API_BASE_URL,
        }
        # RomM API auth: a client token replaces username/password.
        # When no client token is set, fall back to requiring ROMM_USER/ROMM_PASS.
        if not self.ROMM_CLIENT_TOKEN:
            required['ROMM_USER'] = self.USER
            required['ROMM_PASS'] = self.PASS
```

Leave the rest of `validate` (the `missing` check and the GUILD_ID/CHANNEL_ID integer conversion) untouched. This preserves the exact `"Missing required environment variables: ROMM_USER"` message that the existing `test_generic_user_environment_does_not_satisfy_missing_romm_user` asserts on.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv\Scripts\python -m unittest tests.test_bot_auth -v`
Expected: ALL tests PASS, including the three pre-existing `ConfigCredentialTests` (legacy USER/PASS, ROMM_ precedence, missing ROMM_USER) and the two new ones.

- [ ] **Step 6: Commit**

```bash
git add bot.py tests/test_bot_auth.py
git commit -m "feat(auth): accept ROMM_CLIENT_TOKEN config and relax credential validation"
```

---

## Task 2: Short-circuit `ensure_valid_token` for client tokens

**Files:**
- Modify: `bot.py:448-461` (`RommBot.ensure_valid_token`)
- Test: `tests/test_bot_auth.py` (add a test + imports)

- [ ] **Step 1: Add imports to the test file**

At the top of `tests/test_bot_auth.py`, add `import asyncio` and `import types` alongside the existing imports so the import block reads:

```python
import asyncio
import importlib
import os
import types
import unittest
from unittest.mock import patch
```

- [ ] **Step 2: Write the failing test**

Add this method to the `BotAuthTests` class in `tests/test_bot_auth.py`:

```python
    async def test_ensure_valid_token_uses_client_token_without_oauth(self):
        bot_module = importlib.import_module("bot")

        class ClientTokenConfig:
            ROMM_CLIENT_TOKEN = "rmm_clienttoken"

        fake = types.SimpleNamespace(
            config=ClientTokenConfig(),
            access_token=None,
            token_lock=asyncio.Lock(),
        )

        result = await bot_module.RommBot.ensure_valid_token(fake)

        self.assertTrue(result)
        self.assertEqual("rmm_clienttoken", fake.access_token)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv\Scripts\python -m unittest tests.test_bot_auth -v`
Expected: FAIL — without the short-circuit, `ensure_valid_token` falls through to the OAuth path and calls `self.get_oauth_token()`, which the `SimpleNamespace` does not define (`AttributeError`), so `fake.access_token` is never set to the client token.

- [ ] **Step 4: Add the short-circuit in `ensure_valid_token`**

In `bot.py`, insert the client-token branch as the first statement inside the `async with self.token_lock:` block (bot.py:450), before the `# Check if token is expired or missing` comment:

```python
    async def ensure_valid_token(self) -> bool:
        """Ensure we have a valid OAuth token, refreshing if necessary."""
        async with self.token_lock:
            # Client API token is a static credential: no OAuth grant or refresh needed.
            if self.config.ROMM_CLIENT_TOKEN:
                self.access_token = self.config.ROMM_CLIENT_TOKEN
                return True
            # Check if token is expired or missing
            if not self.access_token or time.time() >= self.token_expiry:
```

(Leave everything from `if not self.access_token` onward exactly as it is.)

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv\Scripts\python -m unittest tests.test_bot_auth -v`
Expected: ALL tests PASS.

- [ ] **Step 6: Commit**

```bash
git add bot.py tests/test_bot_auth.py
git commit -m "feat(auth): use client token directly in ensure_valid_token"
```

---

## Task 3: Bootstrap the startup token through `ensure_valid_token`

**Files:**
- Modify: `bot.py:736` (`setup_hook`)

This is a one-line change with no isolated unit test (it lives inside `setup_hook`, which requires a full Discord bot). Its behavior is exercised by Task 2's logic and confirmed in Task 7's manual run. Routing startup through `ensure_valid_token` makes the client-token path apply at boot, and is a no-op for the username/password path (with no access token and no refresh token, `ensure_valid_token` already calls `get_oauth_token`).

- [ ] **Step 1: Replace the startup token call**

In `bot.py`, replace (bot.py:736):

```python
            if not await self.get_oauth_token():
                logger.warning("Failed to obtain OAuth tokens, some features may not work")
            else:
                logger.info("✅ OAuth tokens initialized successfully")
```

with:

```python
            if not await self.ensure_valid_token():
                logger.warning("Failed to obtain RomM API token, some features may not work")
            else:
                logger.info("✅ RomM API token initialized successfully")
```

- [ ] **Step 2: Verify the module still imports and existing tests pass**

Run: `.venv\Scripts\python -m unittest tests.test_bot_auth -v`
Expected: ALL tests PASS (this confirms no syntax/typo regression; `test_bot_module_imports_json_for_json_decode_handlers` imports the module).

- [ ] **Step 3: Commit**

```bash
git add bot.py
git commit -m "refactor(auth): bootstrap startup token via ensure_valid_token"
```

---

## Task 4: Guard the 401 retry and the refresh loop against client tokens

A client token cannot be refreshed, and `refresh_oauth_token` falls back to a password grant — which would fail (and is meaningless) when only a client token is configured. So a 401 under a client token should fail fast with a clear log, and the periodic refresh loop should be skipped.

> Only `make_authenticated_request` needs the 401 guard. The read path, `fetch_api_endpoint` (bot.py:940+), returns `None` on a 401/403 without attempting any refresh, so it is already safe for client tokens — no change required there.

**Files:**
- Modify: `tests/test_bot_auth.py` (add `ROMM_CLIENT_TOKEN = None` to `FakeConfig`; add a test)
- Modify: `bot.py:562-569` (401 handling in `make_authenticated_request`)
- Modify: `bot.py:921-925` (`refresh_token_task`)

- [ ] **Step 1: Add `ROMM_CLIENT_TOKEN` to the shared test `FakeConfig`**

In `tests/test_bot_auth.py`, update the `FakeConfig` class (currently only `API_BASE_URL`):

```python
class FakeConfig:
    API_BASE_URL = "https://romm.example"
    ROMM_CLIENT_TOKEN = None
```

This keeps the existing `test_authenticated_request_refreshes_token_immediately_after_401` working: with `ROMM_CLIENT_TOKEN = None` the 401 path still refreshes exactly as before.

- [ ] **Step 2: Write the failing test**

Add this method to the `BotAuthTests` class in `tests/test_bot_auth.py`:

```python
    async def test_authenticated_request_does_not_refresh_client_token_on_401(self):
        bot_module = importlib.import_module("bot")
        fake_bot = FakeBot()
        fake_bot.config.ROMM_CLIENT_TOKEN = "rmm_clienttoken"
        fake_bot.session = FakeSession([FakeResponse(401)])

        with self.assertLogs("romm_bot", level="ERROR"):
            result = await bot_module.RommBot.make_authenticated_request(
                fake_bot,
                "GET",
                "roms",
            )

        self.assertIsNone(result)
        self.assertEqual(0, fake_bot.refresh_calls)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv\Scripts\python -m unittest tests.test_bot_auth -v`
Expected: FAIL. Without the guard, the 401 triggers `refresh_oauth_token` (so `fake_bot.refresh_calls` becomes `1`), then the retry tries to pop a second response from the single-element `FakeSession` and raises `IndexError`, which the method's outer `except Exception` (bot.py:573) catches and returns `None`. The test fails on `self.assertEqual(0, fake_bot.refresh_calls)` — it is `1`, not `0`.

- [ ] **Step 4: Guard the 401 handler**

In `bot.py`, replace the 401 block (bot.py:562-569):

```python
                if response.status == 401:
                    logger.debug("Got 401, attempting to refresh token")
                    if await self.refresh_oauth_token():
                        headers["Authorization"] = f"Bearer {self.access_token}"
                        async with session.request(method, url, **request_kwargs) as retry_response:
                            return await handle_response(retry_response)
                    else:
                        return None
```

with:

```python
                if response.status == 401:
                    # A client API token can't be refreshed; a 401 means it is invalid/revoked.
                    if self.config.ROMM_CLIENT_TOKEN:
                        logger.error(
                            "Got 401 using ROMM_CLIENT_TOKEN - the client token may be "
                            "invalid, expired, or revoked. Verify it in RomM."
                        )
                        return None
                    logger.debug("Got 401, attempting to refresh token")
                    if await self.refresh_oauth_token():
                        headers["Authorization"] = f"Bearer {self.access_token}"
                        async with session.request(method, url, **request_kwargs) as retry_response:
                            return await handle_response(retry_response)
                    else:
                        return None
```

- [ ] **Step 5: Skip the periodic refresh loop for client tokens**

In `bot.py`, replace `refresh_token_task` (bot.py:921-925):

```python
    @tasks.loop(minutes=10)
    async def refresh_token_task(self):
        """Periodically refresh the OAuth token to keep it valid."""
        if self.access_token:
            await self.ensure_valid_token()
```

with:

```python
    @tasks.loop(minutes=10)
    async def refresh_token_task(self):
        """Periodically refresh the OAuth token to keep it valid."""
        # Client API tokens are static and never need refreshing.
        if self.config.ROMM_CLIENT_TOKEN:
            return
        if self.access_token:
            await self.ensure_valid_token()
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv\Scripts\python -m unittest tests.test_bot_auth -v`
Expected: ALL tests PASS, including the existing 401-refresh test (still refreshes when `ROMM_CLIENT_TOKEN` is `None`) and the new client-token 401 test (no refresh, returns `None`).

- [ ] **Step 7: Commit**

```bash
git add bot.py tests/test_bot_auth.py
git commit -m "feat(auth): fail fast on 401 and skip refresh loop for client tokens"
```

---

## Task 5: Use a Bearer client token for the Socket.IO connection

**Files:**
- Modify: `bot.py:105-113` (`SocketIOManager.connect`)

The Socket.IO connection header is built from USER/PASS today. When a client token is configured we should send it instead, so a deployment can run with *only* `ROMM_CLIENT_TOKEN`. This has no isolated unit test (it opens a real websocket); correctness is confirmed empirically in Task 7. The change is low-risk because the RomM Socket.IO server does not validate credentials at connect time, but we still send a valid Bearer token for forward-compatibility.

- [ ] **Step 1: Branch the auth header on the client token**

In `bot.py`, replace the header-construction block inside `SocketIOManager.connect` (bot.py:106-113):

```python
                    auth_string = f"{self.config.USER}:{self.config.PASS}"
                    auth_bytes = auth_string.encode('ascii')
                    base64_auth = base64.b64encode(auth_bytes).decode('ascii')
                    
                    headers = {
                        'Authorization': f'Basic {base64_auth}',
                        'User-Agent': 'RommBot/1.0'
                    }
```

with:

```python
                    if self.config.ROMM_CLIENT_TOKEN:
                        # Client API tokens authenticate via Bearer, same as the HTTP API.
                        headers = {
                            'Authorization': f'Bearer {self.config.ROMM_CLIENT_TOKEN}',
                            'User-Agent': 'RommBot/1.0'
                        }
                    else:
                        auth_string = f"{self.config.USER}:{self.config.PASS}"
                        auth_bytes = auth_string.encode('ascii')
                        base64_auth = base64.b64encode(auth_bytes).decode('ascii')
                        headers = {
                            'Authorization': f'Basic {base64_auth}',
                            'User-Agent': 'RommBot/1.0'
                        }
```

- [ ] **Step 2: Verify existing tests still pass (no regression)**

Run: `.venv\Scripts\python -m unittest tests.test_bot_auth -v`
Expected: ALL tests PASS. (The Socket.IO path has no unit coverage; this step just confirms the module still imports cleanly.)

- [ ] **Step 3: Commit**

```bash
git add bot.py
git commit -m "feat(auth): send Bearer client token on the Socket.IO connection"
```

---

## Task 6: Document `ROMM_CLIENT_TOKEN` in the README

**Files:**
- Modify: `README.md:115-120` (`.env` Required block)
- Modify: `README.md:147` (Required config details)

- [ ] **Step 1: Update the `.env` Required example**

In `README.md`, replace the Required block of the `.env` example (README.md:115-120):

```env
# Required
TOKEN=your_discord_bot_token
GUILD=your_guild_id
API_URL=http://your_romm_host:port
ROMM_USER=api_username
ROMM_PASS=api_password
```

with:

```env
# Required: Discord + RomM connection
TOKEN=your_discord_bot_token
GUILD=your_guild_id
API_URL=http://your_romm_host:port

# Required: RomM API auth — use EITHER a client token (preferred)...
ROMM_CLIENT_TOKEN=rmm_your_client_token
# ...OR a username/password:
#ROMM_USER=api_username
#ROMM_PASS=api_password
```

- [ ] **Step 2: Update the "Required" config details list**

In `README.md`, replace the `ROMM_USER` / `ROMM_PASS` bullet (README.md:147):

```markdown
- `ROMM_USER` / `ROMM_PASS` — API credentials for RomM. Legacy `USER` / `PASS` still work, but `USER` can collide with the operating system username.
```

with:

```markdown
- **RomM API auth (choose one):**
  - `ROMM_CLIENT_TOKEN` — *(preferred, RomM 4.8+)* A Client API Token so the bot connects without storing a user's password. Create one in the RomM web UI under your user profile → **API Tokens** (or `POST /api/client-tokens`), granting the scopes `roms.read platforms.read firmware.read users.read users.write me.write`. It must be created by an admin user, since scopes are capped by the creating user's permissions (user-manager commands need `users.write`). When set, `ROMM_USER`/`ROMM_PASS` are not required.
  - `ROMM_USER` / `ROMM_PASS` — Username/password fallback used when no client token is set. Legacy `USER` / `PASS` still work, but `USER` can collide with the operating system username.
```

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: document ROMM_CLIENT_TOKEN auth option"
```

---

## Task 7: Manual integration verification (requires a running RomM 4.8+ instance)

No automated test can cover the live wire protocol, CSRF behavior, or the Socket.IO handshake. Run the bot against a real RomM instance configured with **only** `ROMM_CLIENT_TOKEN` (comment out `ROMM_USER`/`ROMM_PASS`) and confirm each item. This is where the two open questions (CSRF on mutations, Socket.IO Bearer) get resolved.

- [ ] **Step 1: Create a client token in RomM** — In the RomM web UI, create an API token (as an admin user) with scopes `roms.read platforms.read firmware.read users.read users.write me.write`. Copy the `rmm_...` value into `.env` as `ROMM_CLIENT_TOKEN`, and comment out `ROMM_USER`/`ROMM_PASS`.

- [ ] **Step 2: Startup** — Start the bot. Confirm the log shows `✅ RomM API token initialized successfully` and no `401`/credential errors.

- [ ] **Step 3: Read path** — Run `/search` (or the stats/info command). Confirm results return AND the cover art renders in the embed (verifies authenticated HTTP + the anonymous cover fetch still work).

- [ ] **Step 4: Mutation + CSRF path** — Run a user-manager command that writes (e.g. create or disable a throwaway RomM user). Confirm it succeeds. **If it fails with a CSRF/403 error**, that's the flagged CSRF-under-bearer case: note it and open a follow-up to conditionally skip `require_csrf` when `ROMM_CLIENT_TOKEN` is set. (Expected outcome: it works, because CSRF applies to cookie/session auth, not bearer tokens.)

- [ ] **Step 5: Socket.IO path** — Trigger a scan from Discord (`/scan`) or start one in RomM. Confirm the bot receives scan progress and `scan:done` events (verifies the Bearer header on the websocket connection from Task 5). **If the socket fails to connect or receive events**, fall back to also setting `ROMM_USER`/`ROMM_PASS` and note it for follow-up.

- [ ] **Step 6: Regression — password mode still works** — Swap back to `ROMM_USER`/`ROMM_PASS` (unset `ROMM_CLIENT_TOKEN`), restart, and confirm startup + `/search` still work. This proves the fallback path is intact.

- [ ] **Step 7: Record results** — Note the outcomes of Steps 4 and 5 in the PR description, since they resolve the previously open questions.

---

## Self-Review Notes

- **Spec coverage:** Config (Task 1), token bootstrap at startup (Tasks 2-3), runtime 401/refresh handling (Task 4), Socket.IO (Task 5), docs (Task 6), and empirical verification of the two open questions — CSRF and Socket.IO (Task 7). All covered.
- **Backward compatibility:** Every change is gated on `self.config.ROMM_CLIENT_TOKEN`. With it unset, `validate` still requires ROMM_USER/ROMM_PASS, `ensure_valid_token` runs the OAuth path, the 401 handler refreshes, the refresh loop runs, and the Socket.IO header stays Basic — identical to today. The three pre-existing `ConfigCredentialTests` and the existing 401-refresh test are preserved unchanged.
- **Naming consistency:** `ROMM_CLIENT_TOKEN` is the single config attribute name used in `Config`, `ensure_valid_token`, `make_authenticated_request`, `refresh_token_task`, `SocketIOManager.connect`, the test `FakeConfig`, and the README.
- **Token-type prefix:** Server-side detection keys on the `rmm_` prefix; the bot does not need to check the prefix (it just sends whatever token is configured), so no prefix validation is added.
