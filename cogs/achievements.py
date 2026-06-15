import logging

logger = logging.getLogger(__name__)

# RA award tiers that count as "mastered" for the leaderboard (case-insensitive).
# RA's real tiers include beaten-softcore / beaten-hardcore / completed / mastered;
# "beaten-*" intentionally does NOT count as mastery.
MASTERY_KINDS = {"mastered", "completed"}


def _is_mastery(kind) -> bool:
    return bool(kind) and str(kind).strip().lower() in MASTERY_KINDS


def _results_of(user) -> list:
    """Defensively read a user's RA progression results (flattened dict, keys may be absent)."""
    prog = user.get("ra_progression") or {}
    return prog.get("results") or []


def compute_global_leaderboard(users, links, bot_username=None):
    """Rank users by total RA achievements earned across all games.

    users: list of RomM UserSchema dicts (id, username, ra_progression).
    links: dict mapping romm user id -> resolved Discord display name (linked users only).
    bot_username: RomM username of the bot's own account to exclude (or None/empty to skip).
    Returns: list of row dicts sorted best-first. Each row:
        {romm_id, name, is_linked, earned, hardcore, mastered}
    """
    bot_name = (bot_username or "").strip().lower()
    rows = []
    for user in users:
        username = user.get("username") or ""
        if bot_name and username.strip().lower() == bot_name:
            continue
        results = _results_of(user)
        if not results:
            continue
        earned = sum((r.get("num_awarded") or 0) for r in results)
        if earned < 1:
            continue
        hardcore = sum((r.get("num_awarded_hardcore") or 0) for r in results)
        mastered = sum(1 for r in results if _is_mastery(r.get("highest_award_kind")))
        romm_id = user.get("id")
        linked_name = links.get(romm_id)
        rows.append({
            "romm_id": romm_id,
            "name": linked_name or username,
            "is_linked": linked_name is not None,
            "earned": earned,
            "hardcore": hardcore,
            "mastered": mastered,
        })
    rows.sort(key=lambda r: (-r["earned"], -r["hardcore"], -r["mastered"], r["name"].lower()))
    return rows


def compute_game_leaderboard(users, links, rom_ra_id, bot_username=None):
    """Rank users by achievements earned for one specific game (matched on rom_ra_id).

    Returns rows sorted best-first; each row:
        {romm_id, name, is_linked, earned, max_possible, hardcore, award_kind}
    Per-game tiebreak is hardcore -> name (mastery is implied by earned == max_possible).
    """
    bot_name = (bot_username or "").strip().lower()
    rows = []
    for user in users:
        username = user.get("username") or ""
        if bot_name and username.strip().lower() == bot_name:
            continue
        entry = next((r for r in _results_of(user) if r.get("rom_ra_id") == rom_ra_id), None)
        if entry is None:
            continue
        earned = entry.get("num_awarded") or 0
        if earned < 1:
            continue
        romm_id = user.get("id")
        linked_name = links.get(romm_id)
        rows.append({
            "romm_id": romm_id,
            "name": linked_name or username,
            "is_linked": linked_name is not None,
            "earned": earned,
            "max_possible": entry.get("max_possible"),
            "hardcore": entry.get("num_awarded_hardcore") or 0,
            "award_kind": entry.get("highest_award_kind"),
        })
    rows.sort(key=lambda r: (-r["earned"], -r["hardcore"], r["name"].lower()))
    return rows
