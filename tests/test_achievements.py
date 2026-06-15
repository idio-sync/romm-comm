import unittest

from cogs.achievements import compute_global_leaderboard


def _user(uid, username, results, ra_username="ra"):
    return {
        "id": uid,
        "username": username,
        "ra_username": ra_username,
        "ra_progression": {"total": len(results), "results": results},
    }


def _result(num_awarded, hardcore=0, kind=None, rom_ra_id=1, max_possible=10):
    return {
        "rom_ra_id": rom_ra_id,
        "max_possible": max_possible,
        "num_awarded": num_awarded,
        "num_awarded_hardcore": hardcore,
        "highest_award_kind": kind,
    }


class GlobalLeaderboardTests(unittest.TestCase):
    def test_ranks_by_total_earned_desc(self):
        users = [
            _user(1, "alice", [_result(10), _result(5, rom_ra_id=2)]),   # 15
            _user(2, "bob", [_result(20)]),                              # 20
        ]
        rows = compute_global_leaderboard(users, {})
        self.assertEqual(["bob", "alice"], [r["name"] for r in rows])
        self.assertEqual(20, rows[0]["earned"])
        self.assertEqual(15, rows[1]["earned"])

    def test_tiebreak_hardcore_then_mastered_then_name(self):
        users = [
            _user(1, "zed", [_result(10, hardcore=2)]),
            _user(2, "amy", [_result(10, hardcore=2)]),  # same earned+hardcore -> name asc
            _user(3, "kim", [_result(10, hardcore=9)]),  # higher hardcore -> first
        ]
        rows = compute_global_leaderboard(users, {})
        self.assertEqual(["kim", "amy", "zed"], [r["name"] for r in rows])

    def test_counts_mastery_case_insensitive_excluding_beaten(self):
        users = [_user(1, "alice", [
            _result(10, kind="Mastered"),
            _result(10, kind="completed", rom_ra_id=2),
            _result(10, kind="beaten-hardcore", rom_ra_id=3),
            _result(10, kind=None, rom_ra_id=4),
        ])]
        rows = compute_global_leaderboard(users, {})
        self.assertEqual(2, rows[0]["mastered"])

    def test_linked_users_decorated_others_use_username(self):
        users = [_user(1, "alice", [_result(5)]), _user(2, "bob", [_result(3)])]
        links = {1: "AliceOnDiscord"}
        rows = compute_global_leaderboard(users, links)
        by_id = {r["romm_id"]: r for r in rows}
        self.assertEqual("AliceOnDiscord", by_id[1]["name"])
        self.assertTrue(by_id[1]["is_linked"])
        self.assertEqual("bob", by_id[2]["name"])
        self.assertFalse(by_id[2]["is_linked"])

    def test_excludes_bot_account_and_zero_and_empty_users(self):
        users = [
            _user(1, "alice", [_result(5)]),
            _user(2, "rommbot", [_result(99)]),          # bot account -> excluded
            _user(3, "noprog", []),                        # empty results -> excluded
            _user(4, "zero", [_result(0)]),                # zero earned -> excluded
            {"id": 5, "username": "nullprog", "ra_progression": None},  # None -> excluded
        ]
        rows = compute_global_leaderboard(users, {}, bot_username="RommBot")
        self.assertEqual(["alice"], [r["name"] for r in rows])

    def test_empty_input_returns_empty(self):
        self.assertEqual([], compute_global_leaderboard([], {}))


if __name__ == "__main__":
    unittest.main()
