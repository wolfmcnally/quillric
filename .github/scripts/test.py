"""Run every offline test; missing optional test prerequisites fail qualification."""
import unittest

if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.discover("tests")
    )
    raise SystemExit(0 if result.wasSuccessful() and not result.skipped else 1)
