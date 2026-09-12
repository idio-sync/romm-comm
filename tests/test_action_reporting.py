"""Tests for how a failed interaction callback reports itself.

Every one of these callbacks writes and then presents. Before the write, "an
error occurred while X" is true; after it, the same message is a lie - and a
lie that invites a retry of something that already happened. Two of the bugs
found on this branch were that sequence, so the distinction is pinned here.
"""

import asyncio
import unittest

from cogs.requests.responders import ActionFailed, report_action


class FakeFollowup:
    def __init__(self, raises=None):
        self.sent = []
        self.raises = raises

    async def send(self, content=None, ephemeral=False, **kwargs):
        if self.raises:
            raise self.raises
        self.sent.append(content)

    @property
    def last(self):
        return self.sent[-1] if self.sent else None


class BeforeTheWriteTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_clean_run_says_nothing(self):
        followup = FakeFollowup()

        async with report_action(followup, "fulfilling the request") as action:
            action.committed("fulfilment")

        self.assertEqual([], followup.sent)

    async def test_failing_before_the_write_reports_the_action_failed(self):
        followup = FakeFollowup()

        with self.assertLogs("cogs.requests.responders", level="ERROR"):
            async with report_action(followup, "fulfilling the request"):
                raise RuntimeError("database is down")

        self.assertIn("An error occurred while fulfilling the request", followup.last)
        self.assertTrue(followup.last.startswith("❌"))


class AfterTheWriteTests(unittest.IsolatedAsyncioTestCase):
    async def test_failing_after_the_write_does_not_disown_it(self):
        followup = FakeFollowup()

        with self.assertLogs("cogs.requests.responders", level="ERROR"):
            async with report_action(followup, "fulfilling the request") as action:
                action.committed("fulfilment")
                raise RuntimeError("DM transport died")

        self.assertNotIn("An error occurred while fulfilling", followup.last)
        self.assertIn("fulfilment went through", followup.last)

    async def test_it_tells_the_user_not_to_retry(self):
        """The buttons are still live; pressing again re-runs everything."""
        followup = FakeFollowup()

        with self.assertLogs("cogs.requests.responders", level="ERROR"):
            async with report_action(followup, "rejecting the request") as action:
                action.committed("rejection")
                raise RuntimeError("boom")

        self.assertIn("Do not retry", followup.last)

    async def test_the_noun_names_what_actually_happened(self):
        for noun in ("fulfilment", "rejection", "cancellation", "note"):
            followup = FakeFollowup()
            with self.assertLogs("cogs.requests.responders", level="ERROR"):
                async with report_action(followup, "doing the thing") as action:
                    action.committed(noun)
                    raise RuntimeError("boom")
            self.assertIn(f"{noun} went through", followup.last)


class LoggingTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_exception_type_is_logged(self):
        """`KeyError: 1` logged as "Error fulfilling request: 1" was
        indistinguishable from ordinary noise, which is the whole complaint."""
        followup = FakeFollowup()

        with self.assertLogs("cogs.requests.responders", level="ERROR") as logs:
            async with report_action(followup, "fulfilling the request"):
                raise KeyError(1)

        joined = "\n".join(logs.output)
        self.assertIn("KeyError", joined)

    async def test_a_traceback_is_logged(self):
        followup = FakeFollowup()

        with self.assertLogs("cogs.requests.responders", level="ERROR") as logs:
            async with report_action(followup, "fulfilling the request"):
                raise KeyError(1)

        self.assertIn("Traceback", "\n".join(logs.output))


class RecognisedFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_action_failed_reports_its_own_message(self):
        followup = FakeFollowup()

        with self.assertLogs("cogs.requests.responders", level="ERROR"):
            async with report_action(followup, "fulfilling the request"):
                raise ActionFailed("That request was already closed.")

        self.assertEqual("❌ That request was already closed.", followup.last)

    async def test_a_recognised_failure_needs_no_traceback(self):
        followup = FakeFollowup()

        with self.assertLogs("cogs.requests.responders", level="ERROR") as logs:
            async with report_action(followup, "fulfilling the request"):
                raise ActionFailed("already closed")

        self.assertNotIn("Traceback", "\n".join(logs.output))


class ReportingFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_failure_to_report_does_not_escape(self):
        """Telling the user it broke can itself break; the callback still has
        to return, or they are left watching a spinner."""
        followup = FakeFollowup(raises=RuntimeError("Discord is down"))

        with self.assertLogs("cogs.requests.responders", level="WARNING"):
            async with report_action(followup, "fulfilling the request"):
                raise RuntimeError("original problem")

    async def test_cancellation_still_propagates(self):
        """CancelledError is a BaseException, so shutdown is not swallowed."""
        followup = FakeFollowup()

        # assertRaises(Exception) would not catch it, which is the point.
        with self.assertRaises(asyncio.CancelledError):
            async with report_action(followup, "fulfilling the request"):
                raise asyncio.CancelledError()

        self.assertEqual([], followup.sent)


if __name__ == "__main__":
    unittest.main()
