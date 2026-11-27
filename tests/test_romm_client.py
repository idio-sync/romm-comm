"""Tests for RommClient's caching and retry behaviour.

fetch_api_endpoint decides how hard to try. A revoked token and a flaky
connection both look like "no data" to the caller, but only one is worth
retrying, and getting that wrong means either hammering a server that will
keep saying no or giving up on a blip.

None of this needed a Discord bot to test once the client stopped being part
of one.
"""

import unittest
from types import SimpleNamespace

from romm_client import (
    BASE_OAUTH_SCOPES,
    OPTIONAL_OAUTH_SCOPES,
    RommApiError,
    RommAuthError,
    RommClient,
    parse_romm_version,
)


class FakeResponse:
    def __init__(self, status, payload=None, text=""):
        self.status = status
        self.headers = {"content-type": "application/json"}
        self._payload = payload if payload is not None else {}
        self._text = text

    async def json(self):
        return self._payload

    async def text(self):
        return self._text


class FakeRequestContext:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeSession:
    """Hands out canned responses, or raises them if they are exceptions."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def get(self, url, **kwargs):
        self.requests.append(url)
        return FakeRequestContext(self.responses.pop(0))


class FakeConfig:
    API_BASE_URL = "https://romm.example"
    ROMM_CLIENT_TOKEN = "rmm_clienttoken"
    CACHE_TTL = 3600
    API_TIMEOUT = 30


def make_client(responses):
    client = RommClient(FakeConfig())
    client.session = FakeSession(responses)
    client.sleeps = []

    async def fake_ensure_session():
        return client.session

    client.ensure_session = fake_ensure_session
    return client


class NoSleep:
    """Record backoff delays instead of waiting them out."""

    def __init__(self, client):
        self.client = client

    def __enter__(self):
        import romm_client

        self.original = romm_client.asyncio.sleep

        async def fake_sleep(seconds):
            self.client.sleeps.append(seconds)

        romm_client.asyncio.sleep = fake_sleep
        return self

    def __exit__(self, *exc):
        import romm_client

        romm_client.asyncio.sleep = self.original
        return False


class FetchEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_successful_fetch_returns_and_caches_the_payload(self):
        client = make_client([FakeResponse(200, {"roms": 3})])

        result = await client.fetch_api_endpoint("stats")

        self.assertEqual({"roms": 3}, result)
        self.assertEqual({"roms": 3}, client.cache.get("stats"))

    async def test_a_cached_endpoint_is_not_requested_again(self):
        client = make_client([FakeResponse(200, {"roms": 3})])
        await client.fetch_api_endpoint("stats")

        result = await client.fetch_api_endpoint("stats")

        self.assertEqual({"roms": 3}, result)
        self.assertEqual(1, len(client.session.requests))

    async def test_bypass_cache_makes_a_fresh_request(self):
        client = make_client([FakeResponse(200, {"roms": 3}), FakeResponse(200, {"roms": 4})])
        await client.fetch_api_endpoint("stats")

        result = await client.fetch_api_endpoint("stats", bypass_cache=True)

        self.assertEqual({"roms": 4}, result)
        self.assertEqual(2, len(client.session.requests))

    async def test_an_auth_failure_is_not_retried(self):
        """401 means the credentials are wrong; asking again will not help."""
        client = make_client([FakeResponse(401, text="unauthorized")])

        with self.assertLogs("romm_bot", level="ERROR"):
            result = await client.fetch_api_endpoint("stats")

        self.assertIsNone(result)
        self.assertEqual(1, len(client.session.requests))

    async def test_a_forbidden_response_is_not_retried_either(self):
        client = make_client([FakeResponse(403, text="forbidden")])

        with self.assertLogs("romm_bot", level="ERROR"):
            result = await client.fetch_api_endpoint("stats")

        self.assertIsNone(result)
        self.assertEqual(1, len(client.session.requests))

    async def test_a_server_error_is_retried_up_to_the_limit(self):
        client = make_client([FakeResponse(500, text="boom")] * 3)

        with self.assertLogs("romm_bot", level="ERROR"):
            result = await client.fetch_api_endpoint("stats", max_retries=2)

        self.assertIsNone(result)
        self.assertEqual(3, len(client.session.requests))

    async def test_a_server_error_that_recovers_returns_the_payload(self):
        client = make_client([FakeResponse(500, text="boom"), FakeResponse(200, {"ok": True})])

        with self.assertLogs("romm_bot", level="ERROR"):
            result = await client.fetch_api_endpoint("stats")

        self.assertEqual({"ok": True}, result)

    async def test_a_timeout_is_retried_with_exponential_backoff(self):
        client = make_client([TimeoutError(), TimeoutError(), FakeResponse(200, {"ok": True})])

        with NoSleep(client):
            result = await client.fetch_api_endpoint("stats")

        self.assertEqual({"ok": True}, result)
        self.assertEqual([1, 2], client.sleeps)

    async def test_repeated_timeouts_give_up(self):
        client = make_client([TimeoutError()] * 3)

        with NoSleep(client), self.assertLogs("romm_bot", level="ERROR"):
            result = await client.fetch_api_endpoint("stats", max_retries=2)

        self.assertIsNone(result)
        self.assertEqual([1, 2], client.sleeps)

    async def test_a_failed_token_grant_gives_up_immediately(self):
        client = make_client([FakeResponse(200, {"ok": True})])

        async def no_token():
            return False

        client.ensure_valid_token = no_token

        with self.assertLogs("romm_bot", level="ERROR"):
            result = await client.fetch_api_endpoint("stats")

        self.assertIsNone(result)
        self.assertEqual([], client.session.requests)

    async def test_unparseable_json_on_a_200_is_not_retried(self):
        """A malformed body is a server-side problem, not a transient one."""
        import json

        class BadJson(FakeResponse):
            async def json(self):
                raise json.JSONDecodeError("bad", "", 0)

        client = make_client([BadJson(200)])

        with self.assertLogs("romm_bot", level="ERROR"):
            result = await client.fetch_api_endpoint("stats")

        self.assertIsNone(result)
        self.assertEqual(1, len(client.session.requests))


class ErrorTypeTests(unittest.TestCase):
    def test_auth_errors_are_api_errors(self):
        """So a caller that only cares about failure can catch the base type."""
        self.assertTrue(issubclass(RommAuthError, RommApiError))


class ClientTokenTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_client_token_is_used_without_an_oauth_grant(self):
        client = RommClient(FakeConfig())

        self.assertTrue(await client.ensure_valid_token())
        self.assertEqual("rmm_clienttoken", client.access_token)


class ParseRommVersionTests(unittest.TestCase):
    def test_a_release(self):
        self.assertEqual((5, 4, 1), parse_romm_version("5.4.1"))

    def test_a_pre_release_reads_as_its_release(self):
        self.assertEqual((5, 5, 0), parse_romm_version("5.5.0-beta.1"))

    def test_a_leading_v_is_tolerated(self):
        self.assertEqual((5, 2, 0), parse_romm_version("v5.2.0"))

    def test_development_and_junk_are_unknown(self):
        for raw in ("development", "", None, 5, "5.4"):
            with self.subTest(raw=raw):
                self.assertIsNone(parse_romm_version(raw))


class GetServerVersionTests(unittest.IsolatedAsyncioTestCase):
    async def version_from(self, body):
        client = object.__new__(RommClient)
        calls = []

        async def make_authenticated_request(method, endpoint, **kwargs):
            calls.append((method, endpoint))
            return body

        client.make_authenticated_request = make_authenticated_request
        version = await client.get_server_version()
        self.assertEqual([("GET", "heartbeat")], calls)
        return version

    async def test_reads_system_version_from_the_heartbeat(self):
        self.assertEqual((5, 4, 1), await self.version_from({"SYSTEM": {"VERSION": "5.4.1"}}))

    async def test_a_failed_heartbeat_is_unknown(self):
        self.assertIsNone(await self.version_from(None))

    async def test_a_heartbeat_without_system_is_unknown(self):
        self.assertIsNone(await self.version_from({"OTHER": {}}))


class TokenResponse:
    def __init__(self, status, text, body=None):
        self.status = status
        self._text = text
        self._body = body
        self.headers = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def text(self):
        return self._text

    async def json(self, content_type="application/json"):
        return self._body


class TokenSession:
    """Answers /api/token by the scopes asked for, recording each request."""

    def __init__(self, answer):
        self.answer = answer
        self.scopes_asked = []

    def post(self, url, data=None, headers=None, json=None):
        # aiohttp keeps form fields as (headers-with-name, extra, value).
        fields = {headers["name"]: value for headers, _, value in data._fields}
        scope = fields["scope"]
        self.scopes_asked.append(scope)
        return self.answer(scope)


def token_client(answer):
    client = object.__new__(RommClient)
    client.config = SimpleNamespace(API_BASE_URL="http://romm", USER="bot", PASS="pw")
    session = TokenSession(answer)

    async def ensure_session():
        return session

    client.ensure_session = ensure_session
    return client, session


GRANTED = {"access_token": "a", "refresh_token": "r", "expires": 900}


class OptionalScopeGrantTests(unittest.IsolatedAsyncioTestCase):
    """RomM refuses the whole grant over one scope the user lacks.

    So tasks.run is asked for, and dropped when refused - it must never be
    able to stop the bot logging in.
    """

    async def test_tasks_run_is_asked_for_first(self):
        client, session = token_client(lambda scope: TokenResponse(200, "", GRANTED))
        self.assertTrue(await client.get_oauth_token())
        self.assertIn("tasks.run", session.scopes_asked[0].split())
        self.assertEqual(1, len(session.scopes_asked))

    async def test_a_scope_refusal_retries_without_the_optional_scopes(self):
        def answer(scope):
            if "tasks.run" in scope.split():
                return TokenResponse(403, '{"detail":"Insufficient scope"}')
            return TokenResponse(200, "", GRANTED)

        client, session = token_client(answer)
        with self.assertLogs("romm_bot", level="WARNING"):
            self.assertTrue(await client.get_oauth_token())
        self.assertEqual(2, len(session.scopes_asked))
        self.assertEqual(" ".join(BASE_OAUTH_SCOPES), session.scopes_asked[1])
        self.assertEqual("a", client.access_token)

    async def test_a_wrong_password_is_not_retried(self):
        client, session = token_client(
            lambda scope: TokenResponse(401, '{"detail":"Invalid username or password"}')
        )
        self.assertFalse(await client.get_oauth_token())
        self.assertEqual(1, len(session.scopes_asked))

    async def test_a_disabled_account_is_not_retried(self):
        client, session = token_client(
            lambda scope: TokenResponse(403, '{"detail":"User account is disabled"}')
        )
        self.assertFalse(await client.get_oauth_token())
        self.assertEqual(1, len(session.scopes_asked))

    async def test_the_base_scopes_refused_too_is_a_failure(self):
        client, session = token_client(
            lambda scope: TokenResponse(403, '{"detail":"Insufficient scope"}')
        )
        self.assertFalse(await client.get_oauth_token())
        self.assertEqual(2, len(session.scopes_asked))

    def test_tasks_run_is_optional_not_base(self):
        self.assertIn("tasks.run", OPTIONAL_OAUTH_SCOPES)
        self.assertNotIn("tasks.run", BASE_OAUTH_SCOPES)


class ScanSession:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.posts = []

    def post(self, url, headers=None, json=None):
        if self.error:
            raise self.error
        self.posts.append((url, json))
        return self.response


def scan_client(session):
    client = object.__new__(RommClient)
    client.config = SimpleNamespace(API_BASE_URL="http://romm")
    client.access_token = "t"

    async def ensure_valid_token():
        return True

    async def ensure_session():
        return session

    client.ensure_valid_token = ensure_valid_token
    client.ensure_session = ensure_session
    return client


class StartScanTests(unittest.IsolatedAsyncioTestCase):
    async def test_posts_the_payload_to_the_tasks_api(self):
        session = ScanSession(TokenResponse(202, "", {"task_id": "j1"}))
        status, body = await scan_client(session).start_scan({"type": "quick"})
        self.assertEqual([("http://romm/api/tasks/scan", {"type": "quick"})], session.posts)
        self.assertEqual((202, {"task_id": "j1"}), (status, body))

    async def test_a_refusal_keeps_its_status_and_reason(self):
        session = ScanSession(TokenResponse(409, "", {"detail": "already running"}))
        self.assertEqual((409, {"detail": "already running"}),
                         await scan_client(session).start_scan({}))

    async def test_an_unreachable_server_is_none(self):
        session = ScanSession(error=OSError("connection refused"))
        self.assertEqual((None, None), await scan_client(session).start_scan({}))


if __name__ == "__main__":
    unittest.main()
