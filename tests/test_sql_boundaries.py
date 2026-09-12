"""Architecture test: SQL belongs in a repository, not in a Discord view.

Query text in a button callback means schema knowledge is spread across the UI,
there is no single place to change a query, and the logic can only be tested by
constructing a discord.ui.View.

This is a ratchet, not a finished state. Modules still holding SQL are listed
in ALLOWED with the reason; the list is meant to shrink. Anything not listed
must go through cogs/requests/repo.py or database_manager.py.
"""

import ast
import re
import unittest
from pathlib import Path

# Deliberately case-sensitive. Structure alone cannot tell SQL from prose -
# "Select the correct game from IGDB:" is a real button label in this codebase
# and matches SELECT ... FROM perfectly well. Every query here is written with
# uppercase keywords, so that convention is what the match keys on. Write a
# query in lowercase and this test will not see it.
SQL_STATEMENT = re.compile(
    r"\bSELECT\b[\s\S]{0,200}?\bFROM\b"
    r"|\bINSERT\s+INTO\b"
    r"|\bUPDATE\s+\w+\s+SET\b"
    r"|\bDELETE\s+FROM\b"
)

# Modules that own data access by design.
REPOSITORIES = {
    Path("database_manager.py"),
    Path("cogs/requests/repo.py"),
}

# Still to move. Each entry is a debt with a reason, not an exemption.
ALLOWED = {
    Path("cogs/requests/cog.py"): (
        "the request-creation path is done; what is left is the platform-mapping "
        "sync and the scan-completion listener, which both need repository "
        "methods that do not exist yet"
    ),
    Path("cogs/emoji_manager.py"): "no repository for emoji_sync_state yet",
    Path("cogs/recent_roms.py"): "no repository for posted_roms yet",
    Path("cogs/user_manager.py"): "no repository for user_links yet",
}


def sql_literals(path: Path):
    """Every string constant in `path` that looks like a SQL statement."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if SQL_STATEMENT.search(node.value):
                first_line = node.value.strip().splitlines()[0].strip()
                found.append((node.lineno, first_line[:70]))
    return found


class SqlBoundaryTests(unittest.TestCase):
    def modules(self):
        paths = sorted(Path("cogs").rglob("*.py"))
        paths += sorted(Path("integrations").glob("*.py"))
        paths += [Path("bot.py")]
        return [p for p in paths if p not in REPOSITORIES]

    def test_sql_stays_out_of_the_ui_layer(self):
        offenders = {}
        for path in self.modules():
            if path in ALLOWED:
                continue
            found = sql_literals(path)
            if found:
                offenders[str(path)] = found

        self.assertEqual(
            {},
            offenders,
            "Move these into a repository (cogs/requests/repo.py or "
            "database_manager.py):\n"
            + "\n".join(
                f"  {path}:{line}  {sql}"
                for path, hits in offenders.items()
                for line, sql in hits
            ),
        )

    def test_the_request_views_are_sql_free(self):
        """The part of the ratchet that has already been pulled in.

        Called out separately so a regression here names the actual rule
        rather than showing up as one more line in a long list.
        """
        for name in ("views_admin.py", "views_user.py", "views_game.py", "embeds.py"):
            path = Path("cogs/requests") / name
            self.assertEqual([], sql_literals(path), f"{path} should not contain SQL")

    def test_every_allowance_is_still_needed(self):
        """A module that no longer has SQL should come off the list."""
        for path, reason in ALLOWED.items():
            self.assertTrue(path.exists(), f"stale allowance: {path}")
            self.assertNotEqual([], sql_literals(path), f"{path} is now clean; drop it from ALLOWED ({reason})")

    def test_the_detector_actually_detects(self):
        import tempfile

        source = (
            "QUERY = 'SELECT a FROM t'\n"
            "OTHER = 'UPDATE t SET a = 1'\n"
            "UI_COPY = 'Select the correct game from IGDB:'\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as handle:
            handle.write(source)
            path = Path(handle.name)

        # The third line is UI copy that a bare-keyword match would flag.
        self.assertEqual(
            ["SELECT a FROM t", "UPDATE t SET a = 1"],
            [sql for _, sql in sql_literals(path)],
        )


if __name__ == "__main__":
    unittest.main()
