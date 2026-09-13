"""The inbound ggrequestz sync, end to end.

_sync_statuses_from_ggrequestz pulls status changes made on the ggrequestz side
back into the local database and returns the transitions so the caller can DM
about them. It called GGRequestzIntegration.get_request_by_id, which did not
exist, and the whole loop sat inside a broad `except Exception` -- so the
feature 2fe17c9 describes has never produced a single transition for anyone
running the integration. It failed silently rather than loudly.

These drive the real method against the real get_request_by_id.
"""

from unittest import IsolatedAsyncioTestCase

from cogs.requests.cog import Request
from integrations.ggrequestz import GGR_REQUEST_PAGE_SIZE, GGRequestzIntegration


def local_row(request_id, ggr_request_id, status="pending", user_id=42):
    return {
        "id": request_id,
        "ggr_request_id": ggr_request_id,
        "status": status,
        "user_id": user_id,
        "game_name": "GoldenEye 007",
        "igdb_game_name": None,
    }


class FakeGgr:
    """The real get_request_by_id over a canned request list."""

    get_request_by_id = GGRequestzIntegration.get_request_by_id
    enabled = True

    def __init__(self, remote):
        self.remote = remote

    async def ensure_session(self):
        return True

    async def get_user_requests(self, limit, offset):
        page = self.remote[offset:offset + limit]
        return {"success": True, "requests": page}


class FakeRepo:
    def __init__(self, rows):
        self.rows = rows
        self.applied = []

    async def list_open_synced_with_ggrequestz(self, user_id=None):
        return self.rows

    async def apply_synced_status(self, request_id, status, notes):
        self.applied.append((request_id, status, notes))


class Syncer:
    """_sync_statuses_from_ggrequestz, with only its collaborators stubbed."""

    _sync_statuses_from_ggrequestz = Request._sync_statuses_from_ggrequestz

    def __init__(self, ggr, repo):
        self.ggr = ggr
        self.repo = repo


class InboundSyncTests(IsolatedAsyncioTestCase):
    async def test_a_status_changed_on_the_ggrequestz_side_comes_back(self):
        ggr = FakeGgr([{"id": "500", "status": "fulfilled"}])
        repo = FakeRepo([local_row(7, 500)])

        transitions = await Syncer(ggr, repo)._sync_statuses_from_ggrequestz()

        self.assertEqual(len(transitions), 1)
        self.assertEqual(transitions[0]["request_id"], 7)
        self.assertEqual(transitions[0]["status"], "fulfilled")
        self.assertEqual(transitions[0]["user_id"], 42)
        self.assertEqual(repo.applied, [(7, "fulfilled", "Synced from ggrequestz")])

    async def test_rejected_maps_onto_the_local_reject_status(self):
        ggr = FakeGgr([{"id": "500", "status": "rejected"}])
        repo = FakeRepo([local_row(7, 500)])

        transitions = await Syncer(ggr, repo)._sync_statuses_from_ggrequestz()

        self.assertEqual(transitions[0]["status"], "reject")

    async def test_an_unchanged_status_produces_no_transition(self):
        ggr = FakeGgr([{"id": "500", "status": "pending"}])
        repo = FakeRepo([local_row(7, 500, status="pending")])

        self.assertEqual(await Syncer(ggr, repo)._sync_statuses_from_ggrequestz(), [])
        self.assertEqual(repo.applied, [])

    async def test_approved_has_no_local_equivalent_and_is_skipped(self):
        ggr = FakeGgr([{"id": "500", "status": "approved"}])
        repo = FakeRepo([local_row(7, 500)])

        self.assertEqual(await Syncer(ggr, repo)._sync_statuses_from_ggrequestz(), [])

    async def test_a_request_ggrequestz_does_not_know_is_left_alone(self):
        ggr = FakeGgr([{"id": "999", "status": "fulfilled"}])
        repo = FakeRepo([local_row(7, 500)])

        self.assertEqual(await Syncer(ggr, repo)._sync_statuses_from_ggrequestz(), [])
        self.assertEqual(repo.applied, [])

    async def test_it_still_works_when_the_request_is_past_the_first_page(self):
        remote = [{"id": str(i), "status": "pending"} for i in range(GGR_REQUEST_PAGE_SIZE)]
        remote.append({"id": "500", "status": "fulfilled"})
        ggr = FakeGgr(remote)
        repo = FakeRepo([local_row(7, 500)])

        transitions = await Syncer(ggr, repo)._sync_statuses_from_ggrequestz()

        self.assertEqual(transitions[0]["status"], "fulfilled")

    async def test_a_disabled_integration_syncs_nothing(self):
        ggr = FakeGgr([{"id": "500", "status": "fulfilled"}])
        ggr.enabled = False
        repo = FakeRepo([local_row(7, 500)])

        self.assertEqual(await Syncer(ggr, repo)._sync_statuses_from_ggrequestz(), [])
        self.assertEqual(repo.applied, [])
