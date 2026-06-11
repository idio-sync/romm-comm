from pathlib import Path
import unittest


class ImportBoundaryTests(unittest.TestCase):
    def test_cogs_do_not_import_admin_check_from_bot_entrypoint(self):
        offenders = []
        for path in Path("cogs").glob("*.py"):
            if "from bot import is_admin" in path.read_text(encoding="utf-8"):
                offenders.append(str(path))

        self.assertEqual([], offenders)
