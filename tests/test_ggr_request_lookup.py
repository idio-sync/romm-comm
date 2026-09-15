"""Tests for GGRequestzIntegration.get_request_by_id.

ggrequestz has no endpoint for a single request, so this pages the caller's own
request list and matches on id. Two details decide whether it works at all: the
list's `id` and the stored ggr_request_id need not share a type, and a short
page is the last page.
"""

from unittest import IsolatedAsyncioTestCase

from integrations.ggrequestz import (
    GGR_REQUEST_MAX_PAGES,
    GGR_REQUEST_PAGE_SIZE,
    GGRequestzIntegration,
)


def entry(request_id, status="pending"):
    """A list entry shaped as ggrequestz returns it: id is a string."""
    return {"id": str(request_id), "status": status, "title": "A Game"}


class FakeIntegration:
    """GGRequestzIntegration.get_request_by_id, driven without a network.

    Only ensure_session and get_user_requests are stubbed; the method under
    test is the real one.
    """

    get_request_by_id = GGRequestzIntegration.get_request_by_id

    def __init__(self, pages, session_ready=True):
        self.pages = pages
        self.session_ready = session_ready
        self.calls = []

    async def ensure_session(self):
        return self.session_ready

    async def get_user_requests(self, limit, offset):
        self.calls.append((limit, offset))
        index = offset // GGR_REQUEST_PAGE_SIZE
        if index >= len(self.pages):
            return None
        page = self.pages[index]
        if page is None:
            return None
        return {"success": True, "requests": page}


class GetRequestByIdTests(IsolatedAsyncioTestCase):
    async def test_it_finds_a_request_on_the_first_page(self):
        ggr = FakeIntegration([[entry(1), entry(99, "fulfilled"), entry(3)]])
        found = await ggr.get_request_by_id(99)
        self.assertEqual(found, entry(99, "fulfilled"))

    async def test_an_integer_id_matches_the_string_the_api_returns(self):
        # ggr_request_id comes out of SQLite as an int; the list gives strings.
        # Comparing them directly would never match, silently.
        ggr = FakeIntegration([[entry(99)]])
        self.assertIsNotNone(await ggr.get_request_by_id(99))
        self.assertIsNotNone(await ggr.get_request_by_id("99"))

    async def test_it_does_not_match_a_different_id(self):
        ggr = FakeIntegration([[entry(1), entry(2)]])
        self.assertIsNone(await ggr.get_request_by_id(99))

    async def test_it_pages_until_it_finds_the_request(self):
        full_page = [entry(i) for i in range(GGR_REQUEST_PAGE_SIZE)]
        second = [entry(999, "rejected")]
        ggr = FakeIntegration([full_page, second])
        found = await ggr.get_request_by_id(999)
        self.assertEqual(found["status"], "rejected")
        self.assertEqual(
            ggr.calls,
            [(GGR_REQUEST_PAGE_SIZE, 0), (GGR_REQUEST_PAGE_SIZE, GGR_REQUEST_PAGE_SIZE)],
        )

    async def test_a_short_page_ends_the_search(self):
        # Anything less than a full page means there is no next page, so asking
        # for one would be a wasted round trip on every miss.
        ggr = FakeIntegration([[entry(1)], [entry(99)]])
        self.assertIsNone(await ggr.get_request_by_id(99))
        self.assertEqual(ggr.calls, [(GGR_REQUEST_PAGE_SIZE, 0)])

    async def test_an_exactly_full_last_page_still_terminates(self):
        full_page = [entry(i) for i in range(GGR_REQUEST_PAGE_SIZE)]
        ggr = FakeIntegration([full_page, []])
        self.assertIsNone(await ggr.get_request_by_id(999))
        self.assertEqual(len(ggr.calls), 2)

    async def test_it_gives_up_rather_than_paging_forever(self):
        full_page = [entry(i) for i in range(GGR_REQUEST_PAGE_SIZE)]
        ggr = FakeIntegration([full_page] * (GGR_REQUEST_MAX_PAGES + 5))
        with self.assertLogs("integrations.ggrequestz", level="WARNING"):
            self.assertIsNone(await ggr.get_request_by_id(999))
        self.assertEqual(len(ggr.calls), GGR_REQUEST_MAX_PAGES)

    async def test_a_failed_page_stops_the_search(self):
        ggr = FakeIntegration([None])
        self.assertIsNone(await ggr.get_request_by_id(99))

    async def test_no_session_means_no_lookup(self):
        ggr = FakeIntegration([[entry(99)]], session_ready=False)
        self.assertIsNone(await ggr.get_request_by_id(99))
        self.assertEqual(ggr.calls, [])

    async def test_a_page_without_a_requests_key_is_survivable(self):
        class Odd(FakeIntegration):
            async def get_user_requests(self, limit, offset):
                self.calls.append((limit, offset))
                return {"success": True}

        ggr = Odd([[]])
        self.assertIsNone(await ggr.get_request_by_id(99))
