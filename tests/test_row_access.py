"""Architecture test: database rows are read by column name, not by position.

MasterDatabase.get_connection sets row_factory, so every row a cog sees is an
sqlite3.Row. Row still supports integer indexing and unpacking, which is what
made the migration to named access safe - and also what lets positional reads
creep back in without anything failing.

They are worth keeping out. Column order is not stable: migrate_for_ggrequestz
appends columns with ALTER TABLE, so a database that ran its migrations in a
different order hands back the same columns in a different order. A positional
read against it is silently wrong rather than loud.

This walks the AST rather than grepping, so it catches any variable name
rather than the handful someone thought to search for. The shapes it knows
about are listed in the detector tests at the bottom; each one is there
because it was once missed:

  row[0]                     a column of a fetched row
  row[-1]                    the same, and a negative literal is a UnaryOp
  rows[i][0]                 a column of one row out of a list
  self.requests[i][0]        the same, through an attribute
  for row in rows: row[0]    the loop shape, which hid the migration reads
  a, b = await cur.fetchone()   unpacking is positional by construction
  for a, b in rows:             so is this
  row := await cur.fetchone()   a walrus binds a row like an assignment
  row = await repo.get_x()      rows now arrive from repositories, not cursors
"""

import ast
import unittest
from pathlib import Path

CURSOR_METHODS = {"fetchone", "fetchall", "fetchmany"}
EXECUTE_METHODS = {"execute", "executemany", "executescript"}

REPOSITORY = Path("cogs/requests/repo.py")

# Legacy-database migration code reads schemas it cannot know the shape of, on
# raw connections that have no row_factory. Positional access is correct there.
#
# Keyed by (file, enclosing function) rather than by file: exempting a whole
# module would quietly cover every future query in it, and an exemption whose
# stated reason is narrower than its effect is worse than none. Keyed by path
# rather than by filename, so it cannot be inherited by some other file that
# happens to share a name.
EXEMPT = {
    (Path("database_manager.py"), "MasterDatabase.migrate_existing_databases"):
        "reads pre-migration databases whose column order it cannot know",
}

# Attributes that hold a list of rows. Unlike a local, an attribute is assigned
# in one method and read in another, so provenance cannot be traced within a
# single function. Most are discovered automatically below; this names the ones
# that are not, because they are assigned from a constructor argument rather
# than from a query. An earlier version of this test tracked only locals, and
# 16 reads of `self.requests[i][1]` sat behind that gap until one of them
# started crashing.
ROW_LIST_ATTRIBUTES = {"requests"}


def _unwrap(node):
    return node.value if isinstance(node, ast.Await) else node


def _fetch_kind(node):
    """'row' for a fetchone call, 'rows' for a fetchall, else None."""
    node = _unwrap(node)
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
        return None
    if node.func.attr == "fetchone":
        return "row"
    if node.func.attr in CURSOR_METHODS:
        return "rows"
    return None


def repository_row_methods(path=REPOSITORY):  # noqa: C901 - walks two AST shapes to find row-returning methods
    """Which repository methods hand back raw rows, and whether one or many.

    Read out of the repository rather than listed here, so a query method added
    tomorrow is covered the day it is written. A method that unwraps its row -
    `return row['count'] if row else 0` - is not a row source and is left out,
    so indexing what it returns is nobody's business but the caller's.
    """
    single, many = set(), set()
    if not path.exists():
        return single, many

    tree = ast.parse(path.read_text(encoding="utf-8"))
    for klass in tree.body:
        if not isinstance(klass, ast.ClassDef):
            continue
        for func in klass.body:
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue

            bound = {}
            for node in ast.walk(func):
                if isinstance(node, ast.Assign):
                    kind = _fetch_kind(node.value)
                    if kind:
                        for target in node.targets:
                            if isinstance(target, ast.Name):
                                bound[target.id] = kind

            for node in ast.walk(func):
                if not isinstance(node, ast.Return) or node.value is None:
                    continue
                kind = _fetch_kind(node.value)
                if kind is None and isinstance(node.value, ast.Name):
                    kind = bound.get(node.value.id)
                if kind == "row":
                    single.add(func.name)
                elif kind == "rows":
                    many.add(func.name)

    return single, many


def row_list_attributes(tree):
    """Attribute names anywhere in `tree` that get assigned a list of rows."""
    found = set(ROW_LIST_ATTRIBUTES)
    row_methods, row_list_methods = repository_row_methods()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        call = _unwrap(node.value)
        is_rows = _fetch_kind(node.value) == "rows" or (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr in row_list_methods
        )
        if is_rows:
            for target in node.targets:
                if isinstance(target, ast.Attribute):
                    found.add(target.attr)
    return found


class RowVariableCollector(ast.NodeVisitor):
    """Track names bound to a row, then flag integer reads of them.

    fetchone gives a row, so `row[0]` is a column read. fetchall gives a list
    of rows, so `rows[0]` is picking a row - fine - and `rows[0][1]` is the
    column read. The two cases are kept apart.

    Local bindings are scoped to the method they are made in, so a `result`
    that holds a row in one place cannot make an unrelated `result[0]`
    elsewhere look like a column read. Nested functions share their enclosing
    scope, because a callback defined inside a method really does see it.
    """

    def __init__(self, attributes):
        self.attributes = attributes
        self.row_methods, self.row_list_methods = repository_row_methods()
        self.row_names = set()
        self.row_list_names = set()
        self.cursor_names = set()
        self.offences = []
        self.scope = []
        self.classes = []

    # -------------------------------------------------------------- scoping

    def visit_ClassDef(self, node):
        self.classes.append(node.name)
        self.generic_visit(node)
        self.classes.pop()

    def _visit_function(self, node):
        outermost = not self.scope
        if outermost:
            saved = (self.row_names, self.row_list_names, self.cursor_names)
            self.row_names, self.row_list_names, self.cursor_names = set(), set(), set()
            prefix = ".".join(self.classes)
            self.scope.append(f"{prefix}.{node.name}" if prefix else node.name)
        else:
            self.scope.append(self.scope[-1])

        self.generic_visit(node)

        self.scope.pop()
        if outermost:
            self.row_names, self.row_list_names, self.cursor_names = saved

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def _where(self):
        return self.scope[0] if self.scope else "<module>"

    def _flag(self, node, description):
        self.offences.append((node.lineno, description, self._where()))

    # ------------------------------------------------------------- bindings

    def _source_kind(self, value):
        """'row', 'rows' or 'cursor' for what `value` evaluates to."""
        kind = _fetch_kind(value)
        if kind:
            return kind
        # A name already known to hold a row, so aliasing it - and, more to the
        # point, unpacking it - is still a row operation. The four unpacks this
        # detector was widened for all read a row out of a loop variable or a
        # repository call first, so nothing at the unpack itself is a call.
        if isinstance(value, ast.Name):
            if value.id in self.row_names:
                return "row"
            if value.id in self.row_list_names:
                return "rows"
            return None
        call = _unwrap(value)
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)):
            return None
        if call.func.attr in EXECUTE_METHODS:
            return "cursor"
        if call.func.attr in self.row_methods:
            return "row"
        if call.func.attr in self.row_list_methods:
            return "rows"
        return None

    def _bind(self, target, kind, node):
        if isinstance(target, ast.Name):
            {"row": self.row_names, "rows": self.row_list_names,
             "cursor": self.cursor_names}[kind].add(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)) and kind == "row":
            self._flag(node, "unpacks a row positionally")

    def visit_Assign(self, node):
        kind = self._source_kind(node.value)
        if kind:
            for target in node.targets:
                self._bind(target, kind, node)
        self.generic_visit(node)

    def visit_AnnAssign(self, node):
        if node.value is not None:
            kind = self._source_kind(node.value)
            if kind:
                self._bind(node.target, kind, node)
        self.generic_visit(node)

    def visit_NamedExpr(self, node):
        kind = self._source_kind(node.value)
        if kind:
            self._bind(node.target, kind, node)
        self.generic_visit(node)

    def _visit_for(self, node):
        iterating_rows = (
            self._source_kind(node.iter) == "rows"
            or (isinstance(node.iter, ast.Name)
                and (node.iter.id in self.row_list_names
                     or node.iter.id in self.cursor_names))
            or (isinstance(node.iter, ast.Attribute) and node.iter.attr in self.attributes)
        )
        if iterating_rows:
            if isinstance(node.target, ast.Name):
                self.row_names.add(node.target.id)
            elif isinstance(node.target, ast.Tuple):
                self._flag(node, "unpacks a row positionally")
        self.generic_visit(node)

    visit_For = _visit_for
    visit_AsyncFor = _visit_for

    # --------------------------------------------------------------- reads

    def _integer_index(self, node):
        index = node.slice
        if isinstance(index, ast.Constant) and isinstance(index.value, int):
            return index.value
        if (isinstance(index, ast.UnaryOp) and isinstance(index.op, ast.USub)
                and isinstance(index.operand, ast.Constant)
                and isinstance(index.operand.value, int)):
            return -index.operand.value
        return None

    def _row_list_label(self, node):
        """Name for `node` if it is something holding a list of rows."""
        if isinstance(node, ast.Name) and node.id in self.row_list_names:
            return node.id
        if isinstance(node, ast.Attribute) and node.attr in self.attributes:
            return f".{node.attr}"
        return None

    def visit_Subscript(self, node):
        index = self._integer_index(node)
        if index is not None:
            inner = node.value
            # row[0] - a column of a single row.
            if isinstance(inner, ast.Name) and inner.id in self.row_names:
                self._flag(node, f"{inner.id}[{index}]")
            # rows[i][0] - a column of one row out of a list of rows.
            elif isinstance(inner, ast.Subscript):
                label = self._row_list_label(inner.value)
                if label:
                    self._flag(node, f"{label}[..][{index}]")
        self.generic_visit(node)


def positional_row_reads(path: Path):
    """(line, description, enclosing function) for every positional read."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    collector = RowVariableCollector(row_list_attributes(tree))
    collector.visit(tree)
    return collector.offences


def source_files():
    paths = sorted(Path("cogs").rglob("*.py"))
    paths += sorted(Path("integrations").glob("*.py"))
    # Globbed rather than listed, so a module added at the top level - as
    # romm_client.py was - is covered without anyone remembering to add it.
    paths += sorted(Path(".").glob("*.py"))
    return paths


def _detector_on(source):
    """Run the detector over a source string."""
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                     encoding="utf-8") as handle:
        handle.write(source)
        path = Path(handle.name)
    return positional_row_reads(path)


class RowAccessTests(unittest.TestCase):
    def test_no_positional_row_reads(self):
        offenders = {}
        for path in source_files():
            found = [
                hit for hit in positional_row_reads(path)
                if (path, hit[2]) not in EXEMPT
            ]
            if found:
                offenders[str(path)] = found

        self.assertEqual(
            {},
            offenders,
            "Read these by column name instead - column order is not stable:\n"
            + "\n".join(
                f"  {path}:{line}  {expr}  (in {where})"
                for path, hits in offenders.items()
                for line, expr, where in hits
            ),
        )

    def test_every_exemption_is_still_needed(self):
        """An exemption that no longer covers anything is a false assurance.

        The previous version of this test only checked the file existed. It
        passed while exempting a whole module in which the detector could not
        see a single read either way.
        """
        for (filename, where), reason in EXEMPT.items():
            self.assertTrue(filename.exists(), f"stale exemption: {filename}")
            covered = [
                hit for hit in positional_row_reads(filename) if hit[2] == where
            ]
            self.assertNotEqual(
                [],
                covered,
                f"{filename}:{where} has no positional reads left; "
                f"drop it from EXEMPT ({reason})",
            )

    def test_the_exemption_covers_one_function_not_a_whole_file(self):
        """Everything else in an exempted module is still checked."""
        for filename, where in EXEMPT:
            elsewhere = [
                hit for hit in positional_row_reads(filename) if hit[2] != where
            ]
            self.assertEqual(
                [],
                elsewhere,
                f"{filename} has positional reads outside {where}:\n"
                + "\n".join(f"  :{line}  {expr}  (in {scope})"
                            for line, expr, scope in elsewhere),
            )


class DetectorTests(unittest.TestCase):
    """A guard that never fires is worth nothing; prove this one fires.

    Every case below is a shape that was once invisible to this test.
    """

    def descriptions(self, source):
        return [expr for _, expr, _ in _detector_on(source)]

    def test_a_column_of_a_fetched_row(self):
        self.assertEqual(
            ["row[1]", "rows[..][2]"],
            self.descriptions(
                "async def f(db):\n"
                "    cursor = await db.execute('SELECT a, b FROM t')\n"
                "    row = await cursor.fetchone()\n"
                "    rows = await cursor.fetchall()\n"
                "    return row[1], rows[0][2], row['a']\n"
            ),
        )

    def test_picking_a_row_out_of_a_list_is_not_a_column_read(self):
        self.assertEqual(
            [],
            self.descriptions(
                "async def f(db):\n"
                "    cursor = await db.execute('SELECT a FROM t')\n"
                "    rows = await cursor.fetchall()\n"
                "    return rows[0]\n"
            ),
        )

    def test_reads_through_an_attribute(self):
        """The shape that hid 16 positional reads.

        `self.requests` is filled in one method and read in another, so
        nothing inside a single function reveals where it came from.
        """
        self.assertEqual(
            [".requests[..][1]", ".requests[..][1]"],
            self.descriptions(
                "class View:\n"
                "    def show(self):\n"
                "        return self.requests[self.index][1]\n"
                "    def other(self):\n"
                "        return self.view.requests[self.view.index][1]\n"
                "    def fine(self):\n"
                "        return self.requests[self.index]\n"
            ),
        )

    def test_a_row_list_attribute_is_discovered_from_its_assignment(self):
        """Not just the ones named in ROW_LIST_ATTRIBUTES."""
        self.assertEqual(
            [".links[..][2]"],
            self.descriptions(
                "class View:\n"
                "    async def load(self, db):\n"
                "        cursor = await db.execute('SELECT a FROM t')\n"
                "        self.links = await cursor.fetchall()\n"
                "    def show(self):\n"
                "        return self.links[self.index][2]\n"
            ),
        )

    def test_a_loop_over_a_list_of_rows(self):
        """How the migration code reads its columns, and it was invisible."""
        self.assertEqual(
            ["row[1]"],
            self.descriptions(
                "async def f(db):\n"
                "    cursor = await db.execute('SELECT a, b FROM t')\n"
                "    rows = await cursor.fetchall()\n"
                "    for row in rows:\n"
                "        print(row[1])\n"
            ),
        )

    def test_a_loop_straight_over_a_fetch(self):
        self.assertEqual(
            ["row[0]"],
            self.descriptions(
                "async def f(cursor):\n"
                "    for row in await cursor.fetchall():\n"
                "        print(row[0])\n"
            ),
        )

    def test_unpacking_a_row(self):
        self.assertEqual(
            ["unpacks a row positionally"],
            self.descriptions(
                "async def f(db):\n"
                "    cursor = await db.execute('SELECT a, b FROM t')\n"
                "    a, b = await cursor.fetchone()\n"
                "    return a, b\n"
            ),
        )

    def test_unpacking_in_a_loop_over_rows(self):
        self.assertEqual(
            ["unpacks a row positionally"],
            self.descriptions(
                "async def f(db):\n"
                "    cursor = await db.execute('SELECT a, b FROM t')\n"
                "    for a, b in await cursor.fetchall():\n"
                "        print(a, b)\n"
            ),
        )

    def test_unpacking_a_loop_variable_over_a_repository_result(self):
        """The exact shape of the four this detector was widened for.

        Nothing at the unpack is a call: the row arrived one statement earlier,
        as a loop variable or a repository return, so a detector looking only
        at the right-hand side of the unpack saw an ordinary name.
        """
        self.assertEqual(
            ["unpacks a row positionally", "unpacks a row positionally"],
            self.descriptions(
                "async def f(repo):\n"
                "    for req in await repo.list_pending():\n"
                "        a, b, c = req\n"
                "    mapping = await repo.get_by_display_name('SNES')\n"
                "    x, y = mapping\n"
            ),
        )

    def test_a_negative_index(self):
        """-1 parses as a UnaryOp, not a Constant, so it slipped through."""
        self.assertEqual(
            ["row[-1]"],
            self.descriptions(
                "async def f(cursor):\n"
                "    row = await cursor.fetchone()\n"
                "    return row[-1]\n"
            ),
        )

    def test_a_walrus_binding(self):
        self.assertEqual(
            ["row[1]"],
            self.descriptions(
                "async def f(cursor):\n"
                "    if (row := await cursor.fetchone()):\n"
                "        return row[1]\n"
            ),
        )

    def test_a_row_that_came_from_a_repository(self):
        """Rows arrive from repo methods now, not from cursors.

        Moving the SQL behind a repository moved every fetch call into one
        file. A detector that only knew about cursors could no longer see any
        of the call sites.
        """
        self.assertEqual(
            ["row[0]", "rows[..][1]"],
            self.descriptions(
                "async def f(repo):\n"
                "    row = await repo.get_igdb_info(1)\n"
                "    rows = await repo.list_pending()\n"
                "    return row[0], rows[0][1]\n"
            ),
        )

    def test_a_repository_method_that_unwraps_its_row_is_not_a_row_source(self):
        """count_pending_for_user returns a number; indexing it is the caller's business."""
        self.assertEqual(
            [],
            self.descriptions(
                "async def f(repo):\n"
                "    counts = await repo.count_pending_for_user(1)\n"
                "    return counts[0]\n"
            ),
        )

    def test_bindings_do_not_leak_between_methods(self):
        """A `result` holding a row here must not indict a `result` over there."""
        self.assertEqual(
            ["result[0]"],
            self.descriptions(
                "class C:\n"
                "    async def one(self, cursor):\n"
                "        result = await cursor.fetchone()\n"
                "        return result[0]\n"
                "    def two(self, items):\n"
                "        result = list(items)\n"
                "        return result[0]\n"
            ),
        )

    def test_a_nested_function_sees_its_enclosing_scope(self):
        self.assertEqual(
            ["row[2]"],
            self.descriptions(
                "async def outer(cursor):\n"
                "    row = await cursor.fetchone()\n"
                "    async def inner():\n"
                "        return row[2]\n"
                "    return inner\n"
            ),
        )

    def test_the_repository_is_read_for_its_row_methods(self):
        """If this stops finding them the detector silently loses its teeth."""
        single, many = repository_row_methods()
        self.assertIn("get_igdb_info", single)
        self.assertIn("list_all", many)
        self.assertNotIn("count_pending_for_user", single | many)
        self.assertNotIn("subscriber_ids", single | many)


if __name__ == "__main__":
    unittest.main()
