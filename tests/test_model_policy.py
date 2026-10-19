"""Offline model-policy tests; no CLI authentication, models, or renders."""

import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import Config, Engine
from engine.core import read_json
from engine.worker import Collector, execute, stream_reader


MODEL = "claude-opus-5-5"
OTHER = "claude-other-model"
PRIVATE = "PRIVATE-PAYLOAD-DO-NOT-RETAIN"
AUTH = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty"}


def init(model=MODEL):
    return {"type": "system", "subtype": "init", "model": model}


def assistant(model=MODEL):
    return {"type": "assistant", "message": {"model": model,
            "content": [{"type": "text", "text": PRIVATE}]}}


def success(model=MODEL):
    return {"type": "result", "subtype": "success", "is_error": False,
            "modelUsage": {model: {"inputTokens": 1, "outputTokens": 1, "private": PRIVATE}},
            "result": PRIVATE}


def collect(events, model=MODEL):
    collector = Collector(requested_model=model)
    for event in events:
        collector.consume(json.dumps(event).encode("utf-8"), "stdout")
    return collector


class ModelEvidenceTests(unittest.TestCase):
    def test_matching_init_and_usage_verify_without_optional_assistant_model(self):
        collector = collect([init(), {"type": "assistant", "message": {"content": []}}, success()])
        self.assertIsNone(collector.model_error())
        self.assertNotIn(PRIVATE, json.dumps(collector.snapshot()))

    def test_missing_init_or_final_usage_is_unknown_even_with_assistant_model(self):
        no_usage = success()
        no_usage.pop("modelUsage")
        empty_usage = success()
        empty_usage["modelUsage"] = {}
        for events in ([success()], [assistant(), success()], [init(), no_usage],
                       [init(), assistant(), no_usage], [init(), empty_usage], [init()]):
            with self.subTest(events=events):
                self.assertEqual("model_observation_missing", collect(events).model_error())

    def test_every_reported_source_must_match_exactly(self):
        for events in ([init(OTHER), assistant(), success()],
                       [init(), assistant(OTHER), success()],
                       [init(), assistant(), success(OTHER)],
                       [init(OTHER), init(), success()],
                       [init(), success(), assistant(OTHER)]):
            with self.subTest(events=events):
                collector = collect(events)
                self.assertEqual("model_mismatch", collector.model_error())
                self.assertNotIn(OTHER, json.dumps(collector.snapshot()))
                self.assertNotIn(PRIVATE, json.dumps(collector.snapshot()))

    def test_aggregate_with_requested_and_additional_model_fails(self):
        result = success()
        result["modelUsage"][OTHER] = {"inputTokens": 0, "outputTokens": 0}
        self.assertEqual("model_mismatch", collect([init(), result]).model_error())

    def test_aliases_case_and_provider_identifiers_are_not_normalized(self):
        for requested, reported in (("opus", MODEL), (MODEL, MODEL.upper()),
                                    ("anthropic/" + MODEL, MODEL)):
            with self.subTest(requested=requested):
                self.assertEqual("model_mismatch", collect([init(reported), success(reported)], requested).model_error())
        for exact in ("opus", "us.anthropic.claude-opus-5-5-v1:0", "publisher/model[1]"):
            with self.subTest(exact=exact):
                self.assertIsNone(collect([init(exact), assistant(exact), success(exact)], exact).model_error())

    def test_matching_evidence_survives_progress_ring_eviction(self):
        collector = collect([init(), assistant()])
        for _ in range(400):
            collector.consume(b'{"type":"assistant","message":{"content":[]}}', "stdout")
        collector.consume(json.dumps(success()).encode(), "stdout")
        metadata = collector.snapshot()[0]
        self.assertEqual(100, len(metadata["events"]))
        self.assertFalse(any(e["type"] == "initialized" for e in metadata["events"]))
        self.assertIsNone(collector.model_error())

    def test_conflict_survives_progress_ring_eviction_and_later_matching_evidence(self):
        collector = collect([init(OTHER), assistant(OTHER)])
        for _ in range(400):
            collector.consume(json.dumps(assistant()).encode(), "stdout")
        collector.consume(json.dumps(init()).encode(), "stdout")
        collector.consume(json.dumps(success()).encode(), "stdout")
        self.assertEqual("model_mismatch", collector.model_error())
        self.assertNotIn(OTHER, json.dumps(collector.snapshot()))

    def test_malformed_model_and_usage_evidence_fail_without_payload_leak(self):
        for bad in (None, 12, [], {}, "contains spaces " + PRIVATE, "x" * 201):
            for source in ("init", "assistant", "usage"):
                with self.subTest(source=source, bad_type=type(bad).__name__):
                    events = [init(), assistant(), success()]
                    if source == "init":
                        events[0]["model"] = bad
                    elif source == "assistant":
                        events[1]["message"]["model"] = bad
                    else:
                        events[-1]["modelUsage"] = bad
                    collector = collect(events)
                    self.assertIn(collector.model_error(), {"model_observation_invalid", "model_observation_missing"})
                    self.assertNotIn(PRIVATE, json.dumps(collector.snapshot()))

    def test_usage_entry_invalid_value_and_excessive_keys_are_bounded(self):
        for usage in ({MODEL: PRIVATE}, {"model" + str(i): {} for i in range(17)}):
            result = success()
            result["modelUsage"] = usage
            collector = collect([init(), result])
            self.assertEqual("model_observation_invalid", collector.model_error())
            self.assertLess(len(json.dumps(collector.snapshot())), 2000)
            self.assertNotIn(PRIVATE, json.dumps(collector.snapshot()))

    def test_multiple_terminal_results_cannot_overwrite_prior_evidence(self):
        self.assertEqual("model_observation_invalid", collect([init(), success(), success()]).model_error())

    def test_unparseable_or_oversized_stdout_prevents_verification(self):
        for gap in (b'{"type":"assistant","message":broken}\n',
                    b'{"type":"assistant","message":{"model":"' + OTHER.encode() + b'","content":"' + b'x' * 70000 + b'"}}\n'):
            collector = Collector(requested_model=MODEL)
            stream_reader(io.BytesIO(json.dumps(init()).encode() + b'\n' + gap + json.dumps(success()).encode() + b'\n'), collector, "stdout")
            self.assertEqual("model_observation_incomplete", collector.model_error())

    def test_reader_failure_prevents_verification_after_matching_result(self):
        collector = collect([init(), success()])
        broken = mock.Mock()
        broken.read1.side_effect = OSError("private diagnostic " + PRIVATE)
        stream_reader(broken, collector, "stdout")
        self.assertEqual("model_observation_incomplete", collector.model_error())
        self.assertNotIn(PRIVATE, json.dumps(collector.snapshot()))


class WorkerModelPolicyTests(unittest.TestCase):
    def setUp(self):
        # Use the writable user Temp directory with inherited ACLs. A Windows
        # sandbox may redirect tempfile to a deeper container with other ACLs.
        temp = Path.home() / "AppData/Local/Temp" if os.name == "nt" else Path(tempfile.gettempdir())
        self.base = temp / "srmp"
        self.root = self.base / uuid.uuid4().hex[:12]
        self.root.mkdir(parents=True)

    def tearDown(self):
        if self.root.resolve().parent != self.base.resolve():
            raise RuntimeError("Unexpected test cleanup path")
        shutil.rmtree(self.root)

    def run_author(self, events, *, returncode=0, effective_auth=None):
        # Only this tiny synthetic producer is launched. Auth is injected and
        # the detached worker launch is suppressed; execute still exercises its
        # real stream collection, final state, and private-file cleanup path.
        payload = "\n".join(json.dumps(event) for event in events) + "\n"
        producer = "import sys; sys.stdin.read(); sys.stdout.write(" + repr(payload) + "); sys.stdout.flush(); raise SystemExit(" + str(returncode) + ")"
        config = Config(data_root=str(self.root / uuid.uuid4().hex[:8]), claude_path=sys.executable,
                        test_mode=True, test_author_command=(sys.executable, "-c", producer),
                        test_auth_status=AUTH, test_effective_auth_status=effective_auth,
                        min_free_disk_bytes=0, min_free_memory_bytes=0, test_timeout_seconds=5)
        engine = Engine(config)
        with mock.patch.object(engine, "_spawn"), mock.patch.dict(os.environ, {}, clear=True):
            submitted = engine.start_job(PRIVATE, require_existing_claude_login=True)
            directory = engine._directory(submitted["job_id"])
            state = read_json(directory / "state.json")
            execute(engine, directory, state)
        state = read_json(directory / "state.json")
        self.assertFalse((directory / "prompt.private.txt").exists())
        self.assertFalse(list(directory.glob("command.*.private.json")))
        self.assertNotIn(PRIVATE, json.dumps(state))
        return state

    def test_successful_matching_producer_publishes_verified_author_result(self):
        state = self.run_author([init(), assistant(), success()])
        self.assertEqual("authored", state["status"], state)
        self.assertTrue(state["model_verified"])
        self.assertEqual(MODEL, state["observed_model"])
        self.assertFalse(state["author_result"]["is_error"])
        self.assertGreater(state["author_finished_at"], 0)

    def test_cli_success_cannot_publish_unverified_author_result(self):
        no_usage = success()
        no_usage.pop("modelUsage")
        cases = (([init(), assistant(OTHER), success()], "model_mismatch"),
                 ([init(), assistant(), no_usage], "model_observation_missing"),
                 ([success()], "model_observation_missing"))
        for events, code in cases:
            with self.subTest(code=code):
                state = self.run_author(events)
                self.assertEqual("failed", state["status"], state)
                self.assertEqual(code, state["error"]["code"])
                for field in ("author_result", "author_finished_at", "observed_model", "model_verified"):
                    self.assertNotIn(field, state)

    def test_author_failure_retains_safe_error_precedence(self):
        result = {"type": "result", "subtype": "error_during_execution", "is_error": True,
                  "error": {"type": "authentication_error", "message": PRIVATE}}
        state = self.run_author([result], returncode=1)
        self.assertEqual("authentication_failed", state["error"]["code"])

    def test_auth_route_failure_stops_before_launch_and_is_not_relabelled(self):
        changed = {"loggedIn": True, "authMethod": "api_key", "apiProvider": "firstParty"}
        with mock.patch("engine.worker.launch") as launch:
            state = self.run_author([init(), success()], effective_auth=changed)
        launch.assert_not_called()
        self.assertEqual("authentication_route_changed", state["error"]["code"])
        self.assertNotIn("model_evidence", state)


if __name__ == "__main__":
    unittest.main()
