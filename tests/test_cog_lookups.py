"""Cogs found by string name must exist, and must have what is called on them.

A get_cog("Request") lookup is an unchecked string. Renaming the cog, or the
method the caller goes on to use, breaks nothing at import time and nothing in
the test suite -- it breaks inside a button handler, in production, where the
only symptom is an interaction that never completes.

This is checked here rather than asserted at boot because several cogs are
legitimately absent depending on configuration (REQUESTS_ENABLED,
ENABLE_USER_MANAGER, and the whole integrations folder), so a runtime check
would either be wrong about optional cogs or too weak to catch anything. A
static check has neither problem, and fails in CI rather than in front of a
user.

This is the check that would have caught the missing
GGRequestzIntegration.update_request_status, which had three call sites, no
implementation, and a green test suite.

What it does not check is type. It proves the attribute exists, not that it is
the thing the caller wants. IGDBHandler is the live example: it holds an
IGDBClient on `self.igdb` and also declares a slash command `async def igdb`,
so renaming the attribute leaves `handler.igdb` resolving perfectly well -- to
the command. Catching that needs a type checker, not this.
"""

import ast
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent

SOURCE_DIRS = ("cogs", "integrations")
SOURCE_FILES = [ROOT / "bot.py"] + [
    path
    for directory in SOURCE_DIRS
    for path in sorted((ROOT / directory).rglob("*.py"))
]

# Attributes every cog inherits from discord.ext.commands.Cog, which no class
# in this tree declares for itself.
COG_INHERITED = {
    "qualified_name", "description", "cog_unload", "bot_check", "bot_check_once",
    "cog_check", "cog_command_error", "cog_before_invoke", "cog_after_invoke",
    "get_commands", "walk_commands", "get_listeners", "has_error_handler",
}

# There is deliberately no EXEMPT list. The only unresolvable uses in the tree
# are the two missing ggrequestz methods, and those are reached through
# getattr() rather than attribute access, so they never reach this check at
# all -- test_ggr_sync.py is where their absence is pinned. Add one here only
# with a (cog, attribute) key and a stated reason, never a bare cog name: a
# whole-cog exemption would cover every method added to it later.


def _string_constants(tree):
    """Module-level NAME = "literal" bindings, so get_cog(CONST) resolves."""
    constants = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        constants[target.id] = node.value.value
    return constants


def _imported_constants(tree, per_module):
    """An imported name carries the value it was given where it was defined."""
    resolved = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                for constants in per_module.values():
                    if alias.name in constants:
                        resolved[alias.asname or alias.name] = constants[alias.name]
    return resolved


def cog_classes():
    """Every class in the tree that is a discord Cog, by name."""
    found = {}
    for path in SOURCE_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            base_names = {
                base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", "")
                for base in node.bases
            }
            if "Cog" in base_names:
                found[node.name] = (path, node)
    return found


def class_members(node):
    """Everything reachable as an attribute: methods, class vars, self.x = ..."""
    members = set(COG_INHERITED)
    for child in node.body:
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            members.add(child.name)
        elif isinstance(child, ast.Assign):
            members.update(t.id for t in child.targets if isinstance(t, ast.Name))
        elif isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
            members.add(child.target.id)
    for sub in ast.walk(node):
        if isinstance(sub, ast.Assign):
            for target in sub.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                ):
                    members.add(target.attr)
        elif isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Attribute):
            if isinstance(sub.target.value, ast.Name) and sub.target.value.id == "self":
                members.add(sub.target.attr)
    return members


def _lookup_name(call, constants):
    """The cog name a get_cog(...) call asks for, if it is knowable."""
    if not (
        isinstance(call.func, ast.Attribute)
        and call.func.attr == "get_cog"
        and len(call.args) == 1
    ):
        return None
    arg = call.args[0]
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    if isinstance(arg, ast.Name):
        return constants.get(arg.id)
    return None


def _binding_target(node):
    """The local name or self-attribute a get_cog result is stored under."""
    if not isinstance(node, ast.Assign) or len(node.targets) != 1:
        return None
    target = node.targets[0]
    if isinstance(target, ast.Name):
        return target.id
    if (
        isinstance(target, ast.Attribute)
        and isinstance(target.value, ast.Name)
        and target.value.id == "self"
    ):
        return "self." + target.attr
    return None


def _attribute_uses(scope, binding):
    """Attributes read off `binding` anywhere inside `scope`."""
    used = set()
    for node in ast.walk(scope):
        if not isinstance(node, ast.Attribute):
            continue
        value = node.value
        if isinstance(value, ast.Name) and value.id == binding:
            used.add(node.attr)
        elif (
            binding.startswith("self.")
            and isinstance(value, ast.Attribute)
            and value.attr == binding[len("self."):]
            and isinstance(value.value, ast.Name)
            and value.value.id == "self"
        ):
            used.add(node.attr)
    return used


def collect_lookups():
    """(cog, attribute, file, line) for everything read off a get_cog result."""
    per_module = {}
    trees = {}
    for path in SOURCE_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        trees[path] = tree
        per_module[path] = _string_constants(tree)

    lookups, names_only = set(), set()
    for path, tree in trees.items():
        constants = dict(per_module[path])
        constants.update(_imported_constants(tree, per_module))

        scopes = [tree] + [
            n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ]
        for scope in scopes:
            for node in ast.walk(scope):
                if not isinstance(node, ast.Assign):
                    continue
                if not isinstance(node.value, ast.Call):
                    continue
                cog_name = _lookup_name(node.value, constants)
                if cog_name is None:
                    continue
                names_only.add((cog_name, path, node.lineno))
                binding = _binding_target(node)
                if binding is None:
                    continue
                for attr in _attribute_uses(scope, binding):
                    lookups.add((cog_name, attr, path, node.lineno))
    return sorted(lookups), sorted(names_only)


class CogLookupTests(unittest.TestCase):
    def test_every_cog_looked_up_by_name_exists(self):
        classes = cog_classes()
        _, names = collect_lookups()
        self.assertTrue(names, "found no get_cog() call sites; the scanner is broken")
        for cog_name, path, line in names:
            where = f"{path.relative_to(ROOT)}:{line}"
            with self.subTest(cog=cog_name, at=where):
                self.assertIn(
                    cog_name, classes,
                    f"{where} looks up '{cog_name}', which is not a Cog class in this tree.",
                )

    def test_every_attribute_used_on_a_looked_up_cog_exists(self):
        classes = cog_classes()
        lookups, _ = collect_lookups()
        self.assertTrue(lookups, "found no attribute uses; the scanner is broken")
        for cog_name, attr, path, line in lookups:
            if cog_name not in classes:
                continue
            where = f"{path.relative_to(ROOT)}:{line}"
            with self.subTest(cog=cog_name, attr=attr, at=where):
                self.assertIn(
                    attr, class_members(classes[cog_name][1]),
                    f"{where} uses {cog_name}.{attr}, which {cog_name} does not define.",
                )


class ScannerTests(unittest.TestCase):
    """The scanner's own behaviour, on source it is handed rather than the tree.

    Mutating real files to test this is unreliable: the obvious mutation of
    IGDBHandler.igdb cannot fail, because a same-named slash command keeps the
    attribute resolvable. These pin the mechanism directly.
    """

    SOURCE = '''
class Thing(commands.Cog):
    CLASS_LEVEL = 1

    def __init__(self, bot):
        self.assigned = bot

    async def a_method(self):
        pass


class Caller:
    async def go(self):
        found = self.bot.get_cog("Thing")
        await found.a_method()
        print(found.assigned, found.CLASS_LEVEL, found.missing)

    async def stored(self):
        self.thing = self.bot.get_cog("Thing")
        print(self.thing.also_missing)
'''

    def _collect(self):
        tree = ast.parse(self.SOURCE)
        classes = {
            n.name: n for n in ast.walk(tree)
            if isinstance(n, ast.ClassDef)
        }
        uses = set()
        for scope in [n for n in ast.walk(tree)
                      if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            for node in ast.walk(scope):
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                    name = _lookup_name(node.value, {})
                    binding = _binding_target(node)
                    if name and binding:
                        uses.update((name, a) for a in _attribute_uses(scope, binding))
        return classes, uses

    def test_it_finds_methods_class_vars_and_self_assignments(self):
        classes, _ = self._collect()
        members = class_members(classes["Thing"])
        self.assertIn("a_method", members)
        self.assertIn("CLASS_LEVEL", members)
        self.assertIn("assigned", members)

    def test_it_sees_uses_through_a_local_binding(self):
        _, uses = self._collect()
        self.assertIn(("Thing", "a_method"), uses)
        self.assertIn(("Thing", "missing"), uses)

    def test_it_sees_uses_through_a_self_attribute_binding(self):
        _, uses = self._collect()
        self.assertIn(("Thing", "also_missing"), uses)

    def test_a_missing_attribute_is_actually_detected(self):
        """The whole point: this is what fails when something is renamed."""
        classes, uses = self._collect()
        members = class_members(classes["Thing"])
        undefined = sorted(attr for cog, attr in uses if attr not in members)
        self.assertEqual(undefined, ["also_missing", "missing"])
