# Per-User RomM Authentication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a Discord user pair their own RomM account with the bot, so the bot can act *as that user* for the narrow set of RomM calls that bind a resource to its owner.

**Architecture:** A root package `romm_tokens/` owns credential custody — the device-grant HTTP flow, AES-256-GCM sealing, the SQL, and a store that is the only place a bearer token is produced. `RommClient` grows one seam (`acting_as(grant) -> ActingClient`) so existing callers are untouched, and `cogs/pair/` is the Discord surface. Reads keep using the bot's token; only the acting write carries a user token.

**Tech Stack:** Python 3.12, py-cord 2.6.1, aiohttp 3.12.15, aiosqlite 0.21.0, `cryptography` (new), qrcode 8.2, pytest 9.1.1, ruff 0.16.7.

**Spec:** [`docs/superpowers/specs/2026-09-13-per-user-romm-auth-design.md`](../specs/2026-09-13-per-user-romm-auth-design.md)

## Global Constraints

- **Every env var is read in `bot.py`'s `Config` class.** No `os.getenv` anywhere else. This is a hard house rule.
- **Per-user scopes are exactly `["me.read", "roms.user.write"]`.** Never request more.
- **Reads stay on the bot token; only the acting write carries a user token.**
- **Credential material never crosses five lines:** never into `APICache`; never into a log record; never into an embed or Discord message; `device_code` is never persisted; a `Grant` is fetched per action and never held in view state, cog attributes, or a queue entry.
- **All four `device/token` outcomes are HTTP 400.** Branch on the `detail` string: `authorization_pending`, `slow_down`, `access_denied`, `expired_token`. Never on the status code.
- **`slow_down` is on the ordinary path** — observed at poll 57 of a 120-poll window. Honouring it is required, not defensive.
- **A 401/403 on the acting path is terminal.** No refresh, no replay. `romm_client.py:358` is a 401 retry that refreshes the *bot's* token and replays — it must not be reachable from the acting path.
- **Every deferred write keyed by `discord_id` carries `AND generation = ?`.** Zero rows affected is a normal outcome, logged at debug.
- Test style: `unittest.TestCase` classes run under pytest. Build subjects with `object.__new__(Cls)` and set attributes, as `tests/test_igdb_token.py` does. Config is tested with `patch.dict(os.environ, {...}, clear=True)`.
- Run tests with `python -m pytest <path> -v` from the repo root.
- ruff: aim for 110 columns, hard wall 180, `max-complexity = 15`.

---

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| `bot.py` | Config parsing/validation; wire `TokenStore`; register `cogs.pair` | Modify |
| `romm_client.py` | Auth-header seam, `ActingClient`, bot scope string | Modify |
| `database_manager.py` | `romm_user_tokens` DDL; `get_user_link_strict` | Modify |
| `romm_tokens/__init__.py` | Public surface: `Grant`, `TokenStore`, errors | Create |
| `romm_tokens/crypto.py` | Key loading, fingerprint, seal/open with AAD | Create |
| `romm_tokens/repo.py` | All SQL against `romm_user_tokens` | Create |
| `romm_tokens/device_flow.py` | `device/init`, URL construction, the poll loop | Create |
| `romm_tokens/store.py` | Orchestration; the only producer of a bearer token | Create |
| `qr.py` | QR generation, lifted from `cogs/search.py` | Create |
| `cogs/search.py` | Call `qr.py` instead of its own generator | Modify |
| `cogs/pair/cog.py` | `/pair`, `/unpair`, `/pair-status`, `/pairings`, listeners, loop | Create |
| `cogs/pair/embeds.py` | Embed builders for the four commands | Create |
| `integrations/romm_streaming.py` | `grant` parameter; preserve `ClaimOutcome.DENIED` | Modify |
| `tests/…` | Nine new test modules; three structural tests widened | Create/Modify |

---

## Task 1: Config — the six new settings and the pairing origin

**Files:**
- Modify: `bot.py` (`Config.__init__`, `Config.validate`)
- Modify: `.env.example`
- Test: `tests/test_bot_auth.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `config.ROMM_USER_AUTH_ENABLED: bool`, `config.ROMM_TOKEN_KEY: str | None`, `config.ROMM_TOKEN_KEY_FILE: str | None`, `config.ROMM_TOKEN_KEY_OLD: str | None`, `config.ROMM_PAIR_BASE_URL: str`, `config.ROMM_PAIR_ROLE_ID: str | None`, `config.ROMM_PAIR_MAX_AGE_DAYS: int`.

- [ ] **Step 1: Write the failing tests**

Add this class to `tests/test_bot_auth.py`:

```python
class PairingConfigTests(unittest.TestCase):
    BASE = {
        "TOKEN": "discord-token",
        "GUILD": "123",
        "API_URL": "https://romm.example",
        "ROMM_CLIENT_TOKEN": "rmm_clienttoken",
    }

    def _config(self, **overrides):
        import importlib
        bot_module = importlib.import_module("bot")
        env = dict(self.BASE)
        env.update(overrides)
        with patch.dict(os.environ, env, clear=True):
            return bot_module.Config()

    def test_pairing_is_off_by_default(self):
        config = self._config()
        self.assertFalse(config.ROMM_USER_AUTH_ENABLED)

    def test_sentinel_domain_never_becomes_the_pairing_origin(self):
        # DOMAIN defaults to 'No website configured'. Joining a relative
        # verification path to that ships users a broken link.
        config = self._config(ROMM_USER_AUTH_ENABLED="true", ROMM_TOKEN_KEY="x" * 44)
        self.assertEqual("https://romm.example", config.ROMM_PAIR_BASE_URL)

    def test_real_domain_is_preferred_over_the_api_url(self):
        config = self._config(
            DOMAIN="https://roms.example.com/",
            ROMM_USER_AUTH_ENABLED="true",
            ROMM_TOKEN_KEY="x" * 44,
        )
        self.assertEqual("https://roms.example.com", config.ROMM_PAIR_BASE_URL)

    def test_explicit_pair_base_url_wins(self):
        config = self._config(
            DOMAIN="https://roms.example.com",
            ROMM_PAIR_BASE_URL="https://pair.example.com",
            ROMM_USER_AUTH_ENABLED="true",
            ROMM_TOKEN_KEY="x" * 44,
        )
        self.assertEqual("https://pair.example.com", config.ROMM_PAIR_BASE_URL)

    def test_missing_key_disables_the_feature_rather_than_raising(self):
        # An optional feature must degrade, not take the bot down.
        config = self._config(ROMM_USER_AUTH_ENABLED="true")
        self.assertFalse(config.ROMM_USER_AUTH_ENABLED)

    def test_max_age_defaults_to_ninety_days(self):
        config = self._config()
        self.assertEqual(90, config.ROMM_PAIR_MAX_AGE_DAYS)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_bot_auth.py::PairingConfigTests -v`
Expected: every test FAILS with `AttributeError: 'Config' object has no attribute 'ROMM_USER_AUTH_ENABLED'`.

- [ ] **Step 3: Add the settings to `Config.__init__`**

In `bot.py`, immediately before `self.validate()` at the end of `Config.__init__`, insert:

```python
        # Per-user RomM authentication. Off by default: it makes the bot
        # custodian of other people's credentials, which is an opt-in.
        self.ROMM_USER_AUTH_ENABLED = self.parse_bool(
            os.getenv('ROMM_USER_AUTH_ENABLED', 'false'), False
        )
        self.ROMM_TOKEN_KEY = os.getenv('ROMM_TOKEN_KEY') or None
        self.ROMM_TOKEN_KEY_FILE = os.getenv('ROMM_TOKEN_KEY_FILE') or None
        # Set alongside a new key to re-seal existing rows, then removed.
        self.ROMM_TOKEN_KEY_OLD = os.getenv('ROMM_TOKEN_KEY_OLD') or None

        self.ROMM_PAIR_ROLE_ID = os.getenv('ROMM_PAIR_ROLE_ID') or self.AUTO_REGISTER_ROLE_ID
        self.ROMM_PAIR_MAX_AGE_DAYS = int(os.getenv('ROMM_PAIR_MAX_AGE_DAYS', '90'))

        # The origin a relative verification_path is joined to. DOMAIN is not
        # usable as a fallback on its own: it defaults to the sentinel string
        # 'No website configured', so a naive `DOMAIN or API_URL` never falls
        # back and ships users a link beginning with that sentence.
        explicit_pair_url = (os.getenv('ROMM_PAIR_BASE_URL') or '').rstrip('/')
        if explicit_pair_url:
            self.ROMM_PAIR_BASE_URL = explicit_pair_url
        elif self.DOMAIN.startswith(('http://', 'https://')):
            self.ROMM_PAIR_BASE_URL = self.DOMAIN
        else:
            self.ROMM_PAIR_BASE_URL = self.API_BASE_URL
            if self.ROMM_USER_AUTH_ENABLED:
                logger.warning(
                    "No usable DOMAIN or ROMM_PAIR_BASE_URL; pairing links will use "
                    f"{self.API_BASE_URL}, which may be unreachable from a phone."
                )

        self._validate_pairing()
```

- [ ] **Step 4: Add the pairing validator**

In `bot.py`, add this method to `Config`, directly above `validate`:

```python
    def _validate_pairing(self):
        """Turn unusable pairing config into a disabled feature, not a dead bot.

        Config.validate raises only for values without which nothing works.
        Pairing is optional, so a missing key disables it the way missing
        IGDB credentials disable IGDB.
        """
        if not self.ROMM_USER_AUTH_ENABLED:
            return

        if self.ROMM_PAIR_BASE_URL and not self.ROMM_PAIR_BASE_URL.startswith(('http://', 'https://')):
            logger.error(
                f"ROMM_PAIR_BASE_URL is not an http(s) origin: {self.ROMM_PAIR_BASE_URL!r}. "
                "Disabling per-user RomM authentication."
            )
            self.ROMM_USER_AUTH_ENABLED = False
            return

        if not (self.ROMM_TOKEN_KEY or self.ROMM_TOKEN_KEY_FILE):
            logger.error(
                "ROMM_USER_AUTH_ENABLED is set but neither ROMM_TOKEN_KEY nor "
                "ROMM_TOKEN_KEY_FILE is. Refusing to store credentials unencrypted; "
                "disabling per-user RomM authentication."
            )
            self.ROMM_USER_AUTH_ENABLED = False
```

- [ ] **Step 5: Run the tests**

Run: `python -m pytest tests/test_bot_auth.py -v`
Expected: PASS, including the pre-existing tests in that file.

- [ ] **Step 6: Document the settings**

Append to `.env.example`:

```
ROMM_USER_AUTH_ENABLED=FALSE              # Let users pair their own RomM account with the bot (default: false)
ROMM_TOKEN_KEY_FILE=/run/secrets/romm_key # Path to a file holding a 32-byte key (preferred over ROMM_TOKEN_KEY)
ROMM_TOKEN_KEY=base64_32_byte_key         # Base64 32-byte key, used when no key file is set
ROMM_TOKEN_KEY_OLD=previous_key           # Set alongside a new key to re-seal stored credentials, then remove
ROMM_PAIR_BASE_URL=https://yourdomain.com # Origin users open to approve pairing (defaults to DOMAIN, then API_URL)
ROMM_PAIR_ROLE_ID=romm_users_role_id      # Role allowed to run /pair (defaults to AUTO_REGISTER_ROLE_ID)
ROMM_PAIR_MAX_AGE_DAYS=90                 # Force re-pairing after this many days; 0 disables (RomM tokens never expire)
```

- [ ] **Step 7: Commit**

```bash
git add bot.py .env.example tests/test_bot_auth.py
git commit -m "feat(pair): read the pairing settings, and guard the sentinel DOMAIN"
```

---

## Task 2: `romm_tokens/crypto.py` — sealing credentials at rest

**Files:**
- Create: `romm_tokens/__init__.py`, `romm_tokens/crypto.py`
- Modify: `requirements.txt`, `pyproject.toml`
- Test: `tests/test_romm_tokens_crypto.py`

**Interfaces:**
- Consumes: Task 1's `config.ROMM_TOKEN_KEY`, `config.ROMM_TOKEN_KEY_FILE`.
- Produces: `SealingKey(material: bytes, fingerprint: str)`; `load_key(raw: str | None, key_file: str | None) -> SealingKey` (raises `ValueError`); `seal(key, discord_id: int, token: str) -> bytes`; `unseal(key, discord_id: int, blob: bytes) -> str` (raises `CredentialUnsealError`); `CredentialUnsealError`.

- [ ] **Step 1: Add the dependency**

In `requirements.txt`, after the `aiosqlite` line:

```
cryptography==46.0.3
```

In `pyproject.toml`, extend the isort setting so the new package sorts as first-party:

```toml
known-first-party = ["cogs", "integrations", "admin_checks", "bot", "database_manager", "romm_tokens", "romm_client", "qr"]
```

Run: `pip install -r requirements-dev.txt`

- [ ] **Step 2: Write the failing tests**

Create `tests/test_romm_tokens_crypto.py`:

```python
"""The sealing layer, including the two ways it is supposed to refuse.

A round-trip test alone would pass for a construction with no integrity
protection and no binding to an owner, which is most of the value here.
"""

import base64
import os
import tempfile
import unittest

from romm_tokens.crypto import CredentialUnsealError, load_key, seal, unseal

KEY_A = base64.b64encode(b"A" * 32).decode()
KEY_B = base64.b64encode(b"B" * 32).decode()


class SealingTests(unittest.TestCase):
    def test_round_trip(self):
        key = load_key(KEY_A, None)
        blob = seal(key, 111, "rmm_secret")
        self.assertEqual("rmm_secret", unseal(key, 111, blob))

    def test_ciphertext_does_not_contain_the_token(self):
        key = load_key(KEY_A, None)
        blob = seal(key, 111, "rmm_secret")
        self.assertNotIn(b"rmm_secret", blob)

    def test_a_different_key_cannot_open_it(self):
        blob = seal(load_key(KEY_A, None), 111, "rmm_secret")
        with self.assertRaises(CredentialUnsealError):
            unseal(load_key(KEY_B, None), 111, blob)

    def test_a_row_moved_between_users_fails_the_aad_check(self):
        # The attack this binding exists for: copy user A's row onto user B's
        # discord_id in the database file and be handed A's credential.
        key = load_key(KEY_A, None)
        blob = seal(key, 111, "rmm_secret")
        with self.assertRaises(CredentialUnsealError):
            unseal(key, 222, blob)

    def test_nonce_is_fresh_per_write(self):
        key = load_key(KEY_A, None)
        self.assertNotEqual(seal(key, 111, "same"), seal(key, 111, "same"))

    def test_fingerprint_identifies_the_key_without_revealing_it(self):
        key = load_key(KEY_A, None)
        self.assertNotEqual(load_key(KEY_B, None).fingerprint, key.fingerprint)
        self.assertEqual(load_key(KEY_A, None).fingerprint, key.fingerprint)
        self.assertNotIn(base64.b64decode(KEY_A).hex(), key.fingerprint)


class KeyLoadingTests(unittest.TestCase):
    def test_key_file_is_preferred_over_the_env_value(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "key")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(KEY_B)
            self.assertEqual(load_key(KEY_B, None).fingerprint, load_key(KEY_A, path).fingerprint)

    def test_wrong_length_is_rejected_by_name(self):
        short = base64.b64encode(b"A" * 31).decode()
        with self.assertRaises(ValueError) as caught:
            load_key(short, None)
        self.assertIn("32 bytes", str(caught.exception))

    def test_unparseable_base64_is_rejected(self):
        with self.assertRaises(ValueError):
            load_key("not base64 !!", None)

    def test_no_key_material_at_all_is_rejected(self):
        with self.assertRaises(ValueError):
            load_key(None, None)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/test_romm_tokens_crypto.py -v`
Expected: collection error — `ModuleNotFoundError: No module named 'romm_tokens'`.

- [ ] **Step 4: Create the package marker**

Create `romm_tokens/__init__.py`:

```python
"""Custody of per-user RomM credentials.

A root package rather than a feature package under cogs/ because
integrations/romm_streaming.py needs to resolve a Discord id to a token.
Under cogs/ that would be an integration importing from a cog - the wrong
layering, and a boundary test forbids it. It cannot live in integrations/
either: load_integration_cogs treats every module there as a loadable
extension.

RommClient deliberately does not import this package. It keeps taking a
Config and nothing else, which is what makes it constructible in a test;
the store reaches it through a callback instead.
"""
```

- [ ] **Step 5: Write `romm_tokens/crypto.py`**

```python
"""Sealing stored credentials, and the two refusals that matter.

AES-256-GCM rather than a bare cipher because an attacker who can write to
the database file is exactly the one this defends against, and a cipher
without integrity protection would let them flip bits in a credential
rather than merely destroy it.

The additional authenticated data is the discord_id. Without it, a row
copied onto another user's id in the database file would open cleanly and
hand out the wrong person's credential - the database's own primary key is
not otherwise cryptographically attached to what it points at.

What this buys is a copied data/ directory, a backup, a volume snapshot, an
accidentally committed database. It does not defend against host
compromise: anything that can read the process environment has the key.
"""

import base64
import hashlib
import logging
import os
from dataclasses import dataclass, field

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger('romm_bot.tokens')

KEY_BYTES = 32
NONCE_BYTES = 12


class CredentialUnsealError(Exception):
    """A stored credential could not be opened.

    Raised for a wrong key, a tampered blob and a row moved between users
    alike - deliberately not distinguished, because the caller's response to
    all three is the same: treat the credential as revoked and ask the user
    to pair again.
    """


@dataclass(frozen=True)
class SealingKey:
    """Key material plus the fingerprint stored beside every row it seals."""

    material: bytes = field(repr=False)
    fingerprint: str


def _fingerprint(material: bytes) -> str:
    """A short, non-reversible label for a key.

    Stored per row so "sealed under a key we no longer have" is
    distinguishable from "corrupt", which decides whether rotation can
    recover the row or the user has to pair again.
    """
    return hashlib.sha256(b'romm-comm-key\x00' + material).hexdigest()[:16]


def load_key(raw: str | None, key_file: str | None) -> SealingKey:
    """Read key material from a file if given, else from the raw value.

    The file is preferred because a Docker secret or read-only mount does
    not show up in `docker inspect`, where an environment variable does.
    """
    material_b64 = None
    if key_file:
        try:
            with open(key_file, encoding='utf-8') as handle:
                material_b64 = handle.read().strip()
        except OSError as e:
            raise ValueError(f"Could not read ROMM_TOKEN_KEY_FILE {key_file!r}: {e}") from e
    elif raw:
        material_b64 = raw.strip()

    if not material_b64:
        raise ValueError("No key material: set ROMM_TOKEN_KEY_FILE or ROMM_TOKEN_KEY")

    try:
        material = base64.b64decode(material_b64, validate=True)
    except Exception as e:
        raise ValueError("Key material is not valid base64") from e

    if len(material) != KEY_BYTES:
        raise ValueError(
            f"Key material must decode to exactly {KEY_BYTES} bytes, got {len(material)}"
        )

    return SealingKey(material=material, fingerprint=_fingerprint(material))


def seal(key: SealingKey, discord_id: int, token: str) -> bytes:
    """Encrypt a credential, bound to the Discord id that owns it."""
    nonce = os.urandom(NONCE_BYTES)
    ciphertext = AESGCM(key.material).encrypt(
        nonce, token.encode('utf-8'), str(discord_id).encode('ascii')
    )
    return nonce + ciphertext


def unseal(key: SealingKey, discord_id: int, blob: bytes) -> str:
    """Decrypt a credential. Raises CredentialUnsealError for every failure."""
    if len(blob) <= NONCE_BYTES:
        raise CredentialUnsealError("Sealed value is too short to contain a nonce")
    try:
        plaintext = AESGCM(key.material).decrypt(
            blob[:NONCE_BYTES], blob[NONCE_BYTES:], str(discord_id).encode('ascii')
        )
    except InvalidTag as e:
        # Never log the blob or the key: this is reached on tampering.
        raise CredentialUnsealError(
            f"Could not open credential for {discord_id} under key {key.fingerprint}"
        ) from e
    return plaintext.decode('utf-8')
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/test_romm_tokens_crypto.py -v`
Expected: PASS (10 tests).

- [ ] **Step 7: Lint**

Run: `python -m ruff check romm_tokens/ tests/test_romm_tokens_crypto.py`
Expected: `All checks passed!`

- [ ] **Step 8: Commit**

```bash
git add romm_tokens/ tests/test_romm_tokens_crypto.py requirements.txt pyproject.toml
git commit -m "feat(pair): seal stored credentials, bound to the id that owns them"
```

---

## Task 3: Schema and `romm_tokens/repo.py`

**Files:**
- Modify: `database_manager.py` (`_create_user_tables`, `verify_tables_exist`)
- Create: `romm_tokens/repo.py`
- Modify: `tests/test_sql_boundaries.py`
- Test: `tests/test_romm_tokens_repo.py`

**Interfaces:**
- Consumes: `MasterDatabase.get_connection()`.
- Produces: `TokenRepo(db)` with `async upsert(discord_id, romm_user_id, romm_username, device_id, token_id, scopes: list[str], expires_at, key_fingerprint, sealed) -> int` (returns the new `generation`); `async get_sealed_row(discord_id) -> dict | None` (includes `sealed`); `async audit_row(discord_id) -> dict | None`; `async list_audit() -> list[dict]`; `async delete(discord_id) -> bool`; `async mark_invalid(discord_id, generation) -> bool`; `async touch_verified(discord_id, generation) -> bool`; `async touch_used(discord_id, generation) -> bool`; `async mark_expiry_warned(discord_id, generation) -> bool`; `async find_by_romm_user(romm_user_id) -> list[dict]`; `async reseal(*, discord_id, key_fingerprint, sealed) -> bool`. Every `mark_*`/`touch_*` returns whether a row was affected.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_romm_tokens_repo.py`:

```python
"""Data access for romm_user_tokens, including the generation guard.

The guard is the point of most of these tests: a background probe that
started before a re-pair must not be able to write over the credential that
replaced the one it was checking.
"""

import asyncio
import os
import tempfile
import unittest

from database_manager import MasterDatabase
from romm_tokens.repo import TokenRepo


def run(coro):
    return asyncio.run(coro)


class RepoTestCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = MasterDatabase(db_path=os.path.join(self._dir.name, "test.db"))
        run(self.db.initialize())
        self.repo = TokenRepo(self.db)

    def tearDown(self):
        self._dir.cleanup()

    def store(self, discord_id=111, romm_user_id=7, sealed=b"sealed-bytes"):
        return run(self.repo.upsert(
            discord_id=discord_id,
            romm_user_id=romm_user_id,
            romm_username="idiosync",
            device_id="dev-1",
            token_id=3,
            scopes=["me.read", "roms.user.write"],
            expires_at=None,
            key_fingerprint="fp0123456789abcd",
            sealed=sealed,
        ))


class StorageTests(RepoTestCase):
    def test_first_store_is_generation_one(self):
        self.assertEqual(1, self.store())

    def test_re_pairing_bumps_the_generation(self):
        self.store()
        self.assertEqual(2, self.store())

    def test_sealed_row_carries_the_blob(self):
        self.store(sealed=b"blob")
        row = run(self.repo.get_sealed_row(111))
        self.assertEqual(b"blob", row["sealed"])
        self.assertEqual(["me.read", "roms.user.write"], row["scopes"])

    def test_audit_row_cannot_carry_credential_material(self):
        # No view can render what it cannot fetch.
        self.store()
        row = run(self.repo.audit_row(111))
        self.assertNotIn("sealed", row)
        self.assertNotIn("key_fingerprint", row)
        self.assertEqual("idiosync", row["romm_username"])

    def test_delete_removes_the_row(self):
        self.store()
        self.assertTrue(run(self.repo.delete(111)))
        self.assertIsNone(run(self.repo.get_sealed_row(111)))

    def test_find_by_romm_user_spots_a_shared_account(self):
        self.store(discord_id=111, romm_user_id=7)
        self.store(discord_id=222, romm_user_id=7)
        self.assertEqual(2, len(run(self.repo.find_by_romm_user(7))))


class GenerationGuardTests(RepoTestCase):
    def test_marking_invalid_at_the_current_generation_works(self):
        generation = self.store()
        self.assertTrue(run(self.repo.mark_invalid(111, generation)))
        self.assertIsNotNone(run(self.repo.audit_row(111))["invalid_since"])

    def test_a_stale_probe_cannot_invalidate_a_replacement(self):
        stale = self.store()          # generation 1, the token being probed
        self.store()                  # generation 2, stored by a re-pair
        self.assertFalse(run(self.repo.mark_invalid(111, stale)))
        self.assertIsNone(run(self.repo.audit_row(111))["invalid_since"])

    def test_every_deferred_write_carries_the_guard(self):
        stale = self.store()
        self.store()
        self.assertFalse(run(self.repo.touch_verified(111, stale)))
        self.assertFalse(run(self.repo.touch_used(111, stale)))
        self.assertFalse(run(self.repo.mark_expiry_warned(111, stale)))

    def test_resealing_does_not_bump_the_generation(self):
        # Rotation changes the key, not the credential. Bumping here would
        # invalidate in-flight probes for no reason.
        generation = self.store()
        run(self.repo.reseal(discord_id=111, key_fingerprint="newfp0123456789", sealed=b"new"))

        row = run(self.repo.get_sealed_row(111))
        self.assertEqual(generation, row["generation"])
        self.assertEqual(b"new", row["sealed"])
        self.assertEqual("newfp0123456789", row["key_fingerprint"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_romm_tokens_repo.py -v`
Expected: collection error — `ModuleNotFoundError: No module named 'romm_tokens.repo'`.

- [ ] **Step 3: Add the table DDL**

In `database_manager.py`, inside `_create_user_tables`, after the `pending_invites` table creation and before the closing `logger.debug(...)`, insert:

```python
        logger.debug("Creating romm_user_tokens table...")

        # Credentials, not a mapping - which is why they are not columns on
        # user_links. get_all_user_links() splats rows into dicts that views
        # render directly, and ciphertext must never be reachable from a code
        # path that ends in an embed.
        await db.execute('''
            CREATE TABLE IF NOT EXISTS romm_user_tokens (
                discord_id        INTEGER PRIMARY KEY,
                romm_user_id      INTEGER NOT NULL,
                romm_username     TEXT    NOT NULL,
                device_id         TEXT    NOT NULL,
                token_id          INTEGER,
                generation        INTEGER NOT NULL DEFAULT 1,
                scopes            TEXT    NOT NULL,
                expires_at        TIMESTAMP,
                key_fingerprint   TEXT    NOT NULL,
                sealed            BLOB    NOT NULL,
                created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_used_at      TIMESTAMP,
                last_verified_at  TIMESTAMP,
                expiry_warned_at  TIMESTAMP,
                invalid_since     TIMESTAMP
            )
        ''')

        await db.execute('''
            CREATE INDEX IF NOT EXISTS idx_romm_user_tokens_romm_user
            ON romm_user_tokens(romm_user_id)
        ''')
```

In the same file, add `'romm_user_tokens'` to the list of table names in `verify_tables_exist`.

- [ ] **Step 4: Write `romm_tokens/repo.py`**

```python
"""Data access for romm_user_tokens.

Two row shapes, deliberately. audit_row and list_audit return everything a
human or a view may see; get_sealed_row is the only query that returns the
ciphertext, and only store.get_grant calls it. That is what keeps "exactly
one function in the tree can produce a bearer token" true at the data layer
rather than by convention.

Every write keyed by discord_id alone would be a race: a revalidation probe
that started before a re-pair would land on the credential that replaced
the one it was checking. So each carries AND generation = ?, and reports
whether it actually hit anything.
"""

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger('romm_bot.tokens')

# Everything a view or an admin may see. Deliberately excludes `sealed` and
# `key_fingerprint`: a caller cannot render what it was never handed.
AUDIT_COLUMNS = (
    "discord_id, romm_user_id, romm_username, device_id, token_id, generation, "
    "scopes, expires_at, created_at, last_used_at, last_verified_at, "
    "expiry_warned_at, invalid_since"
)


def _as_audit_dict(row) -> Dict[str, Any]:
    data = {key: row[key] for key in row.keys()}
    data['scopes'] = json.loads(data['scopes']) if data.get('scopes') else []
    return data


class TokenRepo:
    """Queries for per-user RomM credentials, over a MasterDatabase."""

    def __init__(self, db):
        self.db = db

    async def upsert(self, *, discord_id: int, romm_user_id: int, romm_username: str,
                     device_id: str, token_id: Optional[int], scopes: List[str],
                     expires_at: Optional[str], key_fingerprint: str,
                     sealed: bytes) -> int:
        """Store a credential and return its new generation.

        The generation is read and incremented in the same connection as the
        write, so two concurrent pairings cannot both claim the same number.
        """
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT generation FROM romm_user_tokens WHERE discord_id = ?",
                (discord_id,)
            )
            existing = await cursor.fetchone()
            generation = (existing['generation'] + 1) if existing else 1

            await conn.execute(
                """
                INSERT OR REPLACE INTO romm_user_tokens
                (discord_id, romm_user_id, romm_username, device_id, token_id,
                 generation, scopes, expires_at, key_fingerprint, sealed,
                 created_at, last_used_at, last_verified_at, expiry_warned_at,
                 invalid_since)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        CURRENT_TIMESTAMP, NULL, CURRENT_TIMESTAMP, NULL, NULL)
                """,
                (discord_id, romm_user_id, romm_username, device_id, token_id,
                 generation, json.dumps(scopes), expires_at, key_fingerprint, sealed)
            )
            await conn.commit()
        logger.info(f"Stored RomM credential for {discord_id} at generation {generation}")
        return generation

    async def get_sealed_row(self, discord_id: int) -> Optional[Dict[str, Any]]:
        """The only query that returns ciphertext. Called by store.get_grant."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                f"SELECT {AUDIT_COLUMNS}, key_fingerprint, sealed "
                "FROM romm_user_tokens WHERE discord_id = ?",
                (discord_id,)
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        data = _as_audit_dict(row)
        data['key_fingerprint'] = row['key_fingerprint']
        data['sealed'] = row['sealed']
        return data

    async def audit_row(self, discord_id: int) -> Optional[Dict[str, Any]]:
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                f"SELECT {AUDIT_COLUMNS} FROM romm_user_tokens WHERE discord_id = ?",
                (discord_id,)
            )
            row = await cursor.fetchone()
        return _as_audit_dict(row) if row else None

    async def list_audit(self) -> List[Dict[str, Any]]:
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                f"SELECT {AUDIT_COLUMNS} FROM romm_user_tokens ORDER BY created_at DESC"
            )
            rows = await cursor.fetchall()
        return [_as_audit_dict(row) for row in rows]

    async def find_by_romm_user(self, romm_user_id: int) -> List[Dict[str, Any]]:
        """Pairings pointing at one RomM account - a reportable condition."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                f"SELECT {AUDIT_COLUMNS} FROM romm_user_tokens WHERE romm_user_id = ?",
                (romm_user_id,)
            )
            rows = await cursor.fetchall()
        return [_as_audit_dict(row) for row in rows]

    async def delete(self, discord_id: int) -> bool:
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                "DELETE FROM romm_user_tokens WHERE discord_id = ?", (discord_id,)
            )
            await conn.commit()
            return cursor.rowcount > 0

    async def _guarded_set(self, column: str, discord_id: int, generation: int) -> bool:
        """Set one timestamp column, only if the credential has not moved on."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                f"UPDATE romm_user_tokens SET {column} = CURRENT_TIMESTAMP "
                "WHERE discord_id = ? AND generation = ?",
                (discord_id, generation)
            )
            await conn.commit()
            affected = cursor.rowcount > 0
        if not affected:
            logger.debug(
                f"Skipped {column} for {discord_id}: generation {generation} is stale"
            )
        return affected

    async def reseal(self, *, discord_id: int, key_fingerprint: str, sealed: bytes) -> bool:
        """Swap the ciphertext for the same credential under a new key.

        Deliberately does not touch `generation`: the credential has not
        changed, only the key protecting it, and bumping it here would
        invalidate in-flight probes for no reason.
        """
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                "UPDATE romm_user_tokens SET key_fingerprint = ?, sealed = ? "
                "WHERE discord_id = ?",
                (key_fingerprint, sealed, discord_id)
            )
            await conn.commit()
            return cursor.rowcount > 0

    async def mark_invalid(self, discord_id: int, generation: int) -> bool:
        return await self._guarded_set('invalid_since', discord_id, generation)

    async def touch_verified(self, discord_id: int, generation: int) -> bool:
        return await self._guarded_set('last_verified_at', discord_id, generation)

    async def touch_used(self, discord_id: int, generation: int) -> bool:
        return await self._guarded_set('last_used_at', discord_id, generation)

    async def mark_expiry_warned(self, discord_id: int, generation: int) -> bool:
        return await self._guarded_set('expiry_warned_at', discord_id, generation)
```

Note: `_guarded_set` interpolates `column`, which the SQL boundary test's `execute_calls` rule permits because the module is a declared repository. The value is never caller-supplied — every call site passes a literal.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_romm_tokens_repo.py -v`
Expected: PASS (10 tests).

- [ ] **Step 6: Widen the SQL boundary scan, then declare the repository**

The scan is non-recursive over the repo root, so adding `romm_tokens/repo.py` to `REPOSITORIES` without this change would be a no-op *and* would leave the other three `romm_tokens` modules unchecked while appearing covered.

In `tests/test_sql_boundaries.py`, in `modules()`, after the `integrations` line:

```python
    paths += sorted(Path("romm_tokens").glob("*.py"))
```

and add to `REPOSITORIES`:

```python
    Path("romm_tokens/repo.py"),
```

Then add this test to the same file, alongside `test_the_scan_reaches_inside_the_requests_package`:

```python
    def test_the_scan_reaches_the_token_package(self):
        """A rule that silently stops checking is worse than no rule.

        modules() collects Path(".").glob("*.py"), which is not recursive,
        so declaring romm_tokens/repo.py a repository without widening the
        scan would be a no-op that also left crypto, store and device_flow
        unchecked while appearing covered.
        """
        scanned = {str(path) for path in modules()}

        self.assertIn(str(Path("romm_tokens/crypto.py")), scanned)
```

- [ ] **Step 7: Run the structural suite**

Run: `python -m pytest tests/test_sql_boundaries.py -v`
Expected: PASS, including the new scan-reach test.

- [ ] **Step 8: Commit**

```bash
git add database_manager.py romm_tokens/ tests/test_romm_tokens_repo.py tests/test_sql_boundaries.py
git commit -m "feat(pair): store credentials behind a generation guard"
```

---

## Task 4: `get_user_link_strict` — a lookup that cannot fail open

**Files:**
- Modify: `database_manager.py`
- Test: `tests/test_row_access.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `MasterDatabase.get_user_link_strict(discord_id) -> dict | None`, which raises rather than returning `None` on a database error.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_row_access.py`:

```python
class StrictUserLinkLookupTests(unittest.TestCase):
    """The identity check must not read a broken database as 'no link'.

    get_user_link swallows every exception and returns None, which is fine
    where a degraded read is cosmetic. Pairing treats "no link" as "store
    it", so there the same None would wave a contradicting pairing through
    exactly when the database is unhealthy.
    """

    def test_absent_link_is_none(self):
        import asyncio
        import os
        import tempfile

        from database_manager import MasterDatabase

        with tempfile.TemporaryDirectory() as folder:
            db = MasterDatabase(db_path=os.path.join(folder, "t.db"))
            asyncio.run(db.initialize())
            self.assertIsNone(asyncio.run(db.get_user_link_strict(999)))

    def test_database_error_raises_rather_than_returning_none(self):
        import asyncio

        from database_manager import MasterDatabase

        db = object.__new__(MasterDatabase)

        class _Boom:
            async def __aenter__(self):
                raise RuntimeError("database is locked")

            async def __aexit__(self, *exc):
                return False

        db.get_connection = lambda: _Boom()

        with self.assertRaises(RuntimeError):
            asyncio.run(db.get_user_link_strict(111))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_row_access.py::StrictUserLinkLookupTests -v`
Expected: FAIL with `AttributeError: 'MasterDatabase' object has no attribute 'get_user_link_strict'`.

- [ ] **Step 3: Add the accessor**

In `database_manager.py`, directly after `get_user_link`:

```python
    async def get_user_link_strict(self, discord_id: int) -> Optional[Dict[str, Any]]:
        """get_user_link, but a failure is an error rather than an absence.

        The swallowing version cannot distinguish "this user has no link"
        from "the database is unavailable", and pairing acts on that
        difference: no link means store the pairing, so a degraded read
        would wave through the identity check that exists to stop a phished
        credential being filed under someone else's Discord id.

        The existing accessor keeps its behaviour for the callers where a
        missed link is cosmetic.
        """
        async with self.get_connection() as db:
            cursor = await db.execute(
                """
                SELECT discord_id, romm_username, romm_id, discord_username,
                       discord_avatar, created_by_bot, created_at, updated_at
                FROM user_links
                WHERE discord_id = ?
                """,
                (discord_id,)
            )
            row = await cursor.fetchone()

        if row is None:
            return None
        return {
            'discord_id': row['discord_id'],
            'romm_username': row['romm_username'],
            'romm_id': row['romm_id'],
            'discord_username': row['discord_username'],
            'discord_avatar': row['discord_avatar'],
            'created_by_bot': bool(row['created_by_bot']),
            'created_at': row['created_at'],
            'updated_at': row['updated_at'],
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_row_access.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add database_manager.py tests/test_row_access.py
git commit -m "feat(pair): add a user-link lookup that reports errors instead of absence"
```

---

## Task 5: The `RommClient` seam and `ActingClient`

**Files:**
- Modify: `romm_client.py` (lines 157, 329, 358, 451)
- Test: `tests/test_romm_client_identity.py`

**Interfaces:**
- Consumes: a `grant` object exposing `.token: str` (Task 7 supplies the real `Grant`; this task only needs the attribute).
- Produces: `RommClient._auth_header(grant=None) -> dict`; `RommClient.acting_as(grant, on_auth_failure=None) -> ActingClient`; `ActingClient.request(method, path, *, json=None, timeout=None) -> tuple[int, dict | None]`, raising `RommAuthError` on 401/403.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_romm_client_identity.py`:

```python
"""The acting path, and the trap it has to avoid.

romm_client.py's 401 branch refreshes the bot's OAuth token and replays the
request. Reached with a user's grant, that silently re-sends the write as
the bot - which for a streaming claim means claiming the session as the
bot, the exact bug per-user auth exists to remove.
"""

import asyncio
import unittest

from romm_client import RommAuthError, RommClient


class _FakeResponse:
    def __init__(self, status, payload=None):
        self.status = status
        self._payload = payload if payload is not None else {}

    async def json(self):
        return self._payload

    async def text(self):
        return "body"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """Records the Authorization header of every request it is given."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.sent = []
        self.closed = False

    def request(self, method, url, **kwargs):
        self.sent.append((method, url, kwargs.get('headers', {})))
        return self._responses.pop(0)


class _Config:
    API_BASE_URL = "https://romm.example"
    API_TIMEOUT = 30
    CACHE_TTL = 60
    ROMM_CLIENT_TOKEN = "rmm_bot_token"


class _Grant:
    token = "rmm_user_token"
    discord_id = 111
    generation = 1


def _client(session):
    client = RommClient(_Config())
    client.session = session
    client.access_token = "rmm_bot_token"
    client.token_expiry = 2 ** 31
    return client


class ActingClientTests(unittest.TestCase):
    def test_acting_request_uses_the_grant_not_the_bot_token(self):
        session = _FakeSession([_FakeResponse(200, {"ok": True})])
        client = _client(session)

        status, body = asyncio.run(
            client.acting_as(_Grant()).request("POST", "/api/streaming/sessions")
        )

        self.assertEqual(200, status)
        self.assertEqual({"ok": True}, body)
        self.assertEqual("Bearer rmm_user_token", session.sent[0][2]["Authorization"])

    def test_a_401_is_terminal_and_never_replays_as_the_bot(self):
        # Two responses queued; only one may be consumed.
        session = _FakeSession([_FakeResponse(401), _FakeResponse(200, {"ok": True})])
        client = _client(session)

        with self.assertRaises(RommAuthError):
            asyncio.run(client.acting_as(_Grant()).request("POST", "/api/streaming/sessions"))

        self.assertEqual(1, len(session.sent))
        self.assertEqual("rmm_bot_token", client.access_token)

    def test_auth_failure_callback_fires_once_with_the_grant(self):
        session = _FakeSession([_FakeResponse(403)])
        client = _client(session)
        seen = []

        async def on_auth_failure(grant):
            seen.append(grant)

        acting = client.acting_as(_Grant(), on_auth_failure=on_auth_failure)
        with self.assertRaises(RommAuthError):
            asyncio.run(acting.request("DELETE", "/api/streaming/sessions/ps2"))

        self.assertEqual(1, len(seen))

    def test_the_acting_path_never_writes_to_the_shared_cache(self):
        # APICache is keyed by endpoint alone, so a per-user response in it
        # would be served to a different user.
        session = _FakeSession([_FakeResponse(200, {"secret": "user-specific"})])
        client = _client(session)

        asyncio.run(client.acting_as(_Grant()).request("GET", "/api/users/me"))

        self.assertEqual({}, client.cache.cache)

    def test_acting_client_exposes_no_cache_backed_reader(self):
        acting = _client(_FakeSession([])).acting_as(_Grant())

        self.assertFalse(hasattr(acting, "fetch_api_endpoint"))


class BotPathRegressionTests(unittest.TestCase):
    def test_bot_header_is_unchanged(self):
        client = _client(_FakeSession([]))

        header = asyncio.run(client._auth_header())

        self.assertEqual(
            {"Authorization": "Bearer rmm_bot_token", "Accept": "application/json"},
            header,
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_romm_client_identity.py -v`
Expected: FAIL with `AttributeError: 'RommClient' object has no attribute 'acting_as'`.

- [ ] **Step 3: Add `roms.user.write` to the bot's scope string**

In `romm_client.py`, in `get_oauth_token`, replace the scope line:

```python
            data.add_field('scope', 'roms.read platforms.read firmware.read users.read users.write me.write assets.read')
```

with:

```python
            # roms.user.write is here for the admin force-reclaim paths on
            # streaming sessions, which are the bot's to run, not a user's.
            data.add_field(
                'scope',
                'roms.read platforms.read firmware.read users.read users.write '
                'me.write assets.read roms.user.write'
            )
```

- [ ] **Step 4: Add the auth-header seam**

In `romm_client.py`, add this method to `RommClient` directly above `make_authenticated_request`:

```python
    async def _auth_header(self, grant=None) -> Dict[str, str]:
        """The Authorization header for whoever is acting.

        grant is None for the bot, which is every existing caller. A grant
        supplies its own token and deliberately does not go near
        ensure_valid_token: a device-grant credential has no refresh, so
        there is nothing to refresh it with.
        """
        if grant is not None:
            return {"Authorization": f"Bearer {grant.token}", "Accept": "application/json"}

        if not await self.ensure_valid_token():
            raise RommAuthError("Failed to obtain valid OAuth token")
        return {"Authorization": f"Bearer {self.access_token}", "Accept": "application/json"}

    def acting_as(self, grant, on_auth_failure=None) -> "ActingClient":
        """A client bound to one user's credential.

        Callers hold an object that cannot act as the bot. The alternative -
        an identity= kwarg on the existing methods - fails silently as the
        bot when someone forgets it, which is the bug this feature exists to
        remove.
        """
        return ActingClient(self, grant, on_auth_failure)
```

- [ ] **Step 5: Route the two straightforward header sites through it**

In `make_authenticated_request`, replace these six lines:

```python
            if not await self.ensure_valid_token():
                logger.error("Failed to obtain valid OAuth token")
                return None

            session = await self.ensure_session()
            url = f"{self.config.API_BASE_URL}/api/{endpoint}"

            headers = {"Authorization": f"Bearer {self.access_token}", "Accept": "application/json"}
```

with:

```python
            try:
                headers = await self._auth_header()
            except RommAuthError:
                # This method reports failure as None, not as an exception.
                logger.error("Failed to obtain valid OAuth token")
                return None

            session = await self.ensure_session()
            url = f"{self.config.API_BASE_URL}/api/{endpoint}"
```

In `_get_json`, replace:

```python
        if not await self.ensure_valid_token():
            raise RommAuthError("Failed to obtain valid OAuth token")

        session = await self.ensure_session()
        url = f"{self.config.API_BASE_URL}/api/{endpoint}"

        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json"
        }
```

with:

```python
        headers = await self._auth_header()

        session = await self.ensure_session()
        url = f"{self.config.API_BASE_URL}/api/{endpoint}"
```

Leave `romm_client.py:358` — the 401 replay inside `make_authenticated_request` — exactly as it is. It is correct for the bot path, and `ActingClient` never reaches it.

- [ ] **Step 6: Write `ActingClient`**

Append to `romm_client.py`, after the `RommClient` class:

```python
class ActingClient:
    """One user's credential, over the bot's session and rate limiter.

    Three things this deliberately does not have:

    No cache. APICache is keyed by endpoint alone, so a per-user response
    stored in it would be served to a different user. There is no
    cache-backed reader here at all, which makes that impossible rather
    than merely discouraged.

    No retry. A 401 or 403 on a device-grant credential means revoked,
    expired or under-scoped; none of those improve on a second attempt, and
    RommClient's own 401 branch would refresh the *bot's* token and replay
    the request as the bot.

    No flattening. Callers get (status, body) because the difference
    between 409 and 503 is the whole behaviour of the streaming queue.
    """

    def __init__(self, client: "RommClient", grant, on_auth_failure=None):
        self._client = client
        self._grant = grant
        self._on_auth_failure = on_auth_failure

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Optional[Dict] = None,
        timeout: Optional[int] = None,
    ) -> tuple[int, Optional[Dict]]:
        """Make one call as this user. Raises RommAuthError on 401/403."""
        await self._client.rate_limiter.acquire()
        session = await self._client.ensure_session()
        url = f"{self._client.config.API_BASE_URL}{path}"
        headers = await self._client._auth_header(self._grant)

        kwargs: Dict[str, Any] = {"headers": headers}
        if json is not None:
            kwargs["json"] = json
        if timeout is not None:
            kwargs["timeout"] = aiohttp.ClientTimeout(total=timeout)

        async with session.request(method, url, **kwargs) as response:
            if response.status in (401, 403):
                if self._on_auth_failure is not None:
                    await self._on_auth_failure(self._grant)
                raise RommAuthError(
                    f"RomM rejected the acting credential for {method} {path} "
                    f"(status {response.status})"
                )
            try:
                body = await response.json()
            except Exception:
                # Several RomM endpoints answer success with an empty body.
                body = None
            logger.debug(f"acting: {method} {path} -> {response.status}")
            return response.status, body
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `python -m pytest tests/test_romm_client_identity.py tests/test_romm_client.py tests/test_bot_auth.py -v`
Expected: PASS. The existing `test_romm_client.py` suite is the regression gate for the two rewritten header sites.

- [ ] **Step 8: Commit**

```bash
git add romm_client.py tests/test_romm_client_identity.py
git commit -m "feat(pair): add an acting-as seam that cannot fall back to the bot"
```

---

## Task 6: `romm_tokens/device_flow.py` — init, URL, and the poll loop

**Files:**
- Create: `romm_tokens/device_flow.py`
- Test: `tests/test_device_flow.py`

**Interfaces:**
- Consumes: `RommClient` (for `ensure_session`, `rate_limiter`, `config`).
- Produces: `PairingRequest(device_code, user_code, verification_url, expires_in, interval)`; `PairOutcome` enum (`APPROVED`, `DENIED`, `EXPIRED`, `ERROR`); `PollResult(outcome, payload)`; `DeviceFlow(romm_client)` with `async init(device_identifier, display_name) -> PairingRequest | None` and `async poll(request, sleeper=asyncio.sleep) -> PollResult`; constants `PENDING`, `SLOW_DOWN`, `DENIED_DETAIL`, `EXPIRED_DETAIL`; `REQUESTED_SCOPES`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_device_flow.py`:

```python
"""The device grant, driven by the responses the live server actually sends.

All four outcomes are HTTP 400. A loop keyed on the status code passes a
naive test and hangs for ten minutes on a denial in production, so every
case here is a real body captured from RomM 5.2.
"""

import asyncio
import unittest

from romm_tokens.device_flow import DeviceFlow, PairOutcome

INIT_BODY = {
    "device_code": "d" * 64,
    "user_code": "WUTPT69K",
    "verification_path": "/pair/device",
    "verification_path_complete": "/pair/device?user_code=WUTPT69K",
    "expires_in": 600,
    "interval": 5,
}


class _FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def json(self):
        return self._payload

    async def text(self):
        return "body"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.sent = []

    def request(self, method, url, **kwargs):
        self.sent.append((method, url, kwargs.get('json')))
        return self._responses.pop(0)


class _RateLimiter:
    async def acquire(self):
        return None


class _Config:
    API_BASE_URL = "https://romm.internal:8087"
    ROMM_PAIR_BASE_URL = "https://roms.example.com"


class _FakeClient:
    def __init__(self, session):
        self.config = _Config()
        self.rate_limiter = _RateLimiter()
        self._session = session

    async def ensure_session(self):
        return self._session


def _flow(responses):
    return DeviceFlow(_FakeClient(_FakeSession(responses)))


def pending():
    return _FakeResponse(400, {"detail": "authorization_pending"})


class InitTests(unittest.TestCase):
    def test_init_requests_only_the_two_scopes(self):
        flow = _flow([_FakeResponse(201, INIT_BODY)])

        asyncio.run(flow.init("romm-comm:111", "romm-comm - @jake - a7f3"))

        payload = flow.client._session.sent[0][2]
        self.assertEqual(["me.read", "roms.user.write"], payload["requested_scopes"])
        self.assertEqual("romm-comm:111", payload["client_device_identifier"])
        self.assertEqual("romm-comm - @jake - a7f3", payload["name"])

    def test_url_is_built_from_the_pairing_origin_not_the_api_url(self):
        flow = _flow([_FakeResponse(201, INIT_BODY)])

        request = asyncio.run(flow.init("romm-comm:111", "probe"))

        self.assertEqual(
            "https://roms.example.com/pair/device?user_code=WUTPT69K",
            request.verification_url,
        )

    def test_device_code_is_not_in_the_repr(self):
        flow = _flow([_FakeResponse(201, INIT_BODY)])

        request = asyncio.run(flow.init("romm-comm:111", "probe"))

        self.assertNotIn("d" * 64, repr(request))


class PollTests(unittest.TestCase):
    def setUp(self):
        self.slept = []

    async def _sleeper(self, seconds):
        self.slept.append(seconds)

    def _poll(self, responses):
        flow = _flow([_FakeResponse(201, INIT_BODY)] + responses)
        request = asyncio.run(flow.init("romm-comm:111", "probe"))
        return asyncio.run(flow.poll(request, sleeper=self._sleeper))

    def test_approval_returns_the_payload(self):
        grant = {"access_token": "rmm_x", "device_id": "dev", "scopes": ["me.read"],
                 "expires_at": None}
        result = self._poll([pending(), _FakeResponse(200, grant)])

        self.assertIs(PairOutcome.APPROVED, result.outcome)
        self.assertEqual("rmm_x", result.payload["access_token"])

    def test_denial_stops_immediately_rather_than_waiting_out_the_window(self):
        result = self._poll([pending(), _FakeResponse(400, {"detail": "access_denied"})])

        self.assertIs(PairOutcome.DENIED, result.outcome)
        self.assertEqual(2, len(self.slept))

    def test_expiry_is_terminal(self):
        result = self._poll([_FakeResponse(400, {"detail": "expired_token"})])

        self.assertIs(PairOutcome.EXPIRED, result.outcome)

    def test_slow_down_widens_the_interval(self):
        # Observed at poll 57 of a 120-poll window, so this is the ordinary
        # path for any user who takes more than about five minutes.
        grant = {"access_token": "rmm_x", "device_id": "d", "scopes": [], "expires_at": None}
        self._poll([
            pending(),
            _FakeResponse(400, {"detail": "slow_down"}),
            _FakeResponse(200, grant),
        ])

        self.assertEqual([5, 5, 10], self.slept)

    def test_an_unknown_detail_is_terminal_rather_than_an_infinite_loop(self):
        result = self._poll([_FakeResponse(400, {"detail": "something_new"})])

        self.assertIs(PairOutcome.ERROR, result.outcome)

    def test_polling_gives_up_at_the_expiry_window(self):
        request_responses = [pending()] * 200
        flow = _flow([_FakeResponse(201, INIT_BODY)] + request_responses)
        request = asyncio.run(flow.init("romm-comm:111", "probe"))

        result = asyncio.run(flow.poll(request, sleeper=self._sleeper))

        self.assertIs(PairOutcome.EXPIRED, result.outcome)
        self.assertLessEqual(sum(self.slept), request.expires_in + request.interval)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_device_flow.py -v`
Expected: collection error — `ModuleNotFoundError: No module named 'romm_tokens.device_flow'`.

- [ ] **Step 3: Write `romm_tokens/device_flow.py`**

```python
"""The OAuth 2.0 device authorization grant, as RomM 5.2 actually behaves.

Verified against a live instance on 2026-09-14, because the OpenAPI
document declares only the success responses and the rest is the entire
control flow:

    init                -> 201, expires_in 600, interval 5
    while pending       -> 400 {"detail": "authorization_pending"}
    throttled           -> 400 {"detail": "slow_down"}
    user declined       -> 400 {"detail": "access_denied"}
    window elapsed      -> 400 {"detail": "expired_token"}

Every outcome bar success shares HTTP 400, so nothing but the detail string
separates "still waiting" from "they said no". A loop keyed on the status
code spins for the remaining ten minutes after a denial.

slow_down arrived at poll 57 of a 120-poll window - roughly five minutes -
so it is the ordinary path for anyone who does not reach their browser
immediately, not a rare server mood.

This borrows the client's session and rate limiter rather than opening its
own. A poll loop at the server's interval, times the concurrency cap, is
the highest-volume thing the feature does; leaving it outside the limiter
that governs every other call would be an odd exception.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional

logger = logging.getLogger('romm_bot.tokens')

# The least that lets the bot claim a streaming session and know who
# approved. me.read is not decoration: the token response does not say
# which account granted it.
REQUESTED_SCOPES = ["me.read", "roms.user.write"]

PENDING = "authorization_pending"
SLOW_DOWN = "slow_down"
DENIED_DETAIL = "access_denied"
EXPIRED_DETAIL = "expired_token"

# How much to widen the interval each time the server says slow_down.
BACKOFF_STEP_SECONDS = 5

CLIENT_NAME = "romm-comm"


class PairOutcome(Enum):
    """How a pairing attempt ended."""

    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    ERROR = "error"


@dataclass(frozen=True)
class PairingRequest:
    """One in-flight attempt.

    device_code is repr=False and is never persisted: it alone bears the
    grant, so anything that prints it - a traceback, a log line, an
    assertion diff - hands the credential away.
    """

    device_code: str = field(repr=False)
    user_code: str
    verification_url: str
    expires_in: int
    interval: int


@dataclass
class PollResult:
    outcome: PairOutcome
    payload: Optional[Dict[str, Any]] = field(default=None, repr=False)


class DeviceFlow:
    """Start a device grant and poll it to a conclusion."""

    def __init__(self, client):
        self.client = client

    def _pair_url(self, path: str) -> str:
        """Join a relative verification path to the configured origin.

        RomM returns this path relative on purpose - it is origin-agnostic,
        and the API URL is often a LAN address that is useless in the
        browser the user will actually open.
        """
        return f"{self.client.config.ROMM_PAIR_BASE_URL}{path}"

    async def init(self, device_identifier: str, display_name: str) -> Optional[PairingRequest]:
        """Start a grant. Returns None if RomM refused."""
        payload = {
            "client_device_identifier": device_identifier,
            "name": display_name,
            "client": CLIENT_NAME,
            "platform": "discord",
            "client_version": "1.0",
            "requested_scopes": REQUESTED_SCOPES,
        }

        await self.client.rate_limiter.acquire()
        session = await self.client.ensure_session()
        url = f"{self.client.config.API_BASE_URL}/api/auth/device/init"

        async with session.request("POST", url, json=payload) as response:
            # 201, not 200.
            if response.status not in (200, 201):
                text = await response.text()
                logger.error(f"device/init refused with {response.status}: {text}")
                return None
            body = await response.json()

        return PairingRequest(
            device_code=body["device_code"],
            user_code=body["user_code"],
            verification_url=self._pair_url(body["verification_path_complete"]),
            expires_in=int(body["expires_in"]),
            interval=int(body["interval"]),
        )

    async def poll(self, request: PairingRequest, sleeper=asyncio.sleep) -> PollResult:
        """Poll until the user decides, the window closes, or we give up.

        sleeper is injectable so tests can run the real loop without the
        real ten minutes.
        """
        interval = request.interval
        waited = 0

        while waited < request.expires_in:
            await sleeper(interval)
            waited += interval

            status, body = await self._token_once(request.device_code)

            if status in (200, 201) and isinstance(body, dict) and body.get("access_token"):
                return PollResult(outcome=PairOutcome.APPROVED, payload=body)

            detail = body.get("detail") if isinstance(body, dict) else None

            if detail == PENDING:
                continue
            if detail == SLOW_DOWN:
                interval += BACKOFF_STEP_SECONDS
                logger.debug(f"device/token asked us to slow down; interval now {interval}s")
                continue
            if detail == DENIED_DETAIL:
                return PollResult(outcome=PairOutcome.DENIED)
            if detail == EXPIRED_DETAIL:
                return PollResult(outcome=PairOutcome.EXPIRED)

            # An unrecognised answer is terminal rather than a loop that
            # runs until the window closes.
            logger.error(f"device/token returned {status} with unexpected detail {detail!r}")
            return PollResult(outcome=PairOutcome.ERROR)

        return PollResult(outcome=PairOutcome.EXPIRED)

    async def _token_once(self, device_code: str):
        """One device/token call. Never logs the body: it carries the token."""
        await self.client.rate_limiter.acquire()
        session = await self.client.ensure_session()
        url = f"{self.client.config.API_BASE_URL}/api/auth/device/token"

        async with session.request("POST", url, json={"device_code": device_code}) as response:
            try:
                body = await response.json()
            except Exception:
                body = None
            return response.status, body
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_device_flow.py -v`
Expected: PASS (9 tests).

- [ ] **Step 5: Commit**

```bash
git add romm_tokens/device_flow.py tests/test_device_flow.py
git commit -m "feat(pair): drive the device grant off the detail string, not the status"
```

---

## Task 7: `romm_tokens/store.py` — the only producer of a bearer token

**Files:**
- Modify: `romm_tokens/store.py` (the Task 3 placeholder), `romm_tokens/__init__.py`
- Test: `tests/test_romm_tokens_store.py`, `tests/test_romm_tokens_expiry.py`, `tests/test_romm_tokens_secrecy.py`

**Interfaces:**
- Consumes: Task 2 `crypto`, Task 3 `TokenRepo`, Task 4 `get_user_link_strict`, Task 5 `acting_as`, Task 6 `DeviceFlow`.
- Produces: `Grant(discord_id, generation, romm_user_id, romm_username, scopes, expires_at, created_at, token)` with `token` at `repr=False`; `PairRefusal` enum (`SCOPE_SHORTFALL`, `IDENTITY_MISMATCH`, `LOOKUP_FAILED`, `ALREADY_PAIRED_ELSEWHERE`); `TokenStore(db, config, romm_client)` with `async get_grant(discord_id) -> Grant | None`, `async acting_client(discord_id) -> ActingClient | None`, `async complete_pair(discord_id, payload) -> tuple[Grant | None, PairRefusal | None]`, `async invalidate(grant)`, `async unpair(discord_id) -> bool`, `async revoke_token(token_id) -> bool`.

- [ ] **Step 1: Write the failing store tests**

Create `tests/test_romm_tokens_store.py`:

```python
"""Storing a credential, and the four times it must refuse to.

Each refusal exists because storing would be worse than not pairing: a
credential that cannot do its job, one whose owner contradicts what the bot
was told, one decided on a database read that failed, and one that would
quietly duplicate someone else's account.
"""

import asyncio
import base64
import os
import tempfile
import unittest

from database_manager import MasterDatabase
from romm_tokens.store import PairRefusal, TokenStore

KEY = base64.b64encode(b"K" * 32).decode()

GRANT_PAYLOAD = {
    "access_token": "rmm_user_token",
    "device_id": "dev-1",
    "scopes": ["me.read", "roms.user.write"],
    "expires_at": None,
}


class _Config:
    ROMM_TOKEN_KEY = KEY
    ROMM_TOKEN_KEY_FILE = None
    ROMM_TOKEN_KEY_OLD = None
    ROMM_PAIR_MAX_AGE_DAYS = 90
    API_BASE_URL = "https://romm.example"


class _FakeActing:
    """Answers /users/me and /client-tokens the way RomM 5.2 does."""

    def __init__(self, me=None, tokens=None):
        self.me = me or {"id": 7, "username": "idiosync"}
        self.tokens = tokens if tokens is not None else [
            {"id": 3, "device_id": "dev-1", "name": "romm-comm probe",
             "created_at": "2026-09-15T02:59:09+00:00"}
        ]

    async def request(self, method, path, **kwargs):
        if path == "/api/users/me":
            return 200, self.me
        if path == "/api/client-tokens":
            return 200, self.tokens
        raise AssertionError(f"unexpected path {path}")


class _FakeRommClient:
    def __init__(self, acting=None):
        self.config = _Config()
        self._acting = acting or _FakeActing()
        self.admin_deleted = []

    def acting_as(self, grant, on_auth_failure=None):
        return self._acting

    async def make_authenticated_request(self, method, endpoint, **kwargs):
        self.admin_deleted.append((method, endpoint))
        return {}


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = MasterDatabase(db_path=os.path.join(self._dir.name, "t.db"))
        asyncio.run(self.db.initialize())
        self.romm = _FakeRommClient()
        self.store = TokenStore(self.db, _Config(), self.romm)

    def tearDown(self):
        self._dir.cleanup()

    def complete(self, discord_id=111, payload=None):
        return asyncio.run(self.store.complete_pair(discord_id, payload or dict(GRANT_PAYLOAD)))


class CompletePairTests(StoreTestCase):
    def test_a_good_pairing_is_stored_and_readable(self):
        grant, refusal = self.complete()

        self.assertIsNone(refusal)
        self.assertEqual(7, grant.romm_user_id)
        self.assertEqual("rmm_user_token", asyncio.run(self.store.get_grant(111)).token)

    def test_a_scope_shortfall_is_refused(self):
        payload = dict(GRANT_PAYLOAD, scopes=["me.read"])

        grant, refusal = self.complete(payload=payload)

        self.assertIs(PairRefusal.SCOPE_SHORTFALL, refusal)
        self.assertIsNone(grant)
        self.assertIsNone(asyncio.run(self.store.get_grant(111)))

    def test_a_contradicting_user_link_is_refused(self):
        # The phishing case: the approver is not who the bot was told this
        # Discord user is, so the credential is discarded rather than filed.
        asyncio.run(self.db.add_user_link(111, "someone_else", 99))

        grant, refusal = self.complete()

        self.assertIs(PairRefusal.IDENTITY_MISMATCH, refusal)
        self.assertIsNone(asyncio.run(self.store.get_grant(111)))

    def test_a_matching_user_link_is_accepted(self):
        asyncio.run(self.db.add_user_link(111, "idiosync", 7))

        grant, refusal = self.complete()

        self.assertIsNone(refusal)
        self.assertEqual(7, grant.romm_user_id)

    def test_a_failed_lookup_refuses_rather_than_storing(self):
        async def boom(discord_id):
            raise RuntimeError("database is locked")

        self.db.get_user_link_strict = boom

        grant, refusal = self.complete()

        self.assertIs(PairRefusal.LOOKUP_FAILED, refusal)
        self.assertIsNone(asyncio.run(self.store.get_grant(111)))

    def test_pairing_an_already_paired_romm_account_is_refused(self):
        self.complete(discord_id=111)

        grant, refusal = self.complete(discord_id=222)

        self.assertIs(PairRefusal.ALREADY_PAIRED_ELSEWHERE, refusal)

    def test_re_pairing_revokes_the_token_it_supersedes(self):
        # Verified 2026-09-14: RomM does not replace the old token, it keeps
        # both. Left alone, every re-pair strands a live credential.
        self.complete()
        self.romm.admin_deleted.clear()

        self.complete()

        self.assertIn(("DELETE", "client-tokens/3/admin"), self.romm.admin_deleted)


class GetGrantTests(StoreTestCase):
    def test_an_invalidated_row_reads_as_absent(self):
        grant, _ = self.complete()
        asyncio.run(self.store.invalidate(grant))

        self.assertIsNone(asyncio.run(self.store.get_grant(111)))

    def test_an_unopenable_row_reads_as_absent_rather_than_raising(self):
        self.complete()
        rotated = TokenStore(
            self.db,
            type("C", (_Config,), {"ROMM_TOKEN_KEY": base64.b64encode(b"Z" * 32).decode()}),
            self.romm,
        )

        self.assertIsNone(asyncio.run(rotated.get_grant(111)))


NEW_KEY = base64.b64encode(b"N" * 32).decode()


class RotationTests(StoreTestCase):
    def _rotated(self, old=KEY):
        config = type("C", (_Config,), {"ROMM_TOKEN_KEY": NEW_KEY, "ROMM_TOKEN_KEY_OLD": old})
        return TokenStore(self.db, config, self.romm)

    def test_rotation_reseals_and_keeps_the_credential_usable(self):
        self.complete()
        store = self._rotated()

        self.assertEqual(1, asyncio.run(store.reseal_under_current_key()))
        self.assertEqual("rmm_user_token", asyncio.run(store.get_grant(111)).token)

    def test_rotation_without_the_old_key_costs_a_re_pair_rather_than_crashing(self):
        self.complete()
        store = self._rotated(old=None)

        self.assertEqual(0, asyncio.run(store.reseal_under_current_key()))
        self.assertIsNone(asyncio.run(store.get_grant(111)))

    def test_resealing_twice_is_a_no_op(self):
        self.complete()
        store = self._rotated()
        asyncio.run(store.reseal_under_current_key())

        self.assertEqual(0, asyncio.run(store.reseal_under_current_key()))

    def test_an_unusable_old_key_is_reported_not_raised(self):
        self.complete()
        store = self._rotated(old="not base64 !!")

        self.assertEqual(0, asyncio.run(store.reseal_under_current_key()))
```

- [ ] **Step 2: Write the failing expiry tests**

Create `tests/test_romm_tokens_expiry.py`:

```python
"""The bot-side age bound.

RomM's own default is no expiry at all - a plain approval returns
expires_at: null - so without this the bot would hold non-expiring
credentials for every paired user forever.
"""

import unittest
from datetime import datetime, timedelta, timezone

from romm_tokens.store import is_past_bound


def days_ago(count):
    return (datetime.now(timezone.utc) - timedelta(days=count)).isoformat()


class AgeBoundTests(unittest.TestCase):
    def test_a_fresh_credential_is_within_the_bound(self):
        self.assertFalse(is_past_bound(created_at=days_ago(1), expires_at=None, max_age_days=90))

    def test_a_non_expiring_credential_still_ages_out(self):
        self.assertTrue(is_past_bound(created_at=days_ago(91), expires_at=None, max_age_days=90))

    def test_zero_disables_the_bound(self):
        self.assertFalse(is_past_bound(created_at=days_ago(900), expires_at=None, max_age_days=0))

    def test_the_servers_own_expiry_still_applies(self):
        self.assertTrue(is_past_bound(
            created_at=days_ago(1), expires_at=days_ago(1), max_age_days=90
        ))

    def test_a_naive_timestamp_is_treated_as_utc_rather_than_crashing(self):
        naive = (datetime.now(timezone.utc) - timedelta(days=1)).replace(tzinfo=None).isoformat()
        self.assertFalse(is_past_bound(created_at=naive, expires_at=None, max_age_days=90))

    def test_an_unparseable_timestamp_is_treated_as_past_the_bound(self):
        # Fail closed: a credential whose age cannot be established is not
        # one to keep using.
        self.assertTrue(is_past_bound(created_at="not a date", expires_at=None, max_age_days=90))
```

- [ ] **Step 3: Write the failing secrecy tests**

Create `tests/test_romm_tokens_secrecy.py`:

```python
"""Credential material must not reach a log record or a repr.

A dataclass's generated __repr__ prints every field, so a Grant that did
not opt out would leak the token into every f-string, logger.exception,
pytest assertion diff and py-cord traceback that touched it.
"""

import unittest
from datetime import datetime, timezone

from romm_tokens.store import Grant


def _grant():
    return Grant(
        discord_id=111,
        generation=1,
        romm_user_id=7,
        romm_username="idiosync",
        scopes=("me.read", "roms.user.write"),
        expires_at=None,
        created_at=datetime.now(timezone.utc),
        token="rmm_super_secret_value",
    )


class GrantSecrecyTests(unittest.TestCase):
    def test_repr_does_not_contain_the_token(self):
        self.assertNotIn("rmm_super_secret_value", repr(_grant()))

    def test_str_does_not_contain_the_token(self):
        self.assertNotIn("rmm_super_secret_value", str(_grant()))

    def test_format_does_not_contain_the_token(self):
        self.assertNotIn("rmm_super_secret_value", f"{_grant()}")

    def test_the_token_is_still_reachable_deliberately(self):
        self.assertEqual("rmm_super_secret_value", _grant().token)


class LogRecordTests(unittest.TestCase):
    """Nothing the token package logs may carry credential material.

    This codebase debug-logs response bodies and logs full bodies on
    failure, so the habit runs the other way and needs pinning.
    """

    def test_no_log_record_contains_the_token(self):
        import logging

        grant = _grant()
        logger = logging.getLogger('romm_bot.tokens')

        with self.assertLogs(logger, level="DEBUG") as captured:
            # The shapes a credential realistically reaches logging in.
            logger.debug(f"acting for {grant}")
            logger.info("credential for %s at generation %s", grant.discord_id, grant.generation)
            try:
                raise RuntimeError(f"failed while holding {grant}")
            except RuntimeError:
                logger.exception("pairing failed")

        self.assertNotIn("rmm_super_secret_value", "\n".join(captured.output))
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `python -m pytest tests/test_romm_tokens_store.py tests/test_romm_tokens_expiry.py tests/test_romm_tokens_secrecy.py -v`
Expected: collection errors — `ImportError: cannot import name 'TokenStore' from 'romm_tokens.store'`.

- [ ] **Step 5: Write `romm_tokens/store.py`**

Replace the placeholder file entirely:

```python
"""Orchestration for per-user RomM credentials.

This module is the only place in the tree that turns a stored row back into
a bearer token. Everything else - views, cogs, the streaming integration -
receives either a Grant it did not construct or an ActingClient that
already has one bound, so there is one function to audit rather than a
convention to uphold.

It also owns the four refusals. Each is a case where storing the credential
would be worse than not pairing at all, and three of them were added after
the design was checked against a live server rather than guessed at.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, Optional, Tuple

from dateutil import parser as date_parser

from romm_tokens.crypto import CredentialUnsealError, load_key, seal, unseal
from romm_tokens.device_flow import REQUESTED_SCOPES
from romm_tokens.repo import TokenRepo

logger = logging.getLogger('romm_bot.tokens')

# The scope without which a pairing cannot do the job it was created for.
ESSENTIAL_SCOPE = "roms.user.write"


class PairRefusal(Enum):
    """Why a completed grant was discarded instead of stored."""

    SCOPE_SHORTFALL = "scope_shortfall"
    IDENTITY_MISMATCH = "identity_mismatch"
    LOOKUP_FAILED = "lookup_failed"
    ALREADY_PAIRED_ELSEWHERE = "already_paired_elsewhere"
    UNREADABLE = "unreadable"


@dataclass(frozen=True)
class Grant:
    """One user's live credential, as the rest of the bot sees it.

    token is repr=False deliberately. A dataclass prints every field by
    default, and this object travels through logging, tracebacks and test
    failures.
    """

    discord_id: int
    generation: int
    romm_user_id: int
    romm_username: str
    scopes: Tuple[str, ...]
    expires_at: Optional[datetime]
    created_at: Optional[datetime]
    token: str = field(repr=False)


def _as_aware(raw) -> Optional[datetime]:
    """Parse a RomM timestamp, treating a naive one as UTC.

    RomM 5.2 sends ISO 8601 with an explicit +00:00, but the field is typed
    as a bare string, so nothing guarantees that forever. A naive value
    compared against an aware now() raises, which would turn a cosmetic
    format change into a crash in the revalidation loop.
    """
    if not raw:
        return None
    try:
        parsed = date_parser.parse(str(raw))
    except (ValueError, TypeError, OverflowError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def is_past_bound(created_at, expires_at, max_age_days: int) -> bool:
    """Whether a credential is too old to keep using.

    Two bounds, whichever comes first: the server's own expiry, and the
    bot's. The bot's exists because RomM's default is no expiry - an
    ordinary approval returns null - so without it the custody window for
    every paired user would be unbounded.
    """
    now = datetime.now(timezone.utc)

    expiry = _as_aware(expires_at)
    if expiry is not None and expiry <= now:
        return True

    if not max_age_days:
        return False

    created = _as_aware(created_at)
    if created is None:
        # Fail closed: a credential whose age cannot be established is not
        # one to keep handing out.
        logger.warning(f"Unparseable created_at {created_at!r}; treating as past the age bound")
        return True

    return created + timedelta(days=max_age_days) <= now


class TokenStore:
    """Custody of per-user credentials: read, store, invalidate, revoke."""

    def __init__(self, db, config, romm_client):
        self.db = db
        self.config = config
        self.romm = romm_client
        self.repo = TokenRepo(db)
        self.key = load_key(config.ROMM_TOKEN_KEY, config.ROMM_TOKEN_KEY_FILE)

    # ------------------------------------------------------------- reading

    async def get_grant(self, discord_id: int) -> Optional[Grant]:
        """The live credential for a Discord user, or None.

        None is an ordinary outcome, not an error: absent, invalidated,
        expired, aged out and unopenable all arrive here, and every caller's
        response to all five is the same - ask the user to pair again.
        """
        row = await self.repo.get_sealed_row(discord_id)
        if row is None:
            return None

        if row['invalid_since']:
            return None

        if is_past_bound(row['created_at'], row['expires_at'],
                         self.config.ROMM_PAIR_MAX_AGE_DAYS):
            logger.info(f"Credential for {discord_id} is past its age bound")
            await self.repo.mark_invalid(discord_id, row['generation'])
            return None

        if row['key_fingerprint'] != self.key.fingerprint:
            logger.error(
                f"Credential for {discord_id} was sealed under key "
                f"{row['key_fingerprint']}, which is not configured"
            )
            await self.repo.mark_invalid(discord_id, row['generation'])
            return None

        try:
            token = unseal(self.key, discord_id, row['sealed'])
        except CredentialUnsealError as e:
            logger.error(f"Could not open stored credential: {e}")
            await self.repo.mark_invalid(discord_id, row['generation'])
            return None

        return Grant(
            discord_id=discord_id,
            generation=row['generation'],
            romm_user_id=row['romm_user_id'],
            romm_username=row['romm_username'],
            scopes=tuple(row['scopes']),
            expires_at=_as_aware(row['expires_at']),
            created_at=_as_aware(row['created_at']),
            token=token,
        )

    async def acting_client(self, discord_id: int):
        """A client bound to this user, with invalidation already wired.

        Callers never build one themselves. If each had to invalidate after
        catching RommAuthError, a revoked credential would stay live in the
        database until the hourly loop noticed - and that is the same
        forgettable-step failure ActingClient exists to remove.
        """
        grant = await self.get_grant(discord_id)
        if grant is None:
            return None
        return self.romm.acting_as(grant, on_auth_failure=self.invalidate)

    # ------------------------------------------------------------- writing

    async def complete_pair(
        self, discord_id: int, payload: Dict[str, Any]
    ) -> Tuple[Optional[Grant], Optional[PairRefusal]]:
        """Turn an approved grant into a stored credential, or refuse it."""
        granted = list(payload.get('scopes') or [])
        if ESSENTIAL_SCOPE not in granted:
            logger.warning(
                f"Pairing for {discord_id} granted {granted}; discarding, "
                f"{ESSENTIAL_SCOPE} is required"
            )
            return None, PairRefusal.SCOPE_SHORTFALL

        probe = Grant(
            discord_id=discord_id, generation=0, romm_user_id=0, romm_username="",
            scopes=tuple(granted), expires_at=None, created_at=None,
            token=payload['access_token'],
        )
        acting = self.romm.acting_as(probe)

        identity = await self._read_identity(acting)
        if identity is None:
            return None, PairRefusal.UNREADABLE
        romm_user_id, romm_username = identity

        try:
            link = await self.db.get_user_link_strict(discord_id)
        except Exception as e:
            # Never read a broken database as "this user has no link": that
            # is the branch that stores the pairing.
            logger.error(f"user_links lookup failed for {discord_id}: {e}")
            return None, PairRefusal.LOOKUP_FAILED

        if link and link['romm_id'] != romm_user_id:
            logger.error(
                f"Pairing for {discord_id} was approved by RomM user {romm_user_id} "
                f"but user_links names {link['romm_id']}; discarding"
            )
            return None, PairRefusal.IDENTITY_MISMATCH

        shared = [row for row in await self.repo.find_by_romm_user(romm_user_id)
                  if row['discord_id'] != discord_id]
        if shared:
            logger.error(
                f"RomM user {romm_user_id} is already paired to "
                f"{[row['discord_id'] for row in shared]}; discarding"
            )
            return None, PairRefusal.ALREADY_PAIRED_ELSEWHERE

        superseded = await self.repo.audit_row(discord_id)
        token_id = await self._find_token_id(acting, payload.get('device_id'))

        generation = await self.repo.upsert(
            discord_id=discord_id,
            romm_user_id=romm_user_id,
            romm_username=romm_username,
            device_id=payload.get('device_id') or '',
            token_id=token_id,
            scopes=granted,
            expires_at=payload.get('expires_at'),
            key_fingerprint=self.key.fingerprint,
            sealed=seal(self.key, discord_id, payload['access_token']),
        )

        # Store first, revoke second: a failed revoke must leave the user
        # with a working pairing rather than none.
        if superseded and superseded.get('token_id') and superseded['token_id'] != token_id:
            await self.revoke_token(superseded['token_id'])

        return await self.get_grant(discord_id), None

    async def _read_identity(self, acting) -> Optional[Tuple[int, str]]:
        """Who approved. The token response does not say, so ask."""
        try:
            status, body = await acting.request("GET", "/api/users/me")
        except Exception as e:
            logger.error(f"Could not read /users/me for a new pairing: {e}")
            return None
        if status != 200 or not isinstance(body, dict) or not body.get('id'):
            logger.error(f"/users/me answered {status} for a new pairing")
            return None
        return int(body['id']), str(body.get('username') or '')

    async def _find_token_id(self, acting, device_id) -> Optional[int]:
        """The client-token id for the credential just minted.

        Matched on device_id, which is not unique: a stable device
        identifier reuses one device row and tokens accumulate under it, so
        the newest wins. A null id is tolerated - /unpair falls back to
        telling the user where to revoke by hand.
        """
        if not device_id:
            return None
        try:
            status, body = await acting.request("GET", "/api/client-tokens")
        except Exception as e:
            logger.error(f"Could not list client tokens: {e}")
            return None
        if status != 200 or not isinstance(body, list):
            return None

        matching = [t for t in body if t.get('device_id') == device_id]
        if not matching:
            logger.warning(f"No client token found for device {device_id}")
            return None
        newest = max(matching, key=lambda t: str(t.get('created_at') or ''))
        return newest.get('id')

    async def reseal_under_current_key(self) -> int:
        """Re-encrypt rows sealed under the previous key. Returns how many.

        A fingerprint identifies a key; it does not recover one. So rotation
        needs the old material, which is what ROMM_TOKEN_KEY_OLD carries,
        and it is a one-shot startup pass rather than something get_grant
        can do lazily.

        Rows sealed under a key that is configured nowhere are left for
        get_grant to mark invalid - rotating without ROMM_TOKEN_KEY_OLD is
        supported, it just costs everyone a re-pair.
        """
        if not getattr(self.config, 'ROMM_TOKEN_KEY_OLD', None):
            return 0

        try:
            old_key = load_key(self.config.ROMM_TOKEN_KEY_OLD, None)
        except ValueError as e:
            logger.error(f"ROMM_TOKEN_KEY_OLD is unusable, skipping re-seal: {e}")
            return 0

        if old_key.fingerprint == self.key.fingerprint:
            return 0

        resealed = 0
        for audit in await self.repo.list_audit():
            discord_id = audit['discord_id']
            row = await self.repo.get_sealed_row(discord_id)
            if row is None or row['key_fingerprint'] != old_key.fingerprint:
                continue
            try:
                token = unseal(old_key, discord_id, row['sealed'])
            except CredentialUnsealError as e:
                logger.error(f"Could not re-seal credential for {discord_id}: {e}")
                continue

            await self.repo.reseal(
                discord_id=discord_id,
                key_fingerprint=self.key.fingerprint,
                sealed=seal(self.key, discord_id, token),
            )
            resealed += 1

        if resealed:
            logger.info(f"Re-sealed {resealed} credential(s) under the current key")
        return resealed

    async def invalidate(self, grant: Grant) -> bool:
        """Mark a credential dead, if it is still the current one."""
        return await self.repo.mark_invalid(grant.discord_id, grant.generation)

    async def unpair(self, discord_id: int) -> bool:
        """Forget a credential, and revoke it server-side if we can."""
        row = await self.repo.audit_row(discord_id)
        await self.repo.delete(discord_id)
        if row and row.get('token_id'):
            return await self.revoke_token(row['token_id'])
        return False

    async def revoke_token(self, token_id: int) -> bool:
        """Admin-delete one client token, using the bot's own credential.

        The user's token deliberately has no scope for this: revocation
        runs on users.write, which the bot already holds, so the credential
        the bot stores on someone's behalf stays as small as it can be.
        """
        result = await self.romm.make_authenticated_request(
            "DELETE", f"client-tokens/{token_id}/admin"
        )
        if result is None:
            logger.error(f"Could not revoke client token {token_id}")
            return False
        logger.info(f"Revoked client token {token_id}")
        return True
```

- [ ] **Step 6: Export the public surface**

Append to `romm_tokens/__init__.py`:

```python
from romm_tokens.crypto import CredentialUnsealError  # noqa: E402
from romm_tokens.store import Grant, PairRefusal, TokenStore  # noqa: E402

__all__ = ["CredentialUnsealError", "Grant", "PairRefusal", "TokenStore"]
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `python -m pytest tests/test_romm_tokens_store.py tests/test_romm_tokens_expiry.py tests/test_romm_tokens_secrecy.py -v`
Expected: PASS (24 tests).

- [ ] **Step 8: Commit**

```bash
git add romm_tokens/ tests/test_romm_tokens_store.py tests/test_romm_tokens_expiry.py tests/test_romm_tokens_secrecy.py
git commit -m "feat(pair): store credentials, and refuse the four times storing is wrong"
```

---

## Task 8: Lift the QR generator

**Files:**
- Create: `qr.py`
- Modify: `cogs/search.py` (lines 386-404, 788-799)
- Test: `tests/test_qr.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `qr.generate_qr(url: str, filename: str = "qr.png") -> discord.File | None`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_qr.py`:

```python
"""QR generation, shared by search downloads and pairing DMs."""

import unittest

from qr import generate_qr


class GenerateQrTests(unittest.TestCase):
    def test_returns_a_discord_file_named_as_asked(self):
        result = generate_qr("https://example.com/pair", filename="pair_qr.png")

        self.assertIsNotNone(result)
        self.assertEqual("pair_qr.png", result.filename)

    def test_default_filename_is_stable(self):
        self.assertEqual("qr.png", generate_qr("https://example.com").filename)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_qr.py -v`
Expected: collection error — `ModuleNotFoundError: No module named 'qr'`.

- [ ] **Step 3: Write `qr.py`**

```python
"""QR generation, owned by neither of its two callers.

cogs/search.py generated these for download links and hard-coded the
attachment filename in two places - the file it built and the
attachment:// URL that referenced it. The pairing DM is the second
consumer, and it needs a different name, so the name became a parameter and
the function moved out of the cog.
"""

import io
import logging
from typing import Optional

import discord
import qrcode

logger = logging.getLogger('romm_bot')


def generate_qr(url: str, filename: str = "qr.png") -> Optional[discord.File]:
    """Render a URL as a PNG attachment.

    The caller's embed must reference the same filename via
    attachment://<filename>, which is why it is a parameter rather than a
    constant: the two ends have to agree.
    """
    try:
        code = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=10,
            border=4,
        )
        code.add_data(url)
        code.make(fit=True)

        image = code.make_image(fill_color="black", back_color="white")
        buffer = io.BytesIO()
        image.save(buffer, format='PNG')
        buffer.seek(0)
        return discord.File(buffer, filename=filename)
    except Exception as e:
        logger.error(f"Error generating QR code: {e}")
        return None
```

- [ ] **Step 4: Point `cogs/search.py` at it**

In `cogs/search.py`, add `from qr import generate_qr` to the imports, then replace the body of the `generate_qr` method (around line 386) with a delegation that keeps the existing filename:

```python
    async def generate_qr(self, url: str) -> discord.File:
        """Render a download link as a QR attachment.

        Kept as a method so the call sites below are unchanged; the drawing
        itself lives in qr.py now, shared with the pairing DM.
        """
        return generate_qr(url, filename="download_qr.png")
```

Leave `embed.set_image(url="attachment://download_qr.png")` at line ~795 alone — it matches the filename passed above. That pairing is the reason the filename is explicit here rather than defaulted.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_qr.py tests/test_search_embed.py tests/test_search_random.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add qr.py cogs/search.py tests/test_qr.py
git commit -m "refactor(qr): lift QR generation out of search, with the filename as a parameter"
```

---

## Task 9: `cogs/pair` — the Discord surface

**Files:**
- Create: `cogs/pair/__init__.py`, `cogs/pair/embeds.py`, `cogs/pair/cog.py`
- Modify: `bot.py` (`core_cogs`, `setup_hook`)
- Modify: `tests/test_import_boundaries.py`
- Test: `tests/test_pair_cog.py`

**Interfaces:**
- Consumes: Task 7 `TokenStore`, Task 6 `DeviceFlow`/`PairOutcome`, Task 8 `generate_qr`.
- Produces: `PairCog` with slash commands `pair`, `unpair`, `pair-status`, `pairings`; `bot.romm_tokens: TokenStore`; `sanitize_device_name(display_name, discord_id) -> str`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_pair_cog.py`:

```python
"""The pairing cog's gating and the one string that reaches RomM's UI.

The device name is rendered on RomM's approve screen, and a Discord display
name is attacker-controlled, so it is the one piece of user input that
crosses into someone else's security decision.
"""

import unittest

from cogs.pair.cog import sanitize_device_name


class DeviceNameTests(unittest.TestCase):
    def test_plain_name_is_kept(self):
        self.assertIn("jake", sanitize_device_name("jake", 111))

    def test_markdown_is_stripped(self):
        name = sanitize_device_name("**Approve** `this`", 111)

        self.assertNotIn("*", name)
        self.assertNotIn("`", name)

    def test_newlines_cannot_forge_extra_lines_on_the_approve_screen(self):
        name = sanitize_device_name("jake\nApproved by RomM", 111)

        self.assertNotIn("\n", name)

    def test_a_name_that_sanitizes_to_nothing_falls_back_to_the_discord_id(self):
        self.assertIn("111", sanitize_device_name("***", 111))

    def test_name_is_capped_for_romms_255_char_column(self):
        self.assertLessEqual(len(sanitize_device_name("x" * 400, 111)), 255)

    def test_name_carries_the_bot_prefix_so_revocation_can_recognise_it(self):
        self.assertTrue(sanitize_device_name("jake", 111).startswith("romm-comm"))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_pair_cog.py -v`
Expected: collection error — `ModuleNotFoundError: No module named 'cogs.pair'`.

- [ ] **Step 3: Write `cogs/pair/embeds.py`**

```python
"""Embeds for the pairing commands.

Every builder here takes an audit row, never a Grant. That is not a
convention: repo.audit_row does not return the sealed blob, so a view
cannot render credential material even by mistake.
"""

from typing import Any, Dict, List, Optional

import discord


def pairing_started_embed(user_code: str, verification_url: str, confirm_code: str) -> discord.Embed:
    embed = discord.Embed(
        title="🔗 Link your RomM account",
        description=(
            f"Open **{verification_url}** and approve the request.\n\n"
            f"If you are asked for a code, it is `{user_code}`."
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="Check before you approve",
        value=(
            f"The request should be named `romm-comm · … · {confirm_code}`.\n"
            "If it shows a different name, someone else started it — deny it."
        ),
        inline=False,
    )
    embed.add_field(
        name="What the bot will be able to do",
        value=(
            "Start and control **your** emulator streaming sessions, and read "
            "your RomM profile. Nothing else."
        ),
        inline=False,
    )
    embed.set_image(url="attachment://pair_qr.png")
    return embed


def pair_status_embed(row: Optional[Dict[str, Any]]) -> discord.Embed:
    if row is None:
        return discord.Embed(
            title="Not linked",
            description="Run `/pair` to link your RomM account.",
            color=discord.Color.light_grey(),
        )

    healthy = not row.get('invalid_since')
    embed = discord.Embed(
        title="🔗 RomM account linked" if healthy else "⚠️ RomM link needs renewing",
        color=discord.Color.green() if healthy else discord.Color.orange(),
    )
    embed.add_field(name="RomM account", value=row['romm_username'], inline=True)
    embed.add_field(name="Permissions", value=", ".join(row['scopes']) or "—", inline=True)
    embed.add_field(name="Linked", value=str(row['created_at'] or "—"), inline=True)
    embed.add_field(name="Expires", value=str(row['expires_at'] or "no expiry set"), inline=True)
    embed.add_field(name="Last used", value=str(row['last_used_at'] or "never"), inline=True)
    embed.add_field(name="Last checked", value=str(row['last_verified_at'] or "never"), inline=True)
    if not healthy:
        embed.add_field(
            name="Needs attention",
            value="This link stopped working. Run `/pair` to renew it.",
            inline=False,
        )
    return embed


def pairings_embed(rows: List[Dict[str, Any]], shared: List[int]) -> discord.Embed:
    embed = discord.Embed(
        title=f"🔗 Linked accounts ({len(rows)})",
        color=discord.Color.blurple(),
    )
    if not rows:
        embed.description = "Nobody has linked a RomM account yet."
        return embed

    lines = []
    for row in rows[:25]:
        flag = "⚠️ " if row.get('invalid_since') else ""
        shared_flag = " · **shared account**" if row['romm_user_id'] in shared else ""
        lines.append(
            f"{flag}<@{row['discord_id']}> → `{row['romm_username']}`"
            f" · used {row['last_used_at'] or 'never'}{shared_flag}"
        )
    embed.description = "\n".join(lines)
    if len(rows) > 25:
        embed.set_footer(text=f"Showing 25 of {len(rows)}")
    return embed
```

- [ ] **Step 4: Write `cogs/pair/cog.py`**

```python
"""The /pair family: enrollment, status, revocation, and the audit view.

Two things here are security-relevant rather than cosmetic.

The device name is the only user-controlled string that reaches RomM's
approve screen, where somebody makes a trust decision about it. A Discord
display name can contain markdown, newlines and anything else, so it is
sanitized rather than interpolated.

The in-flight registry holds device codes in memory and nowhere else. A
device code alone bears the grant, so persisting one would put a
credential-equivalent in the database with none of the protection the
sealed column has.
"""

import asyncio
import logging
import re
import secrets
from typing import Dict, Optional

import discord
from discord.ext import commands, tasks

from admin_checks import is_admin
from cogs.pair import embeds
from qr import generate_qr
from romm_tokens.device_flow import DeviceFlow, PairOutcome
from romm_tokens.store import PairRefusal

logger = logging.getLogger('romm_bot.pair')

# How many pairings may be in flight across the whole guild. device/init is
# unauthenticated, so an ungated /pair turns the bot into a spam relay
# against the RomM instance.
MAX_CONCURRENT_PAIRINGS = 5

REVALIDATE_MINUTES = 60

# Only these survive into the name RomM shows. Everything that could forge
# a second line, or render as formatting, is dropped.
_NAME_SAFE = re.compile(r'[^A-Za-z0-9 _.\-]')


def sanitize_device_name(display_name: str, discord_id: int) -> str:
    """Build the name RomM's approve screen will show.

    Composed by the bot, never taken from the user verbatim: this string is
    what someone reads when deciding whether to hand over a credential.
    """
    cleaned = _NAME_SAFE.sub('', display_name or '').strip()
    cleaned = re.sub(r'\s+', ' ', cleaned)[:80]
    who = cleaned or str(discord_id)
    confirm = secrets.token_hex(2)
    return f"romm-comm - {who} - {confirm}"[:255]


class PairCog(commands.Cog):
    """Discord-facing half of per-user RomM authentication."""

    def __init__(self, bot):
        self.bot = bot
        self.config = bot.config
        self.enabled = bool(getattr(bot.config, 'ROMM_USER_AUTH_ENABLED', False))
        self.store = getattr(bot, 'romm_tokens', None)

        if not self.enabled or self.store is None:
            logger.info("Per-user RomM authentication is disabled; /pair will refuse")
            return

        self.flow = DeviceFlow(bot.romm)
        # discord_id -> asyncio.Task. In memory only: these hold device codes.
        self._in_flight: Dict[int, asyncio.Task] = {}
        self.revalidate_loop.start()

    def cog_unload(self):
        if self.enabled and self.store is not None:
            self.revalidate_loop.cancel()
        for task in list(self._in_flight.values()):
            task.cancel()

    # ------------------------------------------------------------- gating

    async def _refuse_if_unavailable(self, ctx) -> bool:
        if not self.enabled or self.store is None:
            await ctx.respond(
                "Per-user RomM linking is not configured on this server.", ephemeral=True
            )
            return True
        return False

    def _may_pair(self, member: discord.Member) -> bool:
        role_id = self.config.ROMM_PAIR_ROLE_ID
        if not role_id:
            return True
        return any(str(role.id) == str(role_id) for role in getattr(member, 'roles', []))

    # ------------------------------------------------------------ commands

    @discord.slash_command(name="pair", description="Link your RomM account to the bot")
    async def pair(self, ctx: discord.ApplicationContext):
        if await self._refuse_if_unavailable(ctx):
            return
        if not self._may_pair(ctx.author):
            await ctx.respond("You do not have the role needed to link an account.", ephemeral=True)
            return
        if len(self._in_flight) >= MAX_CONCURRENT_PAIRINGS:
            await ctx.respond("Too many links are being set up right now. Try again shortly.",
                              ephemeral=True)
            return

        discord_id = ctx.author.id
        self._cancel_in_flight(discord_id)

        display = sanitize_device_name(getattr(ctx.author, 'display_name', ''), discord_id)
        confirm = display.rsplit('- ', 1)[-1]

        request = await self.flow.init(f"romm-comm:{discord_id}", display)
        if request is None:
            await ctx.respond("RomM refused to start the link. Ask an admin to check the logs.",
                              ephemeral=True)
            return

        embed = embeds.pairing_started_embed(request.user_code, request.verification_url, confirm)
        qr_file = generate_qr(request.verification_url, filename="pair_qr.png")

        try:
            channel = await ctx.author.create_dm()
            message = await channel.send(embed=embed, file=qr_file)
            await ctx.respond("Check your DMs to finish linking.", ephemeral=True)
        except discord.Forbidden:
            # An ephemeral reply is the user's own screen, so it is an
            # acceptable fallback for a blocked DM.
            message = None
            await ctx.respond(embed=embed, file=qr_file, ephemeral=True)

        self._in_flight[discord_id] = asyncio.create_task(
            self._await_approval(discord_id, request, message)
        )

    def _cancel_in_flight(self, discord_id: int) -> None:
        task = self._in_flight.pop(discord_id, None)
        if task is not None and not task.done():
            task.cancel()

    async def _await_approval(self, discord_id, request, message) -> None:
        """Poll to a conclusion, then re-check everything the gate checked."""
        try:
            result = await self.flow.poll(request)

            if result.outcome is not PairOutcome.APPROVED:
                await self._tell(message, discord_id, {
                    PairOutcome.DENIED: "Link declined. Nothing was saved.",
                    PairOutcome.EXPIRED: "That link request expired. Run `/pair` to try again.",
                    PairOutcome.ERROR: "RomM gave an unexpected answer. Ask an admin to check.",
                }[result.outcome])
                return

            if not await self._still_eligible(discord_id):
                # Approval can arrive up to ten minutes after the gate ran.
                logger.warning(f"Discarding a pairing for {discord_id}: no longer eligible")
                return

            grant, refusal = await self.store.complete_pair(discord_id, result.payload)
            if refusal is not None:
                await self._tell(message, discord_id, self._refusal_text(refusal))
                return

            await self._tell(
                message, discord_id,
                f"✅ Linked to RomM account `{grant.romm_username}`."
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Pairing for {discord_id} failed: {e}", exc_info=True)
        finally:
            self._in_flight.pop(discord_id, None)

    async def _still_eligible(self, discord_id: int) -> bool:
        guild = self.bot.get_guild(self.config.GUILD_ID)
        member = guild.get_member(discord_id) if guild else None
        return bool(member) and self._may_pair(member) and self.enabled

    @staticmethod
    def _refusal_text(refusal: PairRefusal) -> str:
        return {
            PairRefusal.SCOPE_SHORTFALL:
                "That approval did not grant permission to control streaming sessions, "
                "so it was discarded. Approve both permissions and try again.",
            PairRefusal.IDENTITY_MISMATCH:
                "You approved with a different RomM account than the one linked to you. "
                "Nothing was saved — tell an admin if that is unexpected.",
            PairRefusal.LOOKUP_FAILED:
                "The bot could not check your account records, so nothing was saved. "
                "Try again shortly.",
            PairRefusal.ALREADY_PAIRED_ELSEWHERE:
                "That RomM account is already linked to another Discord user.",
            PairRefusal.UNREADABLE:
                "The bot could not read your RomM profile, so nothing was saved.",
        }[refusal]

    async def _tell(self, message, discord_id: int, text: str) -> None:
        try:
            if message is not None:
                await message.edit(content=text, embed=None, attachments=[])
                return
            user = self.bot.get_user(discord_id)
            if user:
                await user.send(text)
        except discord.HTTPException as e:
            logger.warning(f"Could not deliver pairing result to {discord_id}: {e}")

    @discord.slash_command(name="unpair", description="Remove your RomM account link")
    async def unpair(self, ctx: discord.ApplicationContext):
        if await self._refuse_if_unavailable(ctx):
            return

        self._cancel_in_flight(ctx.author.id)
        row = await self.store.repo.audit_row(ctx.author.id)
        revoked = await self.store.unpair(ctx.author.id)

        if row is None:
            await ctx.respond("You do not have a linked RomM account.", ephemeral=True)
            return

        if revoked:
            await ctx.respond("✅ Unlinked, and the access token was revoked in RomM.",
                              ephemeral=True)
        else:
            await ctx.respond(
                "✅ Unlinked. The bot has forgotten your token, but it still exists in RomM — "
                f"delete `{row['device_id']}` at "
                f"{self.config.ROMM_PAIR_BASE_URL}/client-api-tokens",
                ephemeral=True,
            )

    @discord.slash_command(name="pair-status", description="Show your RomM account link")
    async def pair_status(self, ctx: discord.ApplicationContext):
        if await self._refuse_if_unavailable(ctx):
            return
        row = await self.store.repo.audit_row(ctx.author.id)
        await ctx.respond(embed=embeds.pair_status_embed(row), ephemeral=True)

    @discord.slash_command(name="pairings", description="Admin: who has linked a RomM account")
    @is_admin()
    async def pairings(self, ctx: discord.ApplicationContext):
        if await self._refuse_if_unavailable(ctx):
            return
        rows = await self.store.repo.list_audit()
        seen, shared = set(), []
        for row in rows:
            if row['romm_user_id'] in seen:
                shared.append(row['romm_user_id'])
            seen.add(row['romm_user_id'])
        await ctx.respond(embed=embeds.pairings_embed(rows, shared), ephemeral=True)

    # ------------------------------------------------------------ lifecycle

    @tasks.loop(minutes=REVALIDATE_MINUTES)
    async def revalidate_loop(self):
        """Ask RomM whether each stored credential still works."""
        try:
            rows = await self.store.repo.list_audit()
        except Exception as e:
            logger.error(f"Could not list pairings to revalidate: {e}")
            return

        for row in rows:
            if row.get('invalid_since'):
                continue
            await self._revalidate_one(row['discord_id'])

    async def _revalidate_one(self, discord_id: int) -> None:
        acting = await self.store.acting_client(discord_id)
        if acting is None:
            return
        try:
            await acting.request("GET", "/api/users/me")
        except Exception as e:
            # ActingClient already invalidated on 401/403 via the callback.
            # Everything else - a timeout, a restart, a 502 - must leave the
            # row alone: marking every token dead during a RomM restart
            # would be the worst bug this loop could have.
            logger.debug(f"Revalidation for {discord_id} did not complete: {e}")
            return

        grant = await self.store.get_grant(discord_id)
        if grant is not None:
            await self.store.repo.touch_verified(discord_id, grant.generation)

    @revalidate_loop.before_loop
    async def before_revalidate_loop(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        """A departed member's live credential is the worst one to leave."""
        if not self.enabled or self.store is None:
            return
        self._cancel_in_flight(member.id)
        if await self.store.repo.audit_row(member.id):
            await self.store.unpair(member.id)
            logger.info(f"Removed RomM pairing for departed member {member.id}")

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        """Losing the pairing role unpairs, the same as leaving."""
        if not self.enabled or self.store is None:
            return
        if self._may_pair(before) and not self._may_pair(after):
            self._cancel_in_flight(after.id)
            if await self.store.repo.audit_row(after.id):
                await self.store.unpair(after.id)
                logger.info(f"Removed RomM pairing for {after.id} after role loss")


def setup(bot):
    bot.add_cog(PairCog(bot))
```

- [ ] **Step 5: Write `cogs/pair/__init__.py`**

```python
"""Discord surface for per-user RomM authentication.

Custody itself lives in romm_tokens/, which this package and
integrations/romm_streaming.py both depend on. Nothing depends on this
package, which is what keeps the layering one-directional.
"""

from cogs.pair.cog import setup

__all__ = ["setup"]
```

- [ ] **Step 6: Wire the store and register the cog**

In `bot.py`, in `RommBot.__init__`, after `self.platform_emoji = PlatformEmoji(self)`:

```python
        # Custody of per-user RomM credentials. Built in setup_hook, once the
        # database exists; None until then, and None forever when the feature
        # is off or its key is unusable.
        self.romm_tokens = None
```

In `setup_hook`, after the database verification block and before the SocketIO block:

```python
            if self.config.ROMM_USER_AUTH_ENABLED:
                try:
                    from romm_tokens import TokenStore
                    self.romm_tokens = TokenStore(self.db, self.config, self.romm)
                    # One-shot pass; a no-op unless ROMM_TOKEN_KEY_OLD is set.
                    await self.romm_tokens.reseal_under_current_key()
                    logger.info("✅ Per-user RomM authentication enabled")
                except ValueError as e:
                    # A bad key disables the feature; it does not take the
                    # bot down over something optional.
                    logger.error(f"Per-user RomM authentication disabled: {e}")
                    self.config.ROMM_USER_AUTH_ENABLED = False
```

In `load_all_cogs`, add to `core_cogs` after `'cogs.netplay'`:

```python
            'cogs.pair'
```

The list stays unconditional — `test_extension_loading.py` reads it by AST — and `PairCog` refuses at runtime when the feature is off, following the `REQUESTS_ENABLED` pattern.

- [ ] **Step 7: Widen the import boundary scan**

In `tests/test_import_boundaries.py`, add:

```python
    def test_shared_packages_do_not_import_from_cogs(self):
        """Layering: cogs and integrations depend on romm_tokens, not the reverse.

        integrations/romm_streaming.py needs to resolve a Discord id to a
        credential. If custody lived under cogs/, that would be an
        integration importing a cog.
        """
        offenders = []
        roots = list(Path("integrations").glob("*.py")) + list(Path("romm_tokens").glob("*.py"))
        for path in roots:
            text = path.read_text(encoding="utf-8")
            if "from cogs" in text or "import cogs" in text:
                offenders.append(str(path))

        self.assertEqual([], offenders)

    def test_only_the_token_package_names_the_sealed_column(self):
        """Ciphertext must be unreachable from anything that renders."""
        offenders = []
        for path in Path(".").rglob("*.py"):
            parts = path.parts
            if parts[0] in {".git", "tests", "tools", "romm_tokens"}:
                continue
            if "database_manager.py" in parts:
                continue  # owns the DDL
            if "sealed" in path.read_text(encoding="utf-8"):
                offenders.append(str(path))

        self.assertEqual([], offenders)

    def test_the_pair_cog_does_not_reach_into_user_manager(self):
        text = Path("cogs/pair/cog.py").read_text(encoding="utf-8")

        self.assertNotIn("user_manager", text)
```

- [ ] **Step 8: Run the tests**

Run: `python -m pytest tests/test_pair_cog.py tests/test_import_boundaries.py tests/test_extension_loading.py -v`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add cogs/pair/ bot.py tests/test_pair_cog.py tests/test_import_boundaries.py
git commit -m "feat(pair): add the /pair command family and its lifecycle listeners"
```

---

## Task 10: Lifecycle ordering — cancellation, re-check, and the generation guard end to end

**Files:**
- Test: `tests/test_romm_tokens_lifecycle.py`
- Modify: `cogs/pair/cog.py` only if a test exposes a gap

**Interfaces:**
- Consumes: Tasks 7 and 9.
- Produces: no new interface — this task proves the ordering hazards are closed.

- [ ] **Step 1: Write the tests**

Create `tests/test_romm_tokens_lifecycle.py`:

```python
"""The four ordering hazards, each as a named scenario.

None of these are hypothetical. Each is a window that exists because
approval arrives up to ten minutes after the command, and because a
revalidation probe can outlive the credential it was checking.
"""

import asyncio
import base64
import os
import tempfile
import unittest

from database_manager import MasterDatabase
from romm_tokens.store import TokenStore

KEY = base64.b64encode(b"L" * 32).decode()

PAYLOAD = {
    "access_token": "rmm_user_token",
    "device_id": "dev-1",
    "scopes": ["me.read", "roms.user.write"],
    "expires_at": None,
}


class _Config:
    ROMM_TOKEN_KEY = KEY
    ROMM_TOKEN_KEY_FILE = None
    ROMM_PAIR_MAX_AGE_DAYS = 90
    API_BASE_URL = "https://romm.example"


class _Acting:
    async def request(self, method, path, **kwargs):
        if path == "/api/users/me":
            return 200, {"id": 7, "username": "idiosync"}
        return 200, []


class _Romm:
    config = _Config()

    def acting_as(self, grant, on_auth_failure=None):
        return _Acting()

    async def make_authenticated_request(self, method, endpoint, **kwargs):
        return {}


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = MasterDatabase(db_path=os.path.join(self._dir.name, "t.db"))
        asyncio.run(self.db.initialize())
        self.store = TokenStore(self.db, _Config(), _Romm())

    def tearDown(self):
        self._dir.cleanup()

    def pair(self, discord_id=111):
        return asyncio.run(self.store.complete_pair(discord_id, dict(PAYLOAD)))[0]

    def test_a_stale_probe_cannot_invalidate_the_credential_that_replaced_it(self):
        old = self.pair()          # generation 1: the probe reads this
        self.pair()                # generation 2: a re-pair lands mid-probe

        asyncio.run(self.store.invalidate(old))

        self.assertIsNotNone(asyncio.run(self.store.get_grant(111)))

    def test_invalidating_the_current_credential_works(self):
        grant = self.pair()

        asyncio.run(self.store.invalidate(grant))

        self.assertIsNone(asyncio.run(self.store.get_grant(111)))

    def test_unpair_removes_the_row_entirely(self):
        self.pair()

        asyncio.run(self.store.unpair(111))

        self.assertIsNone(asyncio.run(self.store.repo.audit_row(111)))

    def test_every_deferred_write_is_generation_guarded(self):
        old = self.pair()
        self.pair()

        self.assertFalse(asyncio.run(self.store.repo.touch_verified(111, old.generation)))
        self.assertFalse(asyncio.run(self.store.repo.touch_used(111, old.generation)))
        self.assertFalse(asyncio.run(self.store.repo.mark_expiry_warned(111, old.generation)))
        self.assertFalse(asyncio.run(self.store.repo.mark_invalid(111, old.generation)))
```

- [ ] **Step 2: Run the tests**

Run: `python -m pytest tests/test_romm_tokens_lifecycle.py -v`
Expected: PASS against Tasks 7 and 9 as written. If any fail, the gap is in `store.py` or `repo.py`, not in the test — fix the source.

- [ ] **Step 3: Commit**

```bash
git add tests/test_romm_tokens_lifecycle.py
git commit -m "test(pair): pin the four credential ordering hazards"
```

---

## Task 11: Streaming plumbing — preserve `ClaimOutcome.DENIED`

**Files:**
- Modify: `integrations/romm_streaming.py` (lines 55-60, 126-177, 195-215)
- Test: `tests/test_streaming_auth_outcome.py`

**Interfaces:**
- Consumes: Task 5 `ActingClient`, Task 7 `TokenStore.acting_client`.
- Produces: `RommStreaming._send(method, path, json_body=None, timeout=..., grant=None)`; `claim(rom_id, grant)`; `release(platform, grant=None)`; `save_and_exit(platform, slot=None, grant=None)`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_streaming_auth_outcome.py`:

```python
"""A revoked credential must read as DENIED, not as an outage.

_send catches Exception and returns status 0, which is absent from
_CLAIM_OUTCOMES and so degrades to ClaimOutcome.ERROR - "the broker is
down, tell an admin". For a revoked user token the truth is DENIED: tell
that user to re-pair. The queue would otherwise report an outage and retry
against a credential that will never work again.
"""

import asyncio
import unittest

from integrations.romm_streaming import ClaimOutcome, RommStreaming
from romm_client import RommAuthError


class _Acting:
    def __init__(self, error):
        self.error = error
        self.calls = 0

    async def request(self, method, path, **kwargs):
        self.calls += 1
        raise self.error


class _Store:
    def __init__(self, acting):
        self._acting = acting

    async def acting_client(self, discord_id):
        return self._acting


class _Grant:
    discord_id = 111
    generation = 1
    token = "rmm_user_token"


def _cog(acting):
    cog = object.__new__(RommStreaming)
    cog.enabled = True
    cog.server_enabled = True
    cog.romm = None
    cog.store = _Store(acting)
    return cog


class ClaimOutcomeTests(unittest.TestCase):
    def test_an_auth_error_becomes_denied_not_error(self):
        acting = _Acting(RommAuthError("revoked"))
        cog = _cog(acting)

        result = asyncio.run(cog.claim(42, _Grant()))

        self.assertIs(ClaimOutcome.DENIED, result.outcome)

    def test_a_transport_failure_still_reads_as_broker_trouble(self):
        cog = _cog(_Acting(OSError("connection refused")))

        result = asyncio.run(cog.claim(42, _Grant()))

        self.assertIs(ClaimOutcome.ERROR, result.outcome)

    def test_an_auth_error_is_not_retried(self):
        acting = _Acting(RommAuthError("revoked"))
        cog = _cog(acting)

        asyncio.run(cog.claim(42, _Grant()))

        self.assertEqual(1, acting.calls)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_streaming_auth_outcome.py -v`
Expected: FAIL — `claim()` takes one argument, and `RommAuthError` is swallowed into `ERROR`.

- [ ] **Step 3: Rewrite `_send` to take a grant**

In `integrations/romm_streaming.py`, replace the whole `_send` method with:

```python
    async def _send(
        self,
        method: str,
        path: str,
        json_body: Optional[Dict[str, Any]] = None,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        grant=None,
    ) -> tuple[int, Optional[Dict[str, Any]]]:
        """Make a call as the bot, or as a specific user when given a grant.

        Status 0 means the request never reached RomM, so callers can treat
        it like a 5xx without catching anything.

        RommAuthError is caught ahead of the general handler on purpose. It
        is an Exception, so the catch-all below would turn a revoked
        credential into status 0 and therefore ClaimOutcome.ERROR - an
        outage report for a user who simply needs to pair again.
        """
        try:
            if grant is not None:
                acting = await self.store.acting_client(grant.discord_id)
                if acting is None:
                    return 401, None
                return await acting.request(method, path, json=json_body, timeout=timeout)

            if not await self.romm.ensure_valid_token():
                logger.error("No valid RomM token for streaming request")
                return 401, None

            session = await self.romm.ensure_session()
            url = f"{self.romm.config.API_BASE_URL}{path}"
            headers = await self.romm._auth_header()

            kwargs: Dict[str, Any] = {
                "headers": headers,
                "timeout": aiohttp.ClientTimeout(total=timeout),
            }
            if json_body is not None:
                kwargs["json"] = json_body

            async with session.request(method, url, **kwargs) as response:
                try:
                    body = await response.json()
                except Exception:
                    body = None
                logger.debug(f"streaming: {method} {path} -> {response.status}")
                return response.status, body

        except RommAuthError:
            # The store's callback has already invalidated the credential.
            logger.info(f"Streaming {method} {path} denied: the acting credential is dead")
            return 401, None
        except Exception as e:
            logger.error(f"Streaming request {method} {path} failed: {e}")
            return 0, None
```

Add to the imports at the top of the file:

```python
from romm_client import RommAuthError
```

- [ ] **Step 4: Thread the grant through the write methods**

Replace the signatures and `_send` calls for the acting methods:

```python
    async def claim(self, rom_id: int, grant) -> ClaimResult:
        """POST /api/streaming/sessions - start a session for a ROM.

        Takes a grant because RomM binds the session to whoever claimed it.
        Claiming as the bot is the bug per-user auth exists to remove, so
        there is no default here: a caller without a grant cannot claim.
        """
        status, body = await self._send(
            "POST",
            "/api/streaming/sessions",
            json_body={"rom_id": rom_id},
            timeout=CLAIM_TIMEOUT_SECONDS,
            grant=grant,
        )
        outcome = _CLAIM_OUTCOMES.get(status, ClaimOutcome.ERROR)
        if outcome is ClaimOutcome.ERROR:
            logger.error(f"Unexpected claim status {status} for rom {rom_id}: {body}")
        return ClaimResult(outcome=outcome, body=body)

    async def release(self, platform: str, grant=None) -> bool:
        """DELETE /api/streaming/sessions/{platform} - stop the emulator.

        grant=None runs it as the bot, which is the admin force-reclaim
        path. Pass the owner's grant for an ordinary release.
        """
        status, _ = await self._send(
            "DELETE", f"/api/streaming/sessions/{platform}", grant=grant
        )
        return status in (200, 204)

    async def save_and_exit(self, platform: str, slot: Optional[int] = None, grant=None) -> bool:
        body: Dict[str, Any] = {"wait": True}
        if slot is not None:
            body["slot"] = slot
        status, _ = await self._send(
            "POST",
            f"/api/streaming/sessions/{platform}/save-and-exit",
            json_body=body,
            timeout=SAVE_AND_EXIT_TIMEOUT_SECONDS,
            grant=grant,
        )
        return status in (200, 204)

    async def save_state(self, platform: str, slot: Optional[int] = None, grant=None) -> bool:
        status, _ = await self._send(
            "POST",
            f"/api/streaming/sessions/{platform}/save-state",
            json_body={} if slot is None else {"slot": slot},
            grant=grant,
        )
        return status in (200, 204)

    async def load_state(self, platform: str, slot: Optional[int] = None, grant=None) -> bool:
        status, _ = await self._send(
            "POST",
            f"/api/streaming/sessions/{platform}/load-state",
            json_body={} if slot is None else {"slot": slot},
            grant=grant,
        )
        return status in (200, 204)
```

Leave `get_config`, `list_sessions` and `force_release_all` without a grant: those are reads or admin actions and stay on the bot token.

Update `ClaimOutcome.DENIED`'s comment to:

```python
    DENIED = "denied"                # 401/403 - the acting user's credential is revoked, expired or under-scoped
```

In `RommStreaming.__init__`, add:

```python
        self.store = getattr(bot, 'romm_tokens', None)
```

- [ ] **Step 5: Run the tests**

Run: `python -m pytest tests/test_streaming_auth_outcome.py -v`
Expected: PASS (3 tests).

- [ ] **Step 6: Commit**

```bash
git add integrations/romm_streaming.py tests/test_streaming_auth_outcome.py
git commit -m "feat(streaming): claim as the acting user, and keep DENIED distinct from an outage"
```

---

## Task 12: Documentation and the full-suite gate

**Files:**
- Modify: `README.md`
- Test: the whole suite

- [ ] **Step 1: Document the feature**

Add to `README.md`, after the `ROMM_CLIENT_TOKEN` bullet in the configuration list:

```markdown
### Per-user RomM linking (optional)

Set `ROMM_USER_AUTH_ENABLED=true` to let members link their own RomM account with `/pair`. The bot then acts **as that member** for the few RomM calls that bind a resource to its owner — the first being emulator streaming, where a session is owned by whoever claimed it and only that owner or an admin can control it. Without this, the bot claims sessions as itself and the member cannot touch their own session in RomM's web UI.

**What the bot stores.** One access token per linked member, encrypted with AES-256-GCM and bound to that member's Discord ID, so a row copied onto another ID in the database file will not open. Set the key with `ROMM_TOKEN_KEY_FILE` (preferred — a Docker secret or read-only mount does not appear in `docker inspect`) or `ROMM_TOKEN_KEY` (base64, 32 bytes). Without a key the feature refuses to start rather than storing anything in the clear.

**What that encryption does and does not buy.** It protects a copied `data/` directory, a backup, a volume snapshot, an accidentally committed database. It does **not** protect against host compromise: anything that can read the process environment or memory can read the key.

**What a linked token can do.** Exactly two scopes, `me.read` and `roms.user.write`. In RomM 5.2 that means: start, stop and control streaming sessions (including ending *all* sessions — something that member can already do from RomM's web UI); write, edit and delete their own ROM notes; overwrite their own rating, difficulty, completion and status; create and delete their own play-session history; and read their own profile, which includes their email address. It cannot delete ROMs, change passwords, or read other users' data.

**Expiry.** RomM's device grant produces tokens that never expire by default, and the bot cannot ask for a shorter life — only the approving user can. So the bot enforces its own bound, `ROMM_PAIR_MAX_AGE_DAYS` (default 90), after which a member is asked to link again. Set it to `0` to disable.

**Key rotation.** Set `ROMM_TOKEN_KEY_OLD` to the previous key alongside the new one and restart; stored credentials are re-sealed under the new key, after which the old variable can be removed. Rotating *without* it is supported but lossy — rows sealed under a key that is no longer configured are marked invalid and those members are asked to link again.

**Revocation.** `/unpair` forgets the credential and revokes it in RomM using the bot's own admin permission, so the member's token needs no extra scope for it. Members can also revoke at `<your RomM URL>/client-api-tokens`. Admins see every link with `/pairings`.

**Requires** the bot's own RomM credential to hold `users.write` (for revocation) and `roms.user.write` (for admin force-reclaim of a session).
```

- [ ] **Step 2: Run the whole suite**

Run: `python -m pytest -v`
Expected: PASS. Every pre-existing test must still pass — `test_romm_client.py`, `test_bot_auth.py`, `test_extension_loading.py` and the two boundary tests are the regression gates for the four files this plan modified.

- [ ] **Step 3: Lint everything touched**

Run: `python -m ruff check bot.py romm_client.py database_manager.py qr.py romm_tokens/ cogs/pair/ integrations/romm_streaming.py tests/`
Expected: `All checks passed!`

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: describe per-user RomM linking, and what the stored token can do"
```

---

## Deferred, deliberately

- **Streaming queue cog.** The consumer this exists for. Blocked on the dev instance having zero emulator containers (`GET /api/streaming/config` reports `enabled: false`), so spec verifications 3 and 7 could not be answered: whether `GET /api/streaming/sessions` identifies the holding user, and whether a `roms.user.write` token can release a session it does not own. Both must be settled before that cog is planned.
- **Expiry warning DMs** at 7 and 1 days. `expiry_warned_at` and `mark_expiry_warned` exist and are tested; nothing sends the DM yet. RomM's default of no expiry means the ladder would rarely fire, and `ROMM_PAIR_MAX_AGE_DAYS` covers the case that matters. Worth adding when there is evidence of a deployment that sets expiries.
- **`/pairings` reconcile against `GET /api/client-tokens/all`.** The audit view lists what the bot knows; reconciliation against what RomM knows would catch credentials orphaned before supersede-and-revoke existed. One admin command, no new plumbing.
