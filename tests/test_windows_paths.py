"""Offline path-budget and atomic-write regressions; no provider or renderer."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import uuid

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE))
from engine import Config, Engine, EngineError
from engine import core

# Inherit directory ACLs, as the engine tests do. Windows sandbox TEMP and
# LOCALAPPDATA may point at a much longer redirected container directory.
TEST_BASE = ((Path.home() / "AppData/Local/Temp") if os.name == "nt"
             else Path(tempfile.gettempdir())) / "sr-paths"


def sized_path(base: Path, units: int) -> Path:
    """Make a deterministic lexical path with modest component lengths."""
    result = base
    while units - core.windows_path_units(result) > 65:
        result /= "p" * 40
    remainder = units - core.windows_path_units(result)
    if remainder < 2:
        raise AssertionError("Fixture base is too long")
    return result / ("p" * (remainder - 1))


class PathTests(unittest.TestCase):
    def setUp(self):
        self.root = TEST_BASE / uuid.uuid4().hex[:12]
        self.root.mkdir(parents=True)

    def tearDown(self):
        target = self.root.resolve()
        if not target.is_relative_to(TEST_BASE.resolve()) or target == TEST_BASE.resolve():
            raise AssertionError("Refusing cleanup outside owned fixture directory")
        shutil.rmtree(target)

    def config(self, root):
        return Config(data_root=str(root), test_mode=True,
                      min_free_disk_bytes=0, min_free_memory_bytes=0)

    def test_atomic_collision_is_retried_without_removing_other_writer_file(self):
        target = self.root / "state.json"
        target.write_text('{"old":true}', encoding="utf-8")
        collision = self.root / ".1111111111"
        collision.write_text("other writer", encoding="utf-8")
        values = [uuid.UUID(hex="1" * 32), uuid.UUID(hex="2" * 32)]
        with mock.patch.object(core.uuid, "uuid4", side_effect=values):
            core.atomic_json(target, {"new": True})
        self.assertEqual({"new": True}, core.read_json(target))
        self.assertEqual("other writer", collision.read_text(encoding="utf-8"))
        self.assertFalse((self.root / ".2222222222").exists())

    def test_atomic_collision_limit_keeps_existing_state_and_collision(self):
        target = self.root / "state.json"
        target.write_text('{"old":true}', encoding="utf-8")
        collision = self.root / ".1111111111"
        collision.write_text("other writer", encoding="utf-8")
        with mock.patch.object(core.uuid, "uuid4", return_value=uuid.UUID(hex="1" * 32)) as make_id:
            with self.assertRaisesRegex(EngineError, "temporary file is busy"):
                core.atomic_json(target, {"new": True})
        self.assertEqual(16, make_id.call_count)
        self.assertEqual({"old": True}, core.read_json(target))
        self.assertEqual("other writer", collision.read_text(encoding="utf-8"))

    def test_failed_atomic_replace_preserves_state_and_cleans_only_own_temp(self):
        target = self.root / "state.json"
        target.write_text('{"old":true}', encoding="utf-8")
        with mock.patch.object(core.os, "replace", side_effect=OSError("synthetic failure")):
            with self.assertRaises(OSError):
                core.atomic_json(target, {"new": True})
        self.assertEqual({"old": True}, core.read_json(target))
        self.assertEqual(["state.json"], sorted(item.name for item in self.root.iterdir()))

    def test_failed_json_encoding_preserves_state_and_cleans_temp(self):
        target = self.root / "state.json"
        target.write_text('{"old":true}', encoding="utf-8")
        with self.assertRaises(ValueError):
            core.atomic_json(target, {"bad": float("nan")})
        self.assertEqual({"old": True}, core.read_json(target))
        self.assertEqual(["state.json"], sorted(item.name for item in self.root.iterdir()))

    @unittest.skipUnless(os.name == "nt", "Windows path budget")
    def test_data_root_over_budget_is_rejected_before_any_mutation(self):
        root = sized_path(self.root, core.WINDOWS_DATA_ROOT_LIMIT + 1)
        with mock.patch.object(core.subprocess, "Popen") as spawn:
            with self.assertRaisesRegex(EngineError, "shorter writable --data-root"):
                Engine(self.config(root))
        spawn.assert_not_called()
        self.assertEqual([], list(self.root.iterdir()))

    @unittest.skipUnless(os.name == "nt", "Windows path budget")
    def test_very_long_data_root_reports_actionable_error_before_filesystem_read(self):
        root = sized_path(self.root, 300)
        with self.assertRaisesRegex(EngineError, "install folder may stay"):
            Engine.from_data_root(str(root))
        self.assertEqual([], list(self.root.iterdir()))

    @unittest.skipUnless(os.name == "nt", "Windows path budget")
    def test_budget_counts_utf16_units_instead_of_unicode_characters(self):
        root = sized_path(self.root, 119)
        root = root.with_name(root.name + "\U0001f680")
        self.assertEqual(120, len(str(root)))
        self.assertEqual(121, core.windows_path_units(root))
        with self.assertRaisesRegex(EngineError, "120 UTF-16"):
            Engine(self.config(root))
        self.assertEqual([], list(self.root.iterdir()))

    @unittest.skipUnless(os.name == "nt", "Windows path budget")
    def test_exact_data_root_budget_can_write_configuration_and_queued_job(self):
        root = sized_path(self.root, core.WINDOWS_DATA_ROOT_LIMIT)
        engine = Engine(self.config(root))
        with mock.patch.object(engine, "_spawn") as spawn:
            state = engine.start_job("Synthetic offline path fixture")
        spawn.assert_called_once_with(state["job_id"])
        self.assertEqual("queued", state["status"])
        job = root / "jobs" / state["job_id"]
        # The worker's longest fixed filename must also remain within budget.
        spec = job / ("command." + "0" * 32 + ".private.json")
        core.atomic_json(spec, {"command": [], "cwd": str(job / "project")})
        self.assertLessEqual(core.windows_path_units(spec), core.WINDOWS_FILE_PATH_LIMIT)
        self.assertTrue((job / "empty-mcp.json").is_file())
        self.assertEqual(state["job_id"], core.read_json(job / "state.json")["job_id"])

    @unittest.skipUnless(os.name == "nt", "Windows path budget")
    def test_compact_atomic_temp_works_at_supported_maximum_path(self):
        parent = sized_path(self.root, core.WINDOWS_DIRECTORY_PATH_LIMIT)
        parent.mkdir(parents=True)
        target = parent / "status.json"
        self.assertEqual(259, core.windows_path_units(target))
        self.assertGreater(core.windows_path_units(target.with_name(target.name + "." + "0" * 32 + ".tmp")), 259)
        target.write_text('{"old":true}', encoding="utf-8")
        replace = os.replace
        observations = []

        def observe(source, destination):
            self.assertEqual(target.parent, source.parent)
            self.assertEqual(11, len(source.name))
            self.assertLessEqual(core.windows_path_units(source), 259)
            self.assertEqual({"old": True}, core.read_json(target))
            observations.append(True)
            return replace(source, destination)

        with mock.patch.object(core.os, "replace", side_effect=observe):
            core.atomic_json(target, {"new": True})
        self.assertEqual([True], observations)
        self.assertEqual({"new": True}, core.read_json(target))
        self.assertEqual(["status.json"], [item.name for item in parent.iterdir()])

    @unittest.skipUnless(os.name == "nt", "Windows path budget")
    def test_overlong_file_and_directory_paths_are_refused(self):
        with self.assertRaisesRegex(EngineError, "path budget"):
            core.checked_path(sized_path(self.root, 260))
        with self.assertRaisesRegex(EngineError, "path budget"):
            core.checked_path(sized_path(self.root, 248), directory=True)
        self.assertEqual([], list(self.root.iterdir()))

    @unittest.skipUnless(os.name == "nt", "Windows namespace restrictions")
    def test_device_namespaces_cannot_bypass_path_checks(self):
        for prefix in ("\\\\?\\", "\\\\.\\"):
            with self.subTest(prefix=prefix):
                with self.assertRaisesRegex(EngineError, "extended-length paths"):
                    core.checked_path(prefix + str(self.root))

    def test_relative_and_traversing_paths_remain_rejected(self):
        for path in (Path("relative"), self.root / ".." / "escape"):
            with self.subTest(path=str(path)):
                with self.assertRaisesRegex(EngineError, "non-traversing"):
                    core.checked_path(path)

    @unittest.skipUnless(os.name == "nt", "Windows deep installation regression")
    def test_deep_install_process_can_use_explicit_short_data_root(self):
        # Copy only source into a >120-unit install directory. A foreground
        # Python fixture imports that copy and queues with _spawn mocked, so it
        # cannot start any detached worker, model, or renderer.
        install = sized_path(self.root / "install", 180)
        engine_dir = install / "engine"
        engine_dir.mkdir(parents=True)
        for name in ("__init__.py", "core.py"):
            shutil.copyfile(PACKAGE / "engine" / name, engine_dir / name)
        data = self.root / "data"
        script = """import json, pathlib, sys
from unittest import mock
import engine.core as core
from engine import Config, Engine
assert pathlib.Path(core.__file__).resolve().is_relative_to(pathlib.Path.cwd())
instance = Engine(Config(data_root=sys.argv[1], test_mode=True,
                        min_free_disk_bytes=0, min_free_memory_bytes=0))
with mock.patch.object(instance, '_spawn') as spawn:
    state = instance.start_job('Synthetic offline installed-package fixture')
    assert spawn.call_count == 1
assert state['status'] == 'queued'
print(json.dumps({'root': str(instance.root), 'status': state['status']}))
"""
        result = subprocess.run([sys.executable, "-B", "-c", script, str(data)],
                                cwd=install, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=15,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual({"root": str(data.resolve()), "status": "queued"}, json.loads(result.stdout))


if __name__ == "__main__":
    unittest.main()
