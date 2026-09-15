"""Where GGRequestzIntegration meets the shapes ggrequestz actually sends.

The drift these cover was silent: /api/games/{id} dropped its envelope, the
integration went on reading `data["game"]`, and every lookup came back None -
so requests quietly stopped carrying their game_data cache, and nothing logged
an error. Each test names the shape taken from the route handler in
XTREEMMAK/ggrequestz, so the next time one moves, the mismatch fails here
instead of in production.
"""

import json
from unittest import IsolatedAsyncioTestCase

from integrations.ggrequestz import GGRequestzIntegration


class FakeResponse:
    def __init__(self, status=200, payload=None, text=None):
        self.status = status
        self._payload = payload
        self._text = text if text is not None else json.dumps(payload)

    async def json(self):
        return self._payload

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """Records what was sent and replays a queued response per call."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    def _next(self, method, url, kwargs):
        self.calls.append({'method': method, 'url': url, **kwargs})
        return self.responses.pop(0)

    def get(self, url, **kwargs):
        return self._next('GET', url, kwargs)

    def post(self, url, **kwargs):
        return self._next('POST', url, kwargs)


class FakeIntegration:
    """The real methods, with the session and its readiness stubbed out."""

    get_game_details = GGRequestzIntegration.get_game_details
    create_request = GGRequestzIntegration.create_request
    search_game = GGRequestzIntegration.search_game
    get_endpoint_url = GGRequestzIntegration.get_endpoint_url
    get_auth_headers = GGRequestzIntegration.get_auth_headers
    _build_game_data_cache = GGRequestzIntegration._build_game_data_cache

    def __init__(self, responses):
        self.ggr_base_url = 'http://ggr.test'
        self.ggr_api_key = 'ggr_key'
        self.endpoints = {
            'version': {'path': '/api/version', 'auth': 'bearer'},
            'search': {'path': '/api/search', 'auth': 'bearer'},
            'games': {'path': '/api/games', 'auth': 'bearer'},
            'request': {'path': '/api/request', 'auth': 'bearer'},
            'request_list': {'path': '/api/request', 'auth': 'bearer'},
        }
        self.session = FakeSession(responses)

    async def ensure_session(self):
        return True


GAME = {
    'igdb_id': '1234',
    'title': 'A Game',
    'summary': 'About a game.',
    'cover_url': 'http://cover',
    'platforms': ['PC'],
}


class GameDetailsTests(IsolatedAsyncioTestCase):
    async def test_it_reads_the_bare_game_object(self):
        # /api/games/[id] ends in `return json(game)` -- no envelope. Reading
        # data['game'] behind data['success'] returned None for every game, and
        # the only visible effect was that requests stopped carrying game_data.
        ggr = FakeIntegration([FakeResponse(payload=GAME)])
        self.assertEqual(await ggr.get_game_details('1234'), GAME)

    async def test_it_still_reads_the_documented_envelope(self):
        # openapi.json documents {"success": true, "game": {...}}. An older or
        # patched server may still send it.
        ggr = FakeIntegration([FakeResponse(payload={'success': True, 'game': GAME})])
        self.assertEqual(await ggr.get_game_details('1234'), GAME)

    async def test_a_404_is_no_game(self):
        ggr = FakeIntegration([FakeResponse(status=404, payload={'message': 'Game not found'})])
        self.assertIsNone(await ggr.get_game_details('1234'))

    async def test_an_empty_body_is_no_game(self):
        ggr = FakeIntegration([FakeResponse(payload={})])
        self.assertIsNone(await ggr.get_game_details('1234'))


class CreateRequestFailureTests(IsolatedAsyncioTestCase):
    async def test_a_duplicate_keeps_what_ggrequestz_said(self):
        # POST /api/request answers 409 with the reason and the id of the
        # request already open for that game. Flattening it to "HTTP 409" left
        # the log unable to say which game or which request.
        conflict = {
            'success': False,
            'error': '"A Game" has already been requested and is pending.',
            'existing_request_id': 42,
        }
        ggr = FakeIntegration([
            FakeResponse(payload=GAME),  # igdb_id given, so details are fetched
            FakeResponse(status=409, payload=conflict),
        ])
        result = await ggr.create_request(
            game_name='A Game', platform='PC', user_id=1, username='someone',
            igdb_id='1234',
        )

        self.assertFalse(result['success'])
        self.assertEqual(result['error'], conflict['error'])
        self.assertEqual(result['existing_request_id'], 42)
        self.assertEqual(result['status'], 409)

    async def test_a_missing_scope_names_itself(self):
        # Scope enforcement is default-deny, and the 403 says which scope.
        denial = {
            'success': False,
            'error': 'Insufficient scope',
            'required_scope': 'requests:write',
        }
        ggr = FakeIntegration([
            FakeResponse(payload=GAME),
            FakeResponse(status=403, payload=denial),
        ])
        result = await ggr.create_request(
            game_name='A Game', platform='PC', user_id=1, username='someone',
            igdb_id='1234',
        )

        self.assertEqual(result['error'], 'Insufficient scope')
        self.assertEqual(result['required_scope'], 'requests:write')

    async def test_a_non_json_failure_falls_back_to_the_status(self):
        ggr = FakeIntegration([
            FakeResponse(payload=GAME),
            FakeResponse(status=502, payload=None, text='<html>bad gateway</html>'),
        ])
        result = await ggr.create_request(
            game_name='A Game', platform='PC', user_id=1, username='someone',
            igdb_id='1234',
        )

        self.assertFalse(result['success'])
        self.assertEqual(result['error'], 'HTTP 502')

    async def test_a_created_request_carries_its_id(self):
        created = {
            'success': True,
            'request': {'id': 42, 'title': 'A Game', 'status': 'pending'},
        }
        ggr = FakeIntegration([
            FakeResponse(payload=GAME),
            FakeResponse(status=201, payload=created),
        ])
        result = await ggr.create_request(
            game_name='A Game', platform='PC', user_id=1, username='someone',
            igdb_id='1234',
        )

        self.assertTrue(result['success'])
        self.assertEqual(result['request_id'], 42)
        # The game_data cache only reaches ggrequestz if the lookup worked.
        self.assertEqual(ggr.session.calls[-1]['json']['game_data']['title'], 'A Game')
