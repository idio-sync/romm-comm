import unittest
from pathlib import Path


class ImportBoundaryTests(unittest.TestCase):
    def test_cogs_do_not_import_admin_check_from_bot_entrypoint(self):
        offenders = []
        # rglob, not glob: cogs/requests is a package now, and a non-recursive
        # scan left the ten modules this rule most applies to unchecked.
        for path in Path("cogs").rglob("*.py"):
            if "from bot import is_admin" in path.read_text(encoding="utf-8"):
                offenders.append(str(path))

        self.assertEqual([], offenders)

    def test_the_scan_reaches_inside_the_requests_package(self):
        """A rule that silently stops checking is worse than no rule."""
        scanned = {str(path) for path in Path("cogs").rglob("*.py")}

        self.assertIn(str(Path("cogs/requests/cog.py")), scanned)
