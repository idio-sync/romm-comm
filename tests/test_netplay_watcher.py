"""The netplay session state machine.

Pure by design: every transition is exercised here with fabricated poll
results, so the cog's loop only has to do IO. The rule this file exists to
protect is that a failed poll (None) is not an empty poll ({}) - conflating
them ends a live session every time RomM hiccups.
"""

import unittest

from cogs.netplay.watcher import NetplayState, NetplayWatcher, advance

ROOM = {"r1": {"room_name": "Bomberman", "current": 2, "max": 4,
               "player_name": "idiosync", "hasPassword": False}}


def make_watcher(**overrides):
    values = {
        "rom_id": 50265,
        "rom_name": "Super Bomberman",
        "requester_id": 1,
        "requester_name": "alice",
        "channel_id": 2,
        "created_at": 1000.0,
    }
    values.update(overrides)
    return NetplayWatcher(**values)


class PendingTests(unittest.TestCase):
    def test_starts_pending(self):
        self.assertIs(make_watcher().state, NetplayState.PENDING)

    def test_empty_poll_leaves_it_pending(self):
        w = make_watcher()
        changed = advance(w, {}, now=1010.0)
        self.assertIs(w.state, NetplayState.PENDING)
        self.assertFalse(changed)

    def test_a_room_makes_it_live(self):
        w = make_watcher()
        changed = advance(w, ROOM, now=1010.0)
        self.assertIs(w.state, NetplayState.LIVE)
        self.assertEqual(w.rooms, ROOM)
        self.assertTrue(changed)

    def test_expires_after_the_timeout(self):
        w = make_watcher()
        changed = advance(w, {}, now=1000.0 + 900.0, pending_timeout=900.0)
        self.assertIs(w.state, NetplayState.EXPIRED)
        self.assertTrue(changed)

    def test_does_not_expire_one_second_early(self):
        w = make_watcher()
        advance(w, {}, now=1000.0 + 899.0, pending_timeout=900.0)
        self.assertIs(w.state, NetplayState.PENDING)


class LiveTests(unittest.TestCase):
    def test_empty_poll_ends_the_session(self):
        w = make_watcher(state=NetplayState.LIVE, rooms=ROOM)
        changed = advance(w, {}, now=1010.0)
        self.assertIs(w.state, NetplayState.ENDED)
        self.assertTrue(changed)

    def test_player_count_change_is_reported_as_changed(self):
        w = make_watcher(state=NetplayState.LIVE, rooms=ROOM)
        grown = {"r1": dict(ROOM["r1"], current=3)}
        self.assertTrue(advance(w, grown, now=1010.0))
        self.assertEqual(w.rooms["r1"]["current"], 3)

    def test_identical_poll_is_not_a_change(self):
        """Drives edit suppression: an unchanged poll must not trigger an edit."""
        w = make_watcher(state=NetplayState.LIVE, rooms=ROOM)
        self.assertFalse(advance(w, dict(ROOM), now=1010.0))

    def test_multiple_concurrent_rooms_are_all_kept(self):
        two = {"r1": ROOM["r1"], "r2": dict(ROOM["r1"], room_name="Second")}
        w = make_watcher(state=NetplayState.LIVE, rooms=ROOM)
        advance(w, two, now=1010.0)
        self.assertEqual(len(w.rooms), 2)

    def test_one_room_closing_while_another_opens_stays_live(self):
        w = make_watcher(state=NetplayState.LIVE, rooms=ROOM)
        replacement = {"r2": dict(ROOM["r1"], room_name="Second")}
        advance(w, replacement, now=1010.0)
        self.assertIs(w.state, NetplayState.LIVE)


class FailureTests(unittest.TestCase):
    def test_a_failed_poll_does_not_end_a_live_session(self):
        w = make_watcher(state=NetplayState.LIVE, rooms=ROOM)
        changed = advance(w, None, now=1010.0)
        self.assertIs(w.state, NetplayState.LIVE)
        self.assertFalse(changed)
        self.assertFalse(w.stale)

    def test_repeated_failures_mark_it_stale_but_never_end_it(self):
        """RomM being unreachable says nothing about whether people are playing."""
        w = make_watcher(state=NetplayState.LIVE, rooms=ROOM)
        advance(w, None, now=1010.0, stale_after=3)
        advance(w, None, now=1020.0, stale_after=3)
        self.assertFalse(w.stale)
        changed = advance(w, None, now=1030.0, stale_after=3)
        self.assertIs(w.state, NetplayState.LIVE)
        self.assertTrue(w.stale)
        self.assertTrue(changed)

    def test_going_stale_is_reported_once_not_every_tick(self):
        w = make_watcher(state=NetplayState.LIVE, rooms=ROOM)
        for t in (1010.0, 1020.0, 1030.0):
            advance(w, None, now=t, stale_after=3)
        self.assertFalse(advance(w, None, now=1040.0, stale_after=3))

    def test_a_hundred_failures_still_do_not_end_it(self):
        w = make_watcher(state=NetplayState.LIVE, rooms=ROOM)
        for i in range(100):
            advance(w, None, now=1010.0 + i, stale_after=3)
        self.assertIs(w.state, NetplayState.LIVE)

    def test_recovery_clears_stale_and_is_a_visible_change(self):
        w = make_watcher(state=NetplayState.LIVE, rooms=ROOM)
        for t in (1010.0, 1020.0, 1030.0):
            advance(w, None, now=t, stale_after=3)
        self.assertTrue(w.stale)
        changed = advance(w, ROOM, now=1040.0, stale_after=3)
        self.assertFalse(w.stale)
        self.assertEqual(w.consecutive_failures, 0)
        self.assertTrue(changed)

    def test_failures_while_pending_do_not_expire_early(self):
        w = make_watcher()
        for t in (1010.0, 1020.0, 1030.0, 1040.0):
            advance(w, None, now=t, stale_after=3)
        self.assertIs(w.state, NetplayState.PENDING)

    def test_pending_still_expires_while_polls_keep_failing(self):
        """Otherwise an unreachable RomM fills the watcher cap permanently."""
        w = make_watcher()
        advance(w, None, now=1010.0, pending_timeout=900.0, stale_after=3)
        self.assertIs(w.state, NetplayState.PENDING)
        changed = advance(w, None, now=1900.0, pending_timeout=900.0, stale_after=3)
        self.assertIs(w.state, NetplayState.EXPIRED)
        self.assertTrue(changed)


class TerminalTests(unittest.TestCase):
    def test_ended_is_terminal(self):
        w = make_watcher(state=NetplayState.ENDED)
        self.assertFalse(advance(w, ROOM, now=1010.0))
        self.assertIs(w.state, NetplayState.ENDED)

    def test_expired_is_terminal(self):
        w = make_watcher(state=NetplayState.EXPIRED)
        self.assertFalse(advance(w, ROOM, now=1010.0))
        self.assertIs(w.state, NetplayState.EXPIRED)

    def test_is_terminal_flag(self):
        self.assertFalse(make_watcher().is_terminal)
        self.assertFalse(make_watcher(state=NetplayState.LIVE).is_terminal)
        self.assertTrue(make_watcher(state=NetplayState.ENDED).is_terminal)
        self.assertTrue(make_watcher(state=NetplayState.EXPIRED).is_terminal)


if __name__ == "__main__":
    unittest.main()
