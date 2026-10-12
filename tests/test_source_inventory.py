from pathlib import Path
import tempfile
import unittest

from scripts.source_inventory import inspect, inventory, main, source_files


class SourceInventoryTests(unittest.TestCase):
    def test_inventory_counts_substantive_python_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "sample.py"
            path.write_text('"""Module documentation."""\n\n# note\nvalue = 1\n', encoding="utf-8")
            result = inspect(path, root)
            self.assertEqual(result.physical, 4)
            self.assertEqual(result.substantive, 1)
            self.assertEqual(result.comments, 2)

    def test_inventory_excludes_dependency_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / "vendor").mkdir()
            (root / "src" / "app.py").write_text("value = 1\n", encoding="utf-8")
            (root / "vendor" / "library.py").write_text("value = 1\n", encoding="utf-8")
            self.assertEqual([path.name for path in source_files(root)], ["app.py"])

    def test_inventory_groups_languages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text("value = 1\n", encoding="utf-8")
            (root / "app.js").write_text("const value = 1;\n", encoding="utf-8")
            result = inventory(root)
            self.assertEqual(result.substantive, 2)
            self.assertEqual(set(result.by_language), {"JavaScript", "Python"})

    def test_minimum_exit_code_reflects_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text("value = 1\n", encoding="utf-8")
            self.assertEqual(main((str(root), "--minimum", "1")), 0)
            self.assertEqual(main((str(root), "--minimum", "2")), 1)


if __name__ == "__main__":
    unittest.main()
