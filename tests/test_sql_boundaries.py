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
    Path("cogs/emoji_manager.py"): "no repository for emoji_sync_state yet",
    Path("cogs/recent_roms.py"): "no repository for posted_roms yet",
}


def _literal_text(node):
    """The fixed text of a string node, or None if it is not one.

    f-strings count. An earlier version of this test only looked at
    ast.Constant, so `f"SELECT {COLUMNS} FROM requests"` was invisible to it
    and four queries sat in a module this test called clean.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        # Interpolations are unknowable here; the literal parts are enough to
        # recognise a query, and a placeholder is not going to supply the
        # missing keyword.
        return "".join(
            part.value for part in node.values
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        )
    return None


def sql_literals(path: Path):
    """Every string in `path` that looks like a SQL statement."""
    tree = ast.parse(path.read_text(encoding="utf-8"))

    # An f-string's pieces are Constants in their own right. Collect them once
    # so they can be skipped; the enclosing JoinedStr already covers them.
    nested_in_fstrings = {
        id(part)
        for node in ast.walk(tree)
        if isinstance(node, ast.JoinedStr)
        for part in node.values
    }

    found = []
    for node in ast.walk(tree):
        if id(node) in nested_in_fstrings:
            continue
        text = _literal_text(node)
        if text and SQL_STATEMENT.search(text):
            lines = [line for line in text.strip().splitlines() if line.strip()]
            found.append((node.lineno, lines[0].strip()[:70] if lines else ""))
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

    def test_the_whole_requests_package_is_sql_free(self):
        """The part of the ratchet that has already been pulled in.

        Called out separately so a regression here names the actual rule
        rather than showing up as one more line in a long list. Every query
        the requests feature makes now goes through repo.py.
        """
        for path in sorted(Path("cogs/requests").glob("*.py")):
            if path.name == "repo.py":
                continue
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

    def test_the_detector_sees_f_string_queries(self):
        """It did not, once, and four queries hid behind that.

        Interpolating a column list is the natural way to write these, so a
        detector that only looked at plain string constants called a module
        clean while it still held queries.
        """
        import tempfile

        source = (
            "COLUMNS = 'a, b'\n"
            "QUERY = f'SELECT {COLUMNS} FROM requests ORDER BY created_at'\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as handle:
            handle.write(source)
            path = Path(handle.name)

        self.assertEqual(
            ["SELECT  FROM requests ORDER BY created_at"],
            [sql for _, sql in sql_literals(path)],
        )


if __name__ == "__main__":
    unittest.main()
