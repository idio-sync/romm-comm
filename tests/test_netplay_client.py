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

from romm_client import BASE_OAUTH_SCOPES, NOT_FOUND, RommClient


class RecordingClient(RommClient):
    """A RommClient that records calls instead of making them."""

    def __init__(self, result: Optional[Dict[str, Any]]):
        self.calls: list = []
        self._result = result

    async def make_authenticated_request(self, method, endpoint, data=None,
                                         params=None, form_data=None,
                                         require_csrf=False, not_found=None):
        self.calls.append((method, endpoint, params))
        self.not_found = not_found
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


class JsonResponse:
    """Just what _read_response reads off a response."""

    def __init__(self, status, body=None, raises=False):
        self.status = status
        self._body = body
        self._raises = raises

    async def json(self, content_type="application/json"):
        if self._raises:
            raise ValueError("not JSON")
        return self._body

    async def text(self):
        return str(self._body)


class RomGoneTests(unittest.IsolatedAsyncioTestCase):
    """RomM 5.4+ 404s /netplay/list for a ROM deleted or hidden from us.

    Only RomM's own 404 counts. A proxy that cannot route the request also
    says 404, and ending every watched session over that would be wrong.
    """

    async def test_room_polls_opt_in_to_not_found(self):
        client = RecordingClient({})
        await client.list_netplay_rooms(50265)
        self.assertIs(client.not_found, NOT_FOUND)

    async def test_romms_404_body_reads_as_not_found(self):
        body = {"detail": "Rom with id '50265' not found"}
        result = await RommClient._read_response(JsonResponse(404, body), NOT_FOUND)
        self.assertIs(result, NOT_FOUND)

    async def test_a_404_without_romms_body_is_a_failure(self):
        response = JsonResponse(404, raises=True)  # an HTML page from a proxy
        self.assertIsNone(await RommClient._read_response(response, NOT_FOUND))

    async def test_a_404_with_some_other_json_is_a_failure(self):
        response = JsonResponse(404, ["not", "fastapi"])
        self.assertIsNone(await RommClient._read_response(response, NOT_FOUND))

    async def test_callers_that_do_not_opt_in_still_get_none(self):
        body = {"detail": "Rom with id '50265' not found"}
        self.assertIsNone(await RommClient._read_response(JsonResponse(404, body)))

    async def test_other_failures_are_still_none(self):
        body = {"detail": "Internal error"}
        self.assertIsNone(
            await RommClient._read_response(JsonResponse(500, body), NOT_FOUND)
        )

    async def test_success_is_unchanged(self):
        rooms = {"r1": {"current": 1, "max": 2}}
        result = await RommClient._read_response(JsonResponse(200, rooms), NOT_FOUND)
        self.assertEqual(result, rooms)


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


class PermissionProbeTests(unittest.IsolatedAsyncioTestCase):
    """RomM 5.4 answers {} to a user without emulation read, not a 403.

    Payloads follow backend/tests/endpoints/test_permissions_me.py at 5.4.1.
    """

    @staticmethod
    def body(*actions, is_admin=False):
        return {
            "is_admin": is_admin,
            "grants": [{"action": a, "scope": {"kind": "global"}} for a in actions],
            "hidden": {"platforms": [], "roms": []},
        }

    async def test_requests_the_permissions_me_endpoint(self):
        client = RecordingClient(self.body("rom.play"))
        await client.netplay_permission_ok()
        self.assertEqual(client.calls, [("GET", "permissions/me", None)])

    async def test_rom_play_granted_is_a_yes(self):
        client = RecordingClient(self.body("rom.view", "rom.play"))
        self.assertIs(await client.netplay_permission_ok(), True)

    async def test_rom_play_revoked_is_a_definite_no(self):
        client = RecordingClient(self.body("rom.view", "netplay.host"))
        self.assertIs(await client.netplay_permission_ok(), False)

    async def test_admin_bypasses_the_grant(self):
        client = RecordingClient(self.body(is_admin=True))
        self.assertIs(await client.netplay_permission_ok(), True)

    async def test_no_answer_is_undetermined(self):
        """Pre-5.4 RomM has no such route; a token without me.read is refused."""
        client = RecordingClient(None)
        self.assertIsNone(await client.netplay_permission_ok())

    async def test_an_unexpected_shape_is_undetermined(self):
        client = RecordingClient({"detail": "something else"})
        self.assertIsNone(await client.netplay_permission_ok())


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

    def test_oauth_grant_always_requests_both_netplay_scopes(self):
        """netplay/list is assets.read through RomM 5.3, roms.read from 5.4.

        Base, not optional: an optional scope is dropped when refused, and
        netplay without its scope is a feature that silently never updates.
        """
        self.assertIn("assets.read", BASE_OAUTH_SCOPES)
        self.assertIn("roms.read", BASE_OAUTH_SCOPES)

    def test_readme_documents_both_netplay_scopes(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("assets.read", readme)
        self.assertIn("roms.read", readme)


if __name__ == "__main__":
    unittest.main()
