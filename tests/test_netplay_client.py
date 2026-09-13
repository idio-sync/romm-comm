"""The two RomM netplay reads, and the None-vs-{} contract they promise.

These go through make_authenticated_request rather than fetch_api_endpoint
because the latter writes to the shared APICache even on its bypass path
(romm_client.py:468), which would fill the cache with room lists that are
stale seconds later. The tests pin the endpoint and params so a later
refactor cannot quietly switch helpers.
"""

import pathlib
import unittest
from types import SimpleNamespace
from typing import Any, Dict, Optional

from romm_client import RommClient


class RecordingClient(RommClient):
    """A RommClient that records calls instead of making them."""

    def __init__(self, result: Optional[Dict[str, Any]]):
        self.calls: list = []
        self._result = result

    async def make_authenticated_request(self, method, endpoint, data=None,
                                         params=None, form_data=None,
                                         require_csrf=False):
        self.calls.append((method, endpoint, params))
        return self._result


class FakeResponse:
    def __init__(self, status):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """Answers every GET with one status, or raises to mimic a dead network."""

    def __init__(self, status):
        self._status = status

    def get(self, url, **kwargs):
        if self._status is None:
            raise OSError("connection refused")
        return FakeResponse(self._status)


async def probe_returning(status):
    """Run netplay_scope_ok against a session that always answers `status`."""
    client = object.__new__(RommClient)
    client.config = SimpleNamespace(API_BASE_URL="http://romm")
    client.access_token = "t"

    async def ensure_valid_token():
        return True

    async def ensure_session():
        return FakeSession(status)

    client.ensure_valid_token = ensure_valid_token
    client.ensure_session = ensure_session
    return await client.netplay_scope_ok()


class ListNetplayRoomsTests(unittest.IsolatedAsyncioTestCase):
    async def test_requests_the_netplay_list_endpoint_with_game_id(self):
        client = RecordingClient({})
        await client.list_netplay_rooms(50265)
        self.assertEqual(client.calls, [("GET", "netplay/list", {"game_id": 50265})])

    async def test_empty_dict_means_no_rooms(self):
        client = RecordingClient({})
        self.assertEqual(await client.list_netplay_rooms(50265), {})

    async def test_none_means_the_call_failed(self):
        """Distinct from {}. A failed poll must not read as an ended session."""
        client = RecordingClient(None)
        self.assertIsNone(await client.list_netplay_rooms(50265))

    async def test_returns_the_room_payload_unchanged(self):
        rooms = {
            "84c4be1e": {
                "room_name": "Bomberman",
                "current": 2,
                "max": 4,
                "player_name": "idiosync",
                "hasPassword": False,
            }
        }
        client = RecordingClient(rooms)
        self.assertEqual(await client.list_netplay_rooms(50265), rooms)


class ScopeProbeTests(unittest.IsolatedAsyncioTestCase):
    """A missing scope must be distinguishable from a flaky network."""

    async def test_403_is_a_definite_no(self):
        self.assertIs(await probe_returning(403), False)

    async def test_401_is_a_definite_no(self):
        self.assertIs(await probe_returning(401), False)

    async def test_200_is_a_yes(self):
        self.assertIs(await probe_returning(200), True)

    async def test_422_is_a_yes_because_we_were_allowed_to_ask(self):
        self.assertIs(await probe_returning(422), True)

    async def test_500_is_undetermined(self):
        self.assertIsNone(await probe_returning(500))

    async def test_transport_failure_is_undetermined(self):
        self.assertIsNone(await probe_returning(None))


class GetServerConfigTests(unittest.IsolatedAsyncioTestCase):
    async def test_requests_the_config_endpoint(self):
        client = RecordingClient({"EJS_NETPLAY_ENABLED": True})
        await client.get_server_config()
        self.assertEqual(client.calls, [("GET", "config", None)])

    async def test_returns_none_on_failure(self):
        client = RecordingClient(None)
        self.assertIsNone(await client.get_server_config())


ROOT = pathlib.Path(__file__).resolve().parent.parent


class ScopeTests(unittest.TestCase):
    """Anchored on ROOT rather than the cwd, as tests/test_cog_lookups.py is."""

    def test_oauth_grant_requests_assets_read(self):
        """netplay/list is scope assets.read; without it every call 403s."""
        source = (ROOT / "romm_client.py").read_text(encoding="utf-8")
        scope_lines = [ln for ln in source.splitlines() if "add_field('scope'" in ln]
        self.assertTrue(scope_lines, "no OAuth scope line found in romm_client.py")
        self.assertIn("assets.read", scope_lines[0])

    def test_readme_documents_assets_read(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("assets.read", readme)


if __name__ == "__main__":
    unittest.main()
