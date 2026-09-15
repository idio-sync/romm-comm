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

# One free seat: the only shape that can vanish by filling rather than by
# closing, because RomM drops a room from /netplay/list once it is full.
NEARLY_FULL = {"r1": {"room_name": "Bomberman", "current": 1, "max": 2,
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
    def test_a_room_with_seats_to_spare_ends_when_it_vanishes(self):
        """2 of 4 cannot have filled inside one poll. It closed."""
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

    def test_pending_goes_stale_once_polls_keep_failing(self):
        """Otherwise the post keeps asserting "no room is open yet" while we
        have no idea whether one opened."""
        w = make_watcher()
        for now in (1010.0, 1020.0):
            self.assertFalse(
                advance(w, None, now=now, pending_timeout=900.0, stale_after=3)
            )
            self.assertFalse(w.stale)
        changed = advance(w, None, now=1030.0, pending_timeout=900.0, stale_after=3)
        self.assertTrue(w.stale)
        self.assertTrue(changed)
        self.assertIs(w.state, NetplayState.PENDING)

    def test_a_stale_pending_watcher_recovers_on_a_good_poll(self):
        w = make_watcher()
        for now in (1010.0, 1020.0, 1030.0):
            advance(w, None, now=now, pending_timeout=900.0, stale_after=3)
        self.assertTrue(advance(w, {}, now=1040.0, pending_timeout=900.0))
        self.assertFalse(w.stale)

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


class UnlistedTests(unittest.TestCase):
    """A room that fills disappears from RomM's list exactly like one that closes.

    _is_room_open drops a room once len(players) >= max_players, so `current
    == max` never arrives and a full room is indistinguishable from a gone
    one. A grace period cannot separate them either: for a two-player game
    full IS the steady state, so the room stays absent for the whole session.
    All the machine can do is decline to call it an ending.
    """

    def _unlisted(self, now=1010.0):
        w = make_watcher(state=NetplayState.LIVE, rooms=NEARLY_FULL)
        advance(w, {}, now=now)
        return w

    def test_a_room_one_seat_short_does_not_end_when_it_vanishes(self):
        self.assertIs(self._unlisted().state, NetplayState.LIVE)

    def test_vanishing_is_stamped(self):
        self.assertEqual(self._unlisted(now=1010.0).unlisted_since, 1010.0)

    def test_vanishing_is_a_visible_change(self):
        w = make_watcher(state=NetplayState.LIVE, rooms=NEARLY_FULL)
        self.assertTrue(advance(w, {}, now=1010.0))

    def test_the_last_known_rooms_are_kept(self):
        """The post still has to name the room and its host."""
        self.assertEqual(self._unlisted().rooms, NEARLY_FULL)

    def test_staying_unlisted_is_not_a_change_every_tick(self):
        w = self._unlisted()
        self.assertFalse(advance(w, {}, now=1030.0))

    def test_a_reappearing_room_goes_back_to_listed(self):
        """Someone left: the freed seat is the whole reason to keep polling."""
        w = self._unlisted()
        advance(w, NEARLY_FULL, now=1100.0)
        self.assertIsNone(w.unlisted_since)
        self.assertIs(w.state, NetplayState.LIVE)

    def test_a_reappearing_room_is_a_change_even_if_identical(self):
        """Without this the post keeps saying "no open seats" over an open one."""
        w = self._unlisted()
        self.assertTrue(advance(w, NEARLY_FULL, now=1100.0))

    def test_it_ends_at_the_session_cap(self):
        w = self._unlisted(now=1010.0)
        changed = advance(w, {}, now=1010.0 + 3600.0, session_timeout=3600.0)
        self.assertIs(w.state, NetplayState.ENDED)
        self.assertTrue(changed)

    def test_it_does_not_end_one_second_early(self):
        w = self._unlisted(now=1010.0)
        advance(w, {}, now=1010.0 + 3599.0, session_timeout=3600.0)
        self.assertIs(w.state, NetplayState.LIVE)

    def test_the_cap_dates_the_ending_from_the_disappearance(self):
        """Not from when we gave up looking - that would invent an hour."""
        w = self._unlisted(now=1010.0)
        advance(w, {}, now=1010.0 + 3600.0, session_timeout=3600.0)
        self.assertEqual(w.ended_at, 1010.0)

    def test_the_cap_restarts_when_a_seat_opens_and_closes_again(self):
        w = self._unlisted(now=1010.0)
        advance(w, NEARLY_FULL, now=2000.0)
        advance(w, {}, now=2010.0)
        self.assertEqual(w.unlisted_since, 2010.0)

    def test_a_failed_poll_while_unlisted_does_not_end_it(self):
        w = self._unlisted()
        advance(w, None, now=1030.0)
        self.assertIs(w.state, NetplayState.LIVE)
        self.assertEqual(w.unlisted_since, 1010.0)

    def test_several_rooms_need_only_one_that_could_have_filled(self):
        both = {"a": NEARLY_FULL["r1"], "b": ROOM["r1"]}
        w = make_watcher(state=NetplayState.LIVE, rooms=both)
        advance(w, {}, now=1010.0)
        self.assertIs(w.state, NetplayState.LIVE)

    def test_a_pending_watcher_never_goes_unlisted(self):
        """Nothing was ever listed, so nothing can have stopped being."""
        w = make_watcher()
        advance(w, {}, now=1010.0)
        self.assertIsNone(w.unlisted_since)
        self.assertIs(w.state, NetplayState.PENDING)


if __name__ == "__main__":
    unittest.main()
