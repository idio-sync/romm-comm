import asyncio
import unittest

from cogs.igdb_client import IGDBClient


class _FakeTokenResponse:
    status = 200

    async def json(self):
        return {"access_token": "tok", "expires_in": 5000}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """Counts how many times the Twitch token endpoint is POSTed to."""

    def __init__(self, counter):
        self.counter = counter
        self.closed = False

    def post(self, url, params=None, **kwargs):
        self.counter[0] += 1
        return _FakeTokenResponse()


def _make_client():
    # Bypass __init__ (it requires real IGDB env credentials and raises otherwise).
    client = object.__new__(IGDBClient)
    client.client_id = "id"
    client.client_secret = "secret"
    client.access_token = None
    client.token_expires = None
    client._token_lock = asyncio.Lock()
    return client


class IGDBTokenLockTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_token_requests_refresh_only_once(self):
        # Ten coroutines all needing a token at once must trigger exactly ONE
        # Twitch token POST, not ten (concurrent refreshes would race and
        # invalidate each other's client-credentials token).
        client = _make_client()
        counter = [0]
        fake_session = _FakeSession(counter)

        async def ensure_session():
            # Yield control so the 10 callers genuinely interleave between the
            # token check and the POST (mimicking real network I/O). Without the
            # refresh lock this lets all 10 pass the check and each POST.
            await asyncio.sleep(0)
            return fake_session

        client.ensure_session = ensure_session

        results = await asyncio.gather(*(client.get_access_token() for _ in range(10)))

        self.assertTrue(all(results))
        self.assertEqual("tok", client.access_token)
        self.assertEqual(1, counter[0])

    async def test_valid_token_short_circuits_without_posting(self):
        # A cached, unexpired token must not hit Twitch at all.
        from datetime import datetime, timedelta

        client = _make_client()
        client.access_token = "cached"
        client.token_expires = datetime.now() + timedelta(hours=1)
        counter = [0]
        fake_session = _FakeSession(counter)

        async def ensure_session():
            return fake_session

        client.ensure_session = ensure_session

        self.assertTrue(await client.get_access_token())
        self.assertEqual(0, counter[0])
