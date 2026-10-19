"""Adversarial engine tests. Only offline fixture processes are executed.

Run from the plugin root: python -m unittest discover -s tests -v
"""

from dataclasses import replace
import ctypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import Config, Engine, EngineError


FIXTURES = Path(__file__).parent / "fixtures"
TERMINAL = {"authored", "completed", "failed", "cancelled"}
PROMPT = "SYNTHETIC-PRIVATE-PROMPT-c6e258b4; $(never-run) `never-run` & never-run"
AUTH = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "max"}
# Keep generated Windows job paths short even when the source ZIP is extracted
# deeply. mkdir inherits the user's existing ACLs; each test owns a UUID child.
TEST_TEMP = Path.home() / "AppData" / "Local" / "Temp" if os.name == "nt" else Path(tempfile.gettempdir())
TEST_WORKSPACE = TEST_TEMP / "sr-tests"


def is_alive(pid):
    if not pid:
        return False
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
        kernel.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
        handle = kernel.OpenProcess(0x100000, False, pid)
        if not handle:
            return False
        try:
            return kernel.WaitForSingleObject(handle, 0) == 258
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


class EngineTests(unittest.TestCase):
    def setUp(self):
        # Python 3.13's mode0700 mkdtemp ACL excludes restricted Windows tokens.
        # Use inherited ACLs in our explicit workspace instead.
        self.root = TEST_WORKSPACE / str(uuid.uuid4())
        self.root.mkdir(parents=True)
        self.engines = []
        self.jobs = []
        self.owned_processes = []
        self.child_handles = []

    def tearDown(self):
        for engine, job_id in self.jobs:
            try:
                engine.cancel(job_id)
            except (EngineError, OSError):
                pass
        for process in self.owned_processes:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=4)
        # Keep native handles, so a cleanup cannot accidentally target a reused PID.
        for handle in self.child_handles:
            if os.name == "nt":
                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel.TerminateProcess.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
                kernel.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
                kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
                if kernel.WaitForSingleObject(handle, 0) == 258:
                    kernel.TerminateProcess(handle, 1)
                    kernel.WaitForSingleObject(handle, 4000)
                kernel.CloseHandle(handle)
        # Give only owned workers time to flush their final state before cleanup.
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            active = False
            for engine, job_id in self.jobs:
                try:
                    active |= is_alive(engine.status(job_id).get("worker_pid"))
                except (EngineError, OSError):
                    pass
            if not active:
                break
            time.sleep(0.05)
        resolved_root = self.root.resolve()
        if not resolved_root.is_relative_to(TEST_WORKSPACE.resolve()):
            raise RuntimeError("Refusing cleanup outside test workspace")
        shutil.rmtree(resolved_root)

    def config(self, mode="success", **overrides):
        values = dict(data_root=str(self.root / "data"), claude_path=sys.executable,
                      test_mode=True,
                      test_author_command=(sys.executable, str(FIXTURES / "author.py"), mode),
                      test_auth_status=dict(AUTH), test_timeout_seconds=4,
                      min_free_disk_bytes=0, min_free_memory_bytes=0,
                      ffprobe_path=sys.executable)
        values.update(overrides)
        return Config(**values)

    def engine(self, mode="success", **overrides):
        engine = Engine(self.config(mode, **overrides))
        self.engines.append(engine)
        return engine

    def start(self, engine, prompt=PROMPT, **kwargs):
        state = engine.start_job(prompt, **kwargs)
        job_id = state["job_id"] if isinstance(state, dict) else state
        self.jobs.append((engine, job_id))
        return job_id

    def wait(self, engine, job_id, wanted=TERMINAL, timeout=12):
        deadline = time.monotonic() + timeout
        state = None
        while time.monotonic() < deadline:
            state = engine.status(job_id)
            if state["status"] in wanted:
                return state
            time.sleep(0.05)
        self.fail(f"job did not reach {wanted}: {state}")

    def assert_private(self, state):
        encoded = json.dumps(state)
        self.assertNotIn(PROMPT, encoded)
        self.assertNotIn("DO-NOT-EXPOSE", encoded)
        self.assertNotIn("OVERSIZE-PRIVATE", encoded)
        self.assertLessEqual(len(state.get("events", [])), 100)

    def synthetic_revision_source(self, engine, *, status="failed", phase="render", overrides=None,
                                  omit_fields=(), project_exists=True):
        job_id = str(uuid.uuid4())
        directory = engine.root / "jobs" / job_id
        directory.mkdir()
        project = directory / "project"
        if project_exists:
            (project / "scenes").mkdir(parents=True)
            (project / "output").mkdir()
            (project / "scenes" / "editable.html").write_text("<h1>Retained authored scene</h1>", encoding="utf-8")
            (project / "output" / "final.mp4").write_bytes(b"preserve existing render fixture")
        state = {"job_id": job_id, "status": status, "phase": phase,
                 "created_at": time.time(), "updated_at": time.time(), "events": [],
                 "author_result": {"subtype": "success", "is_error": False},
                 "author_finished_at": 1.0}
        state.update(overrides or {})
        for field in omit_fields:
            state.pop(field, None)
        # Deliberately permits non-finite JSON in corruption cases, which the
        # revision validator must reject even if such a file appears on disk.
        (directory / "state.json").write_text(json.dumps(state), encoding="utf-8")
        return job_id, project

    def test_revision_accepts_proven_failed_or_cancelled_render_without_outputs(self):
        engine = self.engine()
        for status in ("failed", "cancelled"):
            with self.subTest(status=status):
                source_id, source = self.synthetic_revision_source(engine, status=status)
                with mock.patch.object(engine, "_spawn") as spawn:
                    submitted = engine.start_job(PROMPT, seed_job_ids=[source_id])
                spawn.assert_called_once()
                copied = Path(submitted["project_dir"])
                self.assertEqual("<h1>Retained authored scene</h1>", (copied / "scenes" / "editable.html").read_text(encoding="utf-8"))
                self.assertFalse((copied / "output").exists())
                self.assertEqual(b"preserve existing render fixture", (source / "output" / "final.mp4").read_bytes())
                self.assertEqual([source_id], submitted["revision_of"])

    def test_revision_rejects_missing_or_failed_authoring_proof(self):
        engine = self.engine()
        invalid = (None, {}, {"subtype": "success"}, {"is_error": False},
                   {"subtype": "success", "is_error": True},
                   {"subtype": "error_during_execution", "is_error": False},
                   {"subtype": "success", "is_error": 0}, "success")
        for proof in invalid:
            with self.subTest(proof=proof):
                source_id, _ = self.synthetic_revision_source(engine, overrides={"author_result": proof})
                with mock.patch.object(engine, "_spawn") as spawn:
                    with self.assertRaises(EngineError):
                        engine.start_job(PROMPT, seed_job_ids=[source_id])
                spawn.assert_not_called()
        source_id, _ = self.synthetic_revision_source(engine, omit_fields=("author_result",))
        with mock.patch.object(engine, "_spawn") as spawn:
            with self.assertRaises(EngineError):
                engine.start_job(PROMPT, seed_job_ids=[source_id])
        spawn.assert_not_called()

    def test_revision_rejects_invalid_authoring_timestamp_or_missing_project(self):
        engine = self.engine()
        for timestamp in (None, 0, -1, float("nan"), float("inf"), "1", True):
            with self.subTest(author_finished_at=timestamp):
                source_id, _ = self.synthetic_revision_source(engine, overrides={"author_finished_at": timestamp})
                with mock.patch.object(engine, "_spawn") as spawn:
                    with self.assertRaises(EngineError):
                        engine.start_job(PROMPT, seed_job_ids=[source_id])
                spawn.assert_not_called()
        for options in ({"omit_fields": ("author_finished_at",)}, {"project_exists": False}):
            with self.subTest(source_options=options):
                source_id, _ = self.synthetic_revision_source(engine, **options)
                with mock.patch.object(engine, "_spawn") as spawn:
                    with self.assertRaises(EngineError):
                        engine.start_job(PROMPT, seed_job_ids=[source_id])
                spawn.assert_not_called()

    def test_revision_rejects_failed_or_cancelled_author_with_forged_proof(self):
        engine = self.engine()
        for status in ("failed", "cancelled"):
            with self.subTest(status=status):
                source_id, _ = self.synthetic_revision_source(engine, status=status, phase="author")
                with mock.patch.object(engine, "_spawn") as spawn:
                    with self.assertRaises(EngineError):
                        engine.start_job(PROMPT, seed_job_ids=[source_id])
                spawn.assert_not_called()

    def test_recoverable_render_source_cannot_render_directly_or_overwrite_output(self):
        engine = self.engine(render_command=(sys.executable, str(FIXTURES / "render.py"), "fail", "{project}", "{output}"))
        for status in ("failed", "cancelled"):
            with self.subTest(status=status):
                source_id, project = self.synthetic_revision_source(engine, status=status)
                with mock.patch.object(engine, "_spawn") as spawn:
                    with self.assertRaises(EngineError):
                        engine.render_job(source_id)
                spawn.assert_not_called()
                self.assertEqual(b"preserve existing render fixture", (project / "output" / "final.mp4").read_bytes())

    def test_success_returns_editable_artifacts_and_redacts_content(self):
        engine = self.engine()
        job_id = self.start(engine, require_existing_claude_login=True)
        state = self.wait(engine, job_id)
        self.assertEqual("authored", state["status"], state)
        result = engine.result(job_id)
        self.assertTrue(any(Path(x["path"]).name == "index.html" for x in result["artifacts"]))
        self.assert_private(result)
        project = Path(state["project_dir"])
        self.assertNotIn(PROMPT, (project / "fixture-argv.json").read_text(encoding="utf-8"))
        self.assertFalse((project.parent / "prompt.private.txt").exists())
        self.assert_private(json.loads((project.parent / "state.json").read_text(encoding="utf-8")))

    def test_unstructured_stdout_stderr_are_not_public(self):
        engine = self.engine("raw")
        job_id = self.start(engine)
        state = self.wait(engine, job_id)
        self.assert_private(state)
        self.assert_private(engine.result(job_id))
        for path in Path(state["project_dir"]).parent.glob("*.log"):
            text = path.read_text(encoding="utf-8", errors="replace")
            self.assertNotIn(PROMPT, text)
            self.assertNotIn("DO-NOT-EXPOSE", text)

    def test_nonzero_exit_has_safe_error(self):
        engine = self.engine("fail")
        job_id = self.start(engine)
        state = self.wait(engine, job_id)
        self.assertEqual("failed", state["status"], state)
        self.assertTrue(state.get("error", {}).get("code"))
        self.assert_private(state)

    def test_oversized_output_is_bounded(self):
        engine = self.engine("oversize")
        job_id = self.start(engine)
        state = self.wait(engine, job_id)
        self.assertIn(state["status"], {"authored", "failed"})
        self.assert_private(state)
        for path in Path(state["project_dir"]).parent.glob("*.log"):
            self.assertLessEqual(path.stat().st_size, 1024 * 1024)
            self.assertNotIn(b"OVERSIZE-PRIVATE", path.read_bytes())

    def test_timeout_stops_job(self):
        engine = self.engine("hang", test_timeout_seconds=0.6)
        job_id = self.start(engine)
        state = self.wait(engine, job_id)
        self.assertEqual("failed", state["status"], state)
        self.assertIn("timeout", state.get("error", {}).get("code", ""))
        self.assert_private(state)

    def test_cancel_kills_owned_descendant_preserves_other_process(self):
        sentinel = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], stdin=subprocess.DEVNULL)
        self.owned_processes.append(sentinel)
        engine = self.engine("child", test_timeout_seconds=15)
        job_id = self.start(engine)
        state = self.wait(engine, job_id, {"authoring"})
        child_file = Path(state["project_dir"]) / "fixture-child.pid"
        deadline = time.monotonic() + 5
        while not child_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(child_file.exists(), "fixture did not spawn descendant")
        child_pid = int(child_file.read_text(encoding="ascii"))
        if os.name == "nt":
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.restype = ctypes.c_void_p
            kernel.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
            handle = kernel.OpenProcess(0x100001, False, child_pid)
            if handle:
                self.child_handles.append(handle)
        self.assertTrue(is_alive(child_pid))
        engine.cancel(job_id)
        state = self.wait(engine, job_id, {"cancelled"})
        deadline = time.monotonic() + 5
        while is_alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(is_alive(child_pid), "owned child survived cancellation")
        self.assertIsNone(sentinel.poll(), "cancellation affected unrelated sentinel")
        self.assert_private(state)

    @unittest.skipUnless(os.name == "nt", "Windows Job Object crash containment")
    def test_worker_crash_kills_owned_descendant(self):
        engine = self.engine("child", test_timeout_seconds=15)
        job_id = self.start(engine)
        state = self.wait(engine, job_id, {"authoring"})
        child_file = Path(state["project_dir"]) / "fixture-child.pid"
        deadline = time.monotonic() + 5
        while not child_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(child_file.exists(), "fixture did not spawn descendant")
        child_pid = int(child_file.read_text(encoding="ascii"))
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
        kernel.TerminateProcess.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        worker_handle = None
        for pid in (child_pid, state["worker_pid"]):
            handle = kernel.OpenProcess(0x100001, False, pid)
            self.assertTrue(handle, "cannot open our synthetic process")
            self.child_handles.append(handle)
            worker_handle = handle
        self.assertTrue(kernel.TerminateProcess(worker_handle, 77))
        failed = self.wait(engine, job_id, {"failed"})
        self.assertEqual("worker_exited", failed["error"]["code"])
        deadline = time.monotonic() + 5
        while is_alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(is_alive(child_pid), "owned child survived worker crash")

    def test_invalid_job_ids_rejected(self):
        engine = self.engine()
        for value in ("../outside", "..\\outside", "C:\\Windows", "/tmp/outside", "", "a/b", "a\\b", "x" * 500):
            for operation in (engine.status, engine.result, engine.cancel, engine.render_job):
                with self.subTest(value=value, operation=operation.__name__):
                    with self.assertRaises(EngineError):
                        operation(value)

    def test_invalid_inputs_rejected_before_launch(self):
        engine = self.engine()
        for value in ("", "   ", None, 42, "embedded\x00null", "x" * 65537):
            with self.subTest(prompt=value):
                with self.assertRaises(EngineError):
                    engine.start_job(value)
        for model in ("--dangerous", "claude-opus-5-5; calc", "model\nflag", ""):
            with self.subTest(model=model):
                with self.assertRaises(EngineError):
                    engine.start_job("test", model=model)
        for turns in (0, -1, 10000, True, 1.5):
            with self.subTest(max_turns=turns):
                with self.assertRaises(EngineError):
                    engine.start_job("test", max_turns=turns)
        for timeout in (0, 59, 1801, float("nan"), float("inf"), True):
            with self.subTest(wall_timeout=timeout):
                with self.assertRaises(EngineError):
                    engine.start_job("test", wall_timeout=timeout)

    def test_seed_traversal_rejected(self):
        engine = self.engine()
        with self.assertRaises(EngineError):
            engine.start_job("test", seed_job_ids=["../outside"])

    def test_seed_copy_excludes_generated_directories_case_insensitively(self):
        from engine.core import copy_seed

        source = self.root / "seed-copy-source"
        destination = self.root / "seed-copy-destination"
        generated = (".HyperFrames", "ReNdErS", "Output", "Node_Modules", ".Git", ".Cache", ".Claude")
        for name in generated:
            folder = source / name
            folder.mkdir(parents=True)
            (folder / "generated.txt").write_text("generated fixture", encoding="utf-8")
        (source / "scenes").mkdir()
        (source / "assets" / "ReNdErS").mkdir(parents=True)
        (source / "scenes" / "editable.html").write_text("<h1>Editable scene</h1>", encoding="utf-8")
        (source / "assets" / "logo.svg").write_text("<svg></svg>", encoding="utf-8")
        (source / "assets" / "ReNdErS" / "generated.txt").write_text("nested generated fixture", encoding="utf-8")

        copy_seed(source, destination)

        for name in generated:
            self.assertFalse((destination / name).exists(), f"copied excluded directory {name}")
        self.assertFalse((destination / "assets" / "ReNdErS").exists())
        self.assertEqual("<h1>Editable scene</h1>", (destination / "scenes" / "editable.html").read_text(encoding="utf-8"))
        self.assertEqual("<svg></svg>", (destination / "assets" / "logo.svg").read_text(encoding="utf-8"))

    def test_insufficient_disk_never_spawns_author(self):
        marker = self.root / "author-started"
        engine = self.engine(min_free_disk_bytes=2**63,
                             test_author_command=(sys.executable, str(FIXTURES / "author.py"), "success", "--marker", str(marker)))
        job_id = self.start(engine)
        state = self.wait(engine, job_id)
        self.assertEqual("failed", state["status"], state)
        self.assertEqual("insufficient_disk", state["error"]["code"])
        self.assertFalse(marker.exists())

    def test_insufficient_memory_never_spawns_author(self):
        marker = self.root / "author-started"
        engine = self.engine(min_free_memory_bytes=2**63,
                             test_author_command=(sys.executable, str(FIXTURES / "author.py"), "success", "--marker", str(marker)))
        job_id = self.start(engine)
        state = self.wait(engine, job_id)
        self.assertEqual("failed", state["status"], state)
        self.assertEqual("insufficient_memory", state["error"]["code"])
        self.assertFalse(marker.exists())

    def test_missing_login_never_spawns_author(self):
        marker = self.root / "author-started"
        engine = self.engine(test_auth_status={"loggedIn": False},
                             test_author_command=(sys.executable, str(FIXTURES / "author.py"), "success", "--marker", str(marker)))
        job_id = self.start(engine, require_existing_claude_login=True)
        state = self.wait(engine, job_id)
        self.assertEqual("failed", state["status"], state)
        self.assertTrue(state.get("error", {}).get("code"))
        self.assertFalse(marker.exists())

    def test_changed_effective_auth_route_never_spawns_author(self):
        routes = ({**AUTH, "loggedIn": False}, {**AUTH, "authMethod": "api_key"},
                  {**AUTH, "apiProvider": "bedrock"})
        for index, route in enumerate(routes):
            with self.subTest(route_field=index):
                marker = self.root / f"author-started-{index}"
                engine = self.engine(data_root=str(self.root / f"route-{index}"),
                                     test_effective_auth_status={**route, "email": PROMPT},
                                     test_author_command=(sys.executable, str(FIXTURES / "author.py"), "success", "--marker", str(marker)))
                job_id = self.start(engine)
                state = self.wait(engine, job_id)
                self.assertEqual("failed", state["status"], state)
                self.assertEqual("authentication_route_changed", state["error"]["code"], state)
                self.assertFalse(marker.exists())
                self.assert_private(state)

    def test_same_api_auth_route_is_allowed_unless_personal_login_required(self):
        api_auth = {"loggedIn": True, "authMethod": "api_key", "apiProvider": "firstParty", "email": PROMPT}
        for restricted in (False, True):
            with self.subTest(require_existing_claude_login=restricted):
                marker = self.root / f"api-author-started-{restricted}"
                engine = self.engine(data_root=str(self.root / f"api-{restricted}"),
                                     test_auth_status=api_auth, test_effective_auth_status=api_auth,
                                     test_author_command=(sys.executable, str(FIXTURES / "author.py"), "success", "--marker", str(marker)))
                job_id = self.start(engine, require_existing_claude_login=restricted)
                state = self.wait(engine, job_id)
                if restricted:
                    self.assertEqual("failed", state["status"], state)
                    self.assertEqual("authentication_policy_mismatch", state["error"]["code"], state)
                    self.assertFalse(marker.exists())
                else:
                    self.assertEqual("authored", state["status"], state)
                    self.assertTrue(marker.exists())
                self.assert_private(state)

    def test_unrecognized_auth_routes_are_compared_before_redaction(self):
        original = {"loggedIn": True, "authMethod": "future-method-" + PROMPT,
                    "apiProvider": "future-provider-" + PROMPT}
        for index, field in enumerate(("authMethod", "apiProvider")):
            with self.subTest(changed_field=field):
                marker = self.root / f"future-route-started-{index}"
                effective = {**original, field: "different-future-route-" + PROMPT}
                engine = self.engine(data_root=str(self.root / f"future-route-{index}"),
                                     test_auth_status=original, test_effective_auth_status=effective,
                                     test_author_command=(sys.executable, str(FIXTURES / "author.py"), "success", "--marker", str(marker)))
                job_id = self.start(engine)
                state = self.wait(engine, job_id)
                self.assertEqual("failed", state["status"], state)
                self.assertEqual("authentication_route_changed", state["error"]["code"])
                self.assertFalse(marker.exists())
                self.assert_private(state)

    def test_effective_auth_injection_alone_is_rejected_in_production(self):
        config = Config(data_root=str(self.root / "production"), claude_path=sys.executable,
                        test_effective_auth_status=dict(AUTH))
        with self.assertRaises(EngineError):
            Engine(config)

    def test_provider_failures_are_classified_without_private_text(self):
        expected = {"quota": "quota_or_rate_limit", "billing": "billing_limit",
                    "access": "model_access_denied", "model": "model_unavailable"}
        for mode, code in expected.items():
            with self.subTest(mode=mode):
                engine = self.engine(mode, data_root=str(self.root / mode))
                job_id = self.start(engine)
                state = self.wait(engine, job_id)
                self.assertEqual("failed", state["status"], state)
                self.assertEqual(code, state["error"]["code"], state)
                self.assert_private(state)

    def test_failed_renderer_cannot_mark_completed(self):
        engine = self.engine(render_command=(sys.executable, str(FIXTURES / "render.py"), "fail", "{project}", "{output}"))
        job_id = self.start(engine)
        self.assertEqual("authored", self.wait(engine, job_id)["status"])
        engine.render_job(job_id)
        state = self.wait(engine, job_id, {"failed", "completed"})
        self.assertEqual("failed", state["status"], state)
        self.assert_private(state)

    def test_empty_render_cannot_mark_completed(self):
        engine = self.engine(render_command=(sys.executable, str(FIXTURES / "render.py"), "empty", "{project}", "{output}"))
        job_id = self.start(engine)
        self.wait(engine, job_id)
        engine.render_job(job_id)
        state = self.wait(engine, job_id, {"failed", "completed"})
        self.assertEqual("failed", state["status"], state)

    def test_test_injections_rejected_in_production(self):
        with self.assertRaises(EngineError):
            Engine(replace(self.config(), test_mode=False))

    def test_author_jobs_are_serialized(self):
        trace = self.root / "fixture-trace.txt"
        engine = self.engine(test_timeout_seconds=6,
                             test_author_command=(sys.executable, str(FIXTURES / "author.py"), "slow", "--trace", str(trace)))
        first = self.start(engine)
        second = self.start(engine)
        self.assertEqual("authored", self.wait(engine, first)["status"])
        self.assertEqual("authored", self.wait(engine, second)["status"])
        events = [line.split() for line in trace.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(["start", "end", "start", "end"], [entry[0] for entry in events])
        self.assertEqual(events[0][2], events[1][2])
        self.assertEqual(events[2][2], events[3][2])
        self.assertLessEqual(float(events[1][1]), float(events[2][1]))

    def test_render_jobs_are_serialized(self):
        trace = self.root / "render-trace.txt"
        engine = self.engine(test_timeout_seconds=6,
                             render_command=(sys.executable, str(FIXTURES / "render.py"), "slowfail", "{project}", "{output}", "--trace", str(trace)))
        first = self.start(engine)
        second = self.start(engine)
        self.assertEqual("authored", self.wait(engine, first)["status"])
        self.assertEqual("authored", self.wait(engine, second)["status"])
        engine.render_job(first)
        engine.render_job(second)
        self.assertEqual("failed", self.wait(engine, first, {"failed", "completed"})["status"])
        self.assertEqual("failed", self.wait(engine, second, {"failed", "completed"})["status"])
        events = [line.split() for line in trace.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(["start", "end", "start", "end"], [entry[0] for entry in events])
        self.assertLessEqual(float(events[1][1]), float(events[2][1]))

    def test_cancelled_queued_job_never_starts(self):
        engine = self.engine("hang", test_timeout_seconds=15)
        first = self.start(engine)
        self.wait(engine, first, {"authoring"})
        second = self.start(engine)
        engine.cancel(second)
        second_state = self.wait(engine, second, {"cancelled"})
        self.assertFalse((Path(second_state["project_dir"]) / "fixture-argv.json").exists())
        engine.cancel(first)
        self.wait(engine, first, {"cancelled"})

    def test_stale_worker_state_becomes_safe_failure(self):
        engine = self.engine()
        job_id = self.start(engine)
        state = self.wait(engine, job_id)
        deadline = time.monotonic() + 4
        while is_alive(state.get("worker_pid")) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(is_alive(state.get("worker_pid")))
        state_path = Path(state["project_dir"]).parent / "state.json"
        private_state = json.loads(state_path.read_text(encoding="utf-8"))
        private_state.update(status="authoring", phase="author")
        state_path.write_text(json.dumps(private_state), encoding="utf-8")
        recovered = engine.status(job_id)
        self.assertEqual("failed", recovered["status"])
        self.assertEqual("worker_exited", recovered["error"]["code"])
        self.assert_private(recovered)

    def test_stale_first_status_read_cannot_overwrite_new_live_worker(self):
        from engine import core

        engine = self.engine()
        with mock.patch.object(engine, "_spawn"):
            submitted = engine.start_job(PROMPT)
        job_id = submitted["job_id"]
        state_path = engine.root / "jobs" / job_id / "state.json"
        live = core.read_json(state_path)
        live.update(status="authoring", worker_pid=os.getpid(),
                    worker_identity=core.process_identity(os.getpid()), updated_at=time.time())
        self.assertIsNotNone(live["worker_identity"])
        core.atomic_json(state_path, live)
        stale = {**live, "worker_identity": "stale-startup-identity"}
        real_read = core.read_json
        reads = 0

        def stale_once(path):
            nonlocal reads
            if Path(path) == state_path:
                reads += 1
                if reads == 1:
                    return dict(stale)
            return real_read(path)

        with mock.patch("engine.core.read_json", side_effect=stale_once):
            observed = engine.status(job_id)
        self.assertGreaterEqual(reads, 2, "liveness must be checked again after acquiring operation lock")
        self.assertEqual("authoring", observed["status"], observed)
        self.assertEqual("authoring", real_read(state_path)["status"])
        self.assertNotIn("error", observed)

    def test_uncertain_process_queries_preserve_active_state_and_private_prompt(self):
        from engine import core

        cases = (
            ("denied_observation", "known", [core.UNKNOWN_PROCESS], False),
            ("unknown_saved_identity", core.UNKNOWN_PROCESS, ["live"], False),
            ("unset_saved_identity", None, ["live"], False),
            ("matching_identity", "live", ["live"], False),
            ("denied_after_lock", "known", [None, core.UNKNOWN_PROCESS], False),
            ("missing_process", "known", [None, None], True),
            ("missing_with_unknown_saved_identity", core.UNKNOWN_PROCESS, [None, None], True),
            ("known_mismatch", "known", ["different", "different"], True),
        )
        for name, saved_identity, observations, dead in cases:
            with self.subTest(case=name):
                engine = self.engine(data_root=str(self.root / name))
                with mock.patch.object(engine, "_spawn"):
                    submitted = engine.start_job(PROMPT)
                job_id = submitted["job_id"]
                directory = engine.root / "jobs" / job_id
                state_path = directory / "state.json"
                private = core.read_json(state_path)
                private.update(status="authoring", worker_pid=os.getpid(),
                               worker_identity=saved_identity, updated_at=time.time())
                core.atomic_json(state_path, private)
                with mock.patch("engine.core.process_identity", side_effect=observations):
                    observed = engine.status(job_id)
                expected_status = "failed" if dead else "authoring"
                self.assertEqual(expected_status, observed["status"], observed)
                self.assertEqual(expected_status, core.read_json(state_path)["status"])
                self.assertEqual(not dead, (directory / "prompt.private.txt").exists())
                if dead:
                    self.assertEqual("worker_exited", observed["error"]["code"])
                else:
                    self.assertNotIn("error", observed)
                    self.assertEqual(PROMPT, (directory / "prompt.private.txt").read_text(encoding="utf-8"))
                self.assert_private(observed)

    @unittest.skipUnless(os.name == "nt", "Windows process query error distinctions")
    def test_windows_access_denied_process_query_is_not_reported_missing(self):
        from engine import core

        for error, expected in ((5, core.UNKNOWN_PROCESS), (0, core.UNKNOWN_PROCESS),
                                (299, core.UNKNOWN_PROCESS), (87, None), (1168, None)):
            with self.subTest(winerror=error):
                kernel = mock.MagicMock()
                kernel.OpenProcess.return_value = 0
                with mock.patch("engine.core.ctypes.WinDLL", return_value=kernel), \
                     mock.patch("engine.core.ctypes.get_last_error", return_value=error):
                    self.assertEqual(expected, core.process_identity(os.getpid()))
                kernel.CloseHandle.assert_not_called()

    def test_recovery_and_cancel_work_after_executable_or_seed_removed(self):
        placeholder = self.root / "placeholder-claude.exe"
        placeholder.write_bytes(b"offline test placeholder")
        seed = self.root / "seed"
        seed.mkdir()
        engine = self.engine("hang", claude_path=str(placeholder),
                             seed_directory=str(seed), test_timeout_seconds=15)
        job_id = self.start(engine)
        self.wait(engine, job_id, {"authoring"})
        placeholder.unlink()
        seed.rmdir()
        recovered = Engine.from_data_root(str(engine.root))
        self.assertEqual("authoring", recovered.status(job_id)["status"])
        recovered.cancel(job_id)
        self.assertEqual("cancelled", self.wait(recovered, job_id, {"cancelled"})["status"])

    def test_worker_survives_client_process_exit(self):
        engine = self.engine("slow", test_timeout_seconds=6)
        prompt_file = self.root / "producer-prompt.private.txt"
        prompt_file.write_text(PROMPT, encoding="utf-8")
        try:
            completed = subprocess.run([sys.executable, "-m", "engine.cli", "--data-root", str(engine.root),
                                        "start", "--prompt-file", str(prompt_file)],
                                       stdin=subprocess.DEVNULL, text=True, capture_output=True,
                                       cwd=Path(__file__).resolve().parents[1], timeout=10)
        finally:
            prompt_file.unlink()
        self.assertEqual(0, completed.returncode, "synthetic producer failed")
        submission = json.loads(completed.stdout)
        self.assert_private(submission)
        job_id = submission["job_id"]
        self.jobs.append((engine, job_id))
        state = self.wait(engine, job_id)
        self.assertEqual("authored", state["status"], state)
        self.assert_private(state)

    def test_artifact_link_escape_is_rejected(self):
        engine = self.engine(render_command=(sys.executable, str(FIXTURES / "render.py"), "fail", "{project}", "{output}"))
        job_id = self.start(engine)
        state = self.wait(engine, job_id)
        outside = self.root / "owned-outside"
        outside.mkdir()
        (outside / "private.txt").write_text("owned test secret", encoding="utf-8")
        link = Path(state["project_dir"]) / "escape"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"OS does not permit fixture symlink: {error.winerror if os.name == 'nt' else error.errno}")
        try:
            with self.assertRaises(EngineError):
                engine.result(job_id)
            with self.assertRaises(EngineError):
                engine.render_job(job_id)
        finally:
            link.unlink()


class CollectorTests(unittest.TestCase):
    def test_structured_author_errors_keep_only_safe_finite_codes(self):
        from engine.worker import Collector

        cases = (
            ({"type": "api_error", "message": "Connection error. " + PROMPT}, "network_error"),
            ({"type": "authentication_error", "message": PROMPT}, "authentication_failed"),
            ({"type": "model_not_found", "message": PROMPT}, "model_unavailable"),
            ({"type": "api_error", "message": "You do not have access to this model. " + PROMPT}, "model_access_denied"),
            ({"status": 429, "message": PROMPT}, "quota_or_rate_limit"),
            ({"type": "billing_error", "message": "Insufficient credits. " + PROMPT}, "billing_limit"),
            ({"type": "permission_error", "message": "Permission denied. " + PROMPT}, "permission_denied"),
            ({"status_code": 503, "message": PROMPT}, "service_unavailable"),
        )
        for payload, expected in cases:
            for kind in ("assistant", "error"):
                with self.subTest(code=expected, kind=kind):
                    collector = Collector()
                    collector.consume(json.dumps({"type": kind, "error": payload, "private": PROMPT}).encode(), "stdout")
                    metadata, result, code = collector.snapshot()
                    self.assertEqual(expected, code)
                    self.assertIn({"type": "author_error", "code": expected}, metadata["events"])
                    self.assertNotIn(PROMPT, json.dumps((metadata, result, code)))
                    self.assertTrue(all(set(event) <= {"type", "code"} for event in metadata["events"]))

    def test_error_content_is_classified_only_with_error_marker(self):
        from engine.worker import Collector

        content = [{"type": "text", "text": "Connection error. " + PROMPT}]
        for marker, expected in ((False, None), (True, "network_error")):
            with self.subTest(has_error_marker=marker):
                collector = Collector()
                event = {"type": "assistant", "message": {"content": content}}
                if marker:
                    event["error"] = "api_error"
                collector.consume(json.dumps(event).encode(), "stdout")
                self.assertEqual(expected, collector.snapshot()[2])
                self.assertNotIn(PROMPT, json.dumps(collector.snapshot()))
        collector = Collector()
        collector.consume(json.dumps({"type": "assistant", "message": {
            "error": {"type": "authentication_error", "message": PROMPT}}}).encode(), "stdout")
        self.assertEqual("authentication_failed", collector.snapshot()[2])
        self.assertNotIn(PROMPT, json.dumps(collector.snapshot()))

    def test_success_text_and_unknown_error_do_not_invent_error_codes(self):
        from engine.worker import Collector

        collector = Collector()
        collector.consume(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                      "result": "The scene demonstrates a connection error. " + PROMPT}).encode(), "stdout")
        self.assertIsNone(collector.snapshot()[2])
        collector.consume(json.dumps({"type": "error", "error": {"type": "unrecognized_error", "message": PROMPT}}).encode(), "stdout")
        self.assertIsNone(collector.snapshot()[2])
        self.assertNotIn(PROMPT, json.dumps(collector.snapshot()))

    def test_stream_reader_emits_flushed_small_event_before_process_exit(self):
        import threading
        from engine.worker import Collector, stream_reader

        collector = Collector()
        child = subprocess.Popen(
            [sys.executable, "-c",
             "import time; print('{\"type\":\"system\",\"subtype\":\"init\",\"model\":\"claude-opus-5-5\"}', flush=True); time.sleep(2)"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        reader = threading.Thread(target=stream_reader, args=(child.stdout, collector, "stdout"), daemon=True)
        reader.start()
        try:
            deadline = time.monotonic() + 1
            events = []
            while time.monotonic() < deadline:
                events = collector.snapshot()[0]["events"]
                if events:
                    break
                time.sleep(0.01)
            self.assertEqual([{"type": "initialized", "model": "claude-opus-5-5"}], events,
                             "flushed small event was buffered until EOF or a full read")
            self.assertIsNone(child.poll(), "event was not observed while producer remained alive")
        finally:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=3)
            reader.join(timeout=2)

    def test_malformed_author_metadata_cannot_stop_collector(self):
        from engine.worker import Collector

        collector = Collector()
        for subtype in ([], {}, 1, None):
            collector.consume(json.dumps({"type": "result", "subtype": subtype, "result": PROMPT}).encode(), "stdout")
            self.assertEqual("unknown", collector.snapshot()[1]["subtype"])
        for name in ([], {}, 1, None):
            collector.consume(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": name, "input": PROMPT}]}}).encode(), "stdout")
        collector.consume(b'{"type":"system","subtype":"init","model":"claude-opus-5-5"}', "stdout")
        self.assertEqual("initialized", collector.snapshot()[0]["events"][-1]["type"])
        self.assertNotIn(PROMPT, json.dumps(collector.snapshot()))

    def test_render_progress_allowlists_fields_values_and_stages(self):
        from engine.worker import Collector

        collector = Collector(allow_render=True)
        stages = ("check", "render", "mux", "mux_audio", "probe", "probe_sound",
                  "probe_final", "decode_verify", "complete", "failed")
        for stage in stages:
            collector.consume(json.dumps({"stage": stage, "state": "succeeded", "text": PROMPT,
                                          "command": PROMPT, "stderr": PROMPT}).encode(), "stdout")
        events = collector.snapshot()[0]["events"]
        self.assertEqual(list(stages), [event["stage"] for event in events])
        self.assertTrue(all(event == {"type": "render_progress", "stage": stage, "state": "succeeded"}
                            for event, stage in zip(events, stages)))
        for unknown in ("PRIVATE-UNKNOWN-" + PROMPT, [], {}, 1, None):
            collector.consume(json.dumps({"stage": unknown, "text": PROMPT}).encode(), "stdout")
        self.assertEqual(len(stages), len(collector.snapshot()[0]["events"]))
        collector.consume(json.dumps({"stage": "failed", "text": PROMPT}).encode(), "stdout")
        self.assertEqual("failed", collector.snapshot()[0]["events"][-1]["state"])
        for invalid_state in (PROMPT, [], {}, 1, None):
            collector.consume(json.dumps({"stage": "render", "state": invalid_state, "text": PROMPT}).encode(), "stdout")
            self.assertEqual("running", collector.snapshot()[0]["events"][-1]["state"])
        self.assertNotIn(PROMPT, json.dumps(collector.snapshot()))
        author_collector = Collector()
        author_collector.consume(b'{"stage":"render","state":"running"}', "stdout")
        self.assertEqual([], author_collector.snapshot()[0]["events"])
        for _ in range(120):
            collector.consume(b'{"stage":"render","state":"running"}', "stdout")
        self.assertEqual(100, len(collector.snapshot()[0]["events"]))


if __name__ == "__main__":
    unittest.main()
