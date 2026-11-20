"""Run every offline product test; missing optional test prerequisites fail qualification.

`tests/` also holds the methodology's pytest governance proofs, which run through
`./bin/test`. The product suite is every `tests/test_*.py` module that imports unittest.
"""
import ast
from pathlib import Path
import sys
import unittest


def imports_unittest(path: Path) -> bool:
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.Import) and any(a.name == "unittest" for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and node.module == "unittest":
            return True
    return False


if __name__ == "__main__":
    directory = Path("tests").resolve()
    names = [p.stem for p in sorted(directory.glob("test_*.py")) if imports_unittest(p)]
    if not names:
        raise SystemExit("no product test modules found")
    sys.path.insert(0, str(directory))
    suite = unittest.defaultTestLoader.loadTestsFromNames(names)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() and not result.skipped else 1)
