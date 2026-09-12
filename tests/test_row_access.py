"""Architecture test: database rows are read by column name, not by position.

MasterDatabase.get_connection sets row_factory, so every row a cog sees is an
sqlite3.Row. Row still supports integer indexing, which is what makes the
migration to named access safe - and also what lets positional reads creep
back in without anything failing.

They are worth keeping out. Column order is not stable: migrate_for_ggrequestz
appends columns with ALTER TABLE, so a database that ran its migrations in a
different order hands back the same columns in a different order. A positional
read against it is silently wrong rather than loud.

This test walks the AST rather than grepping, so it catches any variable name
rather than the handful someone thought to search for.
"""

import ast
import unittest
from pathlib import Path

CURSOR_METHODS = {"fetchone", "fetchall", "fetchmany"}

# Legacy-database migration code reads schemas it cannot know the shape of, on
# raw connections that have no row_factory. Positional access is correct there.
EXEMPT = {
    "database_manager.py": "reads pre-migration databases of unknown shape",
}


def _is_cursor_fetch(node):
    """True for `cursor.fetchone()` / `await cursor.fetchall()` and friends."""
    if isinstance(node, ast.Await):
        node = node.value
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in CURSOR_METHODS
    )


# Attributes that hold a list of rows. Unlike a local, an attribute is
# assigned in one method and read in another, so provenance cannot be traced
# within a single function - these are named instead. An earlier version of
# this test tracked only locals, and 16 reads of `self.requests[i][1]` sat
# behind that gap until one of them started crashing.
ROW_LIST_ATTRIBUTES = {"requests"}


class RowVariableCollector(ast.NodeVisitor):
    """Track names bound to a fetch result, then flag integer reads of them.

    fetchone gives a row, so `row[0]` is a column read. fetchall gives a list
    of rows, so `rows[0]` is picking a row - fine - and `rows[0][1]` is the
    column read. The two cases are kept apart.
    """

    def __init__(self):
        self.row_names = set()
        self.row_list_names = set()
        self.offences = []

    def visit_Assign(self, node):
        if _is_cursor_fetch(node.value):
            method = (node.value.value if isinstance(node.value, ast.Await) else node.value).func.attr
            bucket = self.row_names if method == "fetchone" else self.row_list_names
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bucket.add(target.id)
        self.generic_visit(node)

    def _integer_index(self, node):
        if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, int):
            return node.slice.value
        return None

    def _row_list_label(self, node):
        """Name for `node` if it is something holding a list of rows."""
        if isinstance(node, ast.Name) and node.id in self.row_list_names:
            return node.id
        # self.requests[...] / self.view.requests[...]
        if isinstance(node, ast.Attribute) and node.attr in ROW_LIST_ATTRIBUTES:
            return f".{node.attr}"
        return None

    def visit_Subscript(self, node):
        index = self._integer_index(node)
        if index is not None:
            inner = node.value
            # row[0] - a column of a single fetched row.
            if isinstance(inner, ast.Name) and inner.id in self.row_names:
                self.offences.append((node.lineno, f"{inner.id}[{index}]"))
            # rows[i][0] - a column of one row out of a list of rows.
            elif isinstance(inner, ast.Subscript):
                label = self._row_list_label(inner.value)
                if label:
                    self.offences.append((node.lineno, f"{label}[..][{index}]"))
        self.generic_visit(node)


def positional_row_reads(path: Path):
    collector = RowVariableCollector()
    collector.visit(ast.parse(path.read_text(encoding="utf-8")))
    return collector.offences


class RowAccessTests(unittest.TestCase):
    def source_files(self):
        paths = sorted(Path("cogs").rglob("*.py"))
        paths += sorted(Path("integrations").glob("*.py"))
        paths += [Path("bot.py"), Path("database_manager.py")]
        return [p for p in paths if p.name not in EXEMPT]

    def test_no_positional_row_reads(self):
        offenders = {}
        for path in self.source_files():
            found = positional_row_reads(path)
            if found:
                offenders[str(path)] = found

        self.assertEqual(
            {},
            offenders,
            "Read these by column name instead - column order is not stable:\n"
            + "\n".join(
                f"  {path}:{line}  {expr}"
                for path, hits in offenders.items()
                for line, expr in hits
            ),
        )

    def test_the_detector_actually_detects(self):
        """A guard that never fires is worth nothing; prove this one fires."""
        import tempfile

        source = (
            "async def f(db):\n"
            "    cursor = await db.execute('SELECT a, b FROM t')\n"
            "    row = await cursor.fetchone()\n"
            "    rows = await cursor.fetchall()\n"
            "    return row[1], rows[0][2], row['a']\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as handle:
            handle.write(source)
            path = Path(handle.name)

        found = positional_row_reads(path)
        self.assertEqual(["row[1]", "rows[..][2]"], [expr for _, expr in found])

    def test_the_detector_sees_reads_through_an_attribute(self):
        """A list of rows kept on self is still a list of rows.

        This is the shape that hid 16 positional reads: `self.requests` is
        filled in one method and read in another, so nothing inside a single
        function reveals where it came from.
        """
        import tempfile

        source = "\n".join([
            "class View:",
            "    def show(self):",
            "        return self.requests[self.index][1]",
            "    def other(self):",
            "        return self.view.requests[self.view.index][1]",
            "    def fine(self):",
            "        return self.requests[self.index]",
        ])
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as handle:
            handle.write(source)
            path = Path(handle.name)

        self.assertEqual(
            [".requests[..][1]", ".requests[..][1]"],
            [expr for _, expr in positional_row_reads(path)],
        )

    def test_every_exemption_names_a_real_file(self):
        for filename in EXEMPT:
            self.assertTrue(Path(filename).exists(), f"stale exemption: {filename}")


if __name__ == "__main__":
    unittest.main()
