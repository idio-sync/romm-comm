"""Architecture test: SQL belongs in a repository, not in a Discord view.

Query text in a button callback means schema knowledge is spread across the UI,
there is no single place to change a query, and the logic can only be tested by
constructing a discord.ui.View.

Two rules, because they catch different mistakes.

The first is structural: a module that is not a repository must not call
.execute() at all. That holds however the query is spelled, wherever the text
came from, and whatever it does - a lowercase SELECT, a string built by
join(), an ALTER TABLE - so it cannot be evaded by writing the query
differently.

The second still reads string literals, because a query *written* in a view
and handed to something else to run crosses the same boundary without the view
ever running it. That one is spelling-dependent and its limits are documented
at SQL_STATEMENT.

This is a ratchet, not a finished state. Modules still holding SQL are listed
in ALLOWED with the reason; the list is meant to shrink.
"""

import ast
import re
import unittest
from pathlib import Path

EXECUTE_METHODS = {"execute", "executemany", "executescript"}

# Deliberately case-sensitive. Structure alone cannot tell SQL from prose -
# "Select the correct game from IGDB:" is a real button label in this codebase
# and matches SELECT ... FROM perfectly well. Every query here is written with
# uppercase keywords, so that convention is what the match keys on. This is a
# secondary net only: anything actually run is caught by execute_calls
# regardless of how it is written.
#
# The window between SELECT and FROM is wide enough for any column list this
# schema could grow - it was 200, which an explicit list of 40 columns already
# overruns - but not unbounded, so a SELECT and a FROM in unrelated sentences
# of one long string cannot pair up.
SQL_STATEMENT = re.compile(
    r"\bSELECT\b[\s\S]{0,1000}?\bFROM\b"
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


def execute_calls(path: Path):
    """Every cursor-style execute call in `path`.

    Structural, so it does not care how the query is spelled or whether the
    text is even visible here - `conn.execute(QUERY)` counts.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        (node.lineno, f"{ast.unparse(node.func)}(...)")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in EXECUTE_METHODS
    ]


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


def modules():
    paths = sorted(Path("cogs").rglob("*.py"))
    paths += sorted(Path("integrations").glob("*.py"))
    # Globbed rather than listed, so a module added at the top level - as
    # romm_client.py was - is covered without anyone remembering to add it.
    paths += sorted(Path(".").glob("*.py"))
    return [p for p in paths if p not in REPOSITORIES]


def _detector_on(source):
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                     encoding="utf-8") as handle:
        handle.write(source)
        return Path(handle.name)


class SqlBoundaryTests(unittest.TestCase):
    def test_only_repositories_run_queries(self):
        """The structural rule: no .execute() outside a repository."""
        offenders = {}
        for path in modules():
            if path in ALLOWED:
                continue
            found = execute_calls(path)
            if found:
                offenders[str(path)] = found

        self.assertEqual(
            {},
            offenders,
            "Run these through a repository (cogs/requests/repo.py or "
            "database_manager.py):\n"
            + "\n".join(
                f"  {path}:{line}  {call}"
                for path, hits in offenders.items()
                for line, call in hits
            ),
        )

    def test_sql_stays_out_of_the_ui_layer(self):
        """The text rule: no query written outside a repository either."""
        offenders = {}
        for path in modules():
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
            self.assertEqual([], execute_calls(path), f"{path} should run no queries")
            self.assertEqual([], sql_literals(path), f"{path} should not contain SQL")

    def test_every_allowance_is_still_needed(self):
        """A module that no longer touches the database should come off the list."""
        for path, reason in ALLOWED.items():
            self.assertTrue(path.exists(), f"stale allowance: {path}")
            self.assertNotEqual(
                [],
                execute_calls(path) + sql_literals(path),
                f"{path} is now clean; drop it from ALLOWED ({reason})",
            )


class DetectorTests(unittest.TestCase):
    def test_execute_calls_are_found_however_the_query_is_written(self):
        """Each of these evades the text matcher; none evades this one."""
        path = _detector_on(
            "async def f(conn, cols):\n"
            "    await conn.execute('select a from t')\n"
            "    await conn.execute('SELECT a ' + 'FROM t')\n"
            "    await conn.execute(' '.join(['SELECT a', 'FROM t']))\n"
            "    await conn.execute('ALTER TABLE t ADD COLUMN x INTEGER')\n"
            "    await conn.execute('DELETE t WHERE a = 1')\n"
            "    await conn.executemany(QUERY, rows)\n"
        )
        self.assertEqual(
            ["conn.execute(...)"] * 5 + ["conn.executemany(...)"],
            [call for _, call in execute_calls(path)],
        )

    def test_execute_calls_ignore_a_module_with_no_database_access(self):
        path = _detector_on("def f(view):\n    return view.children\n")
        self.assertEqual([], execute_calls(path))

    def test_the_text_matcher_actually_matches(self):
        path = _detector_on(
            "QUERY = 'SELECT a FROM t'\n"
            "OTHER = 'UPDATE t SET a = 1'\n"
            "UI_COPY = 'Select the correct game from IGDB:'\n"
        )
        # The third line is UI copy that a bare-keyword match would flag.
        self.assertEqual(
            ["SELECT a FROM t", "UPDATE t SET a = 1"],
            [sql for _, sql in sql_literals(path)],
        )

    def test_the_text_matcher_sees_f_string_queries(self):
        """It did not, once, and four queries hid behind that.

        Interpolating a column list is the natural way to write these, so a
        detector that only looked at plain string constants called a module
        clean while it still held queries.
        """
        path = _detector_on(
            "COLUMNS = 'a, b'\n"
            "QUERY = f'SELECT {COLUMNS} FROM requests ORDER BY created_at'\n"
        )
        self.assertEqual(
            ["SELECT  FROM requests ORDER BY created_at"],
            [sql for _, sql in sql_literals(path)],
        )

    def test_the_text_matcher_reaches_past_a_long_column_list(self):
        """REQUEST_COLUMNS is 17 names; the window used to stop at 200 chars."""
        columns = ", ".join(f"column_{i}" for i in range(40))
        path = _detector_on(f"QUERY = 'SELECT {columns} FROM requests'\n")
        self.assertEqual(1, len(sql_literals(path)))


if __name__ == "__main__":
    unittest.main()
