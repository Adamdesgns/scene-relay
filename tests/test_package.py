import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

MODULE = Path(__file__).resolve().parents[1] / "scripts/package.py"
TEST_RUNTIME = Path(__file__).resolve().parent / ".runtime" / "packaging"
spec = importlib.util.spec_from_file_location("source_package", MODULE)
package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package)


class PackageTests(unittest.TestCase):
    def setUp(self):
        TEST_RUNTIME.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=TEST_RUNTIME, prefix="package-")
        self.root = Path(self.temporary.name)
        self.root.resolve().relative_to(TEST_RUNTIME.resolve())
        for relative in package.REQUIRED_FILES:
            self.write(relative, "synthetic source\n")

    def tearDown(self):
        self.root.resolve().relative_to(TEST_RUNTIME.resolve())
        self.temporary.cleanup()

    def write(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_allowlist_excludes_runtime_private_outputs_and_assets(self):
        kept = "server/additional.py"
        self.write(kept, "# synthetic code")
        for relative in (
            "runtime/prompt.private.txt", "tests/.runtime/author.py", "tests/__pycache__/test.py",
            "server/credentials.py", "scripts/brief.PRIVATE.py", "docs/secret-notes.md",
            "docs/output/report.md", "production/index.html", "skills/demo/assets/helper.py",
            "server/unapproved.json", "docs/trace.log", "unapproved.py", ".env",
            "tests/fixtures/render-smoke/output/final.py", "tests/fixtures/render-smoke/debug.log",
        ):
            self.write(relative, "PRIVATE-TEST-SENTINEL")
        result = package.build_package(self.root)
        with zipfile.ZipFile(result["package"]) as archive:
            self.assertEqual(set(archive.namelist()), package.REQUIRED_FILES | {kept})
            for member in archive.namelist():
                self.assertNotIn(b"PRIVATE-TEST-SENTINEL", archive.read(member))

    def test_package_is_reproducible_and_manifests_are_at_archive_root(self):
        first = package.build_package(self.root)
        second = package.build_package(self.root, self.root / "dist/second.zip")
        self.assertEqual(first["sha256"], second["sha256"])
        with zipfile.ZipFile(second["package"]) as archive:
            self.assertIn("plugin.json", archive.namelist())
            self.assertIn(".codex-plugin/plugin.json", archive.namelist())
        self.assertFalse(second["installs_or_jobs_started"])

    def test_output_cannot_replace_source_or_escape_dist(self):
        for path in (self.root / "server/source.zip", self.root / "dist/../escape.zip", self.root / "dist/output.py"):
            with self.assertRaises(package.PackageError):
                package.build_package(self.root, path)

    def test_included_reparse_point_is_rejected_without_reading_it(self):
        target = self.root / "server/mcp_server.py"
        original = package.is_link_or_reparse
        with patch.object(package, "is_link_or_reparse", side_effect=lambda path: path == target or original(path)):
            with self.assertRaises(package.PackageError):
                package.collect_sources(self.root)

    def test_missing_required_file_fails_before_creating_archive(self):
        (self.root / "plugin.json").unlink()
        with self.assertRaises(package.PackageError):
            package.build_package(self.root)
        self.assertFalse((self.root / "dist/source-plugin.zip").exists())

    def test_oversized_included_file_is_rejected(self):
        self.write("server/oversized.py", "12345")
        with patch.object(package, "MAX_FILE_BYTES", 4):
            with self.assertRaises(package.PackageError):
                package.collect_sources(self.root)


if __name__ == "__main__":
    unittest.main()
