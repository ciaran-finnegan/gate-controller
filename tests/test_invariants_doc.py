"""docs/invariants.md names the tests that guard each invariant. They must exist.

A guard that a refactor deletes or renames is a guard nobody notices is gone:
on 2026-10-05 the fast lane's guarantee (#165) had been broken for two weeks
by two later PRs whose own tests passed. This keeps the document and the
suite in step -- rename a guard, and this fails until the document says so.
"""
import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "invariants.md"
REFERENCE = re.compile(r"`(tests/[\w/]+\.py)::([\w:]+)`")


def _defined(path: Path) -> set[str]:
    """Every test as `name` and `Class::name`."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name.startswith("test_"):
                    names.add(item.name)
                    names.add(f"{node.name}::{item.name}")
        elif isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            names.add(node.name)
    return names


class InvariantGuardTests(unittest.TestCase):
    def test_every_guard_named_in_the_invariants_document_exists(self):
        references = REFERENCE.findall(DOC.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(references), 10, "the document lost its guards")
        for file, name in references:
            with self.subTest(guard=f"{file}::{name}"):
                path = ROOT / file
                self.assertTrue(path.is_file(), f"{file} does not exist")
                self.assertTrue(name in _defined(path), f"{file} has no test {name}")

    def test_every_invariant_names_at_least_one_guard(self):
        sections = re.split(r"^## ", DOC.read_text(encoding="utf-8"), flags=re.MULTILINE)[1:]
        self.assertGreaterEqual(len(sections), 10)
        for section in sections:
            with self.subTest(invariant=section.splitlines()[0]):
                self.assertRegex(section, REFERENCE)


if __name__ == "__main__":
    unittest.main()
