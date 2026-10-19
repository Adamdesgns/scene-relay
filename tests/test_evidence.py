"""Synthetic evidence integrity tests; never invokes a model or renderer."""
import copy
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from scripts import evidence as helper

BASE = Path(__file__).resolve().parents[1]


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="evidence-test-", dir=BASE / "tests")
        self.root = Path(self.temp.name).resolve()
        assert self.root.is_relative_to((BASE / "tests").resolve())
        self.old_id, self.new_id = str(uuid.uuid4()), str(uuid.uuid4())
        self.old = self.root / "jobs" / self.old_id / "project"
        self.new = self.root / "jobs" / self.new_id / "project"
        self.old.mkdir(parents=True)
        self.new.mkdir(parents=True)
        self.pending = {"schema_version": 1, "pending": True}
        self.old_html = ('<html>\r\n<body><h1>Brief your next launch.</h1>\r\n'
                         '<script id="run-evidence" type="application/json">' +
                         json.dumps(self.pending) + '</script>\r\n</body></html>')
        self.new_html = self.old_html.replace("Brief your next launch.", "Make your next launch a film.")
        (self.old / "index.html").write_bytes(self.old_html.encode())
        (self.new / "index.html").write_bytes(self.new_html.encode())
        for directory in (self.old, self.new):
            self.put(directory / "evidence.json", self.pending)
            self.put(directory / "hyperframes.json", {})
        self.put(self.root / "config.private.json", {"test_mode": False})
        auth = {"subtype": "success", "is_error": False}
        self.old_state = {"job_id": self.old_id, "status": "completed", "phase": "render", "exit_code": 0,
                          "author_finished_at": 1, "author_result": auth, "observed_model": "claude-opus-5-5",
                          "revision_of": []}
        self.new_state = {"job_id": self.new_id, "status": "authored", "phase": "author", "exit_code": 0,
                          "author_finished_at": 2, "author_result": auth, "observed_model": "claude-opus-5-5",
                          "revision_of": [self.old_id]}
        self.put(self.old.parent / "state.json", self.old_state)
        self.put(self.new.parent / "state.json", self.new_state)
        output = self.old / "output"
        output.mkdir()
        self.media_path = output / "final.mp4"
        self.media_path.write_bytes(b"Synthetic integrity fixture only. Not a media render.")
        self.report = {"schema_version": 1, "ok": True, "check": {"ok": True, "findings": []},
                       "source": {"source_sha256": {"index.html": hashlib.sha256(
                           (self.old / "index.html").read_text(encoding="utf-8-sig").encode()).hexdigest()}},
                       "media": {"duration": 40.0, "width": 1920, "height": 1080, "fps": 30.0,
                                 "video_codec": "h264", "audio_codec": "aac", "audio_channels": 2},
                       "output": {"file": "output/final.mp4", "bytes": self.media_path.stat().st_size,
                                  "sha256": helper.digest(self.media_path)}}
        self.put(output / "render-report.json", self.report)
        self.value = {"schema_version": 1,
                      "product": {"name": "Scene Relay", "author": "@adamdesgns", "free_plugin": True, "local_only": True},
                      "authoring": {"status": "completed", "provider": "Claude Code", "model": "claude-opus-5-5",
                                    "job_id": self.new_id, "files": ["index.html", "hyperframes.json"]},
                      "revision": {"status": "completed", "request": "Make the ending more direct.",
                                   "before": "Brief your next launch.", "after": "Make your next launch a film."},
                      "render": {"status": "completed", "width": 1920, "height": 1080, "fps": 30.0,
                                 "duration_seconds": 40.0, "audio_present": True},
                      "verification": {"status": "passed", "checks": []}}
        self.put(self.root / "public.json", self.value)
        self.args = SimpleNamespace(data_root=str(self.root), project=str(self.new), author_job_id=self.new_id,
                                    render_job_id=self.old_id, evidence=str(self.root / "public.json"),
                                    revision_file="index.html", check=False)

    def put(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def reject(self, code):
        original = (self.new / "index.html").read_bytes()
        with self.assertRaisesRegex(helper.EvidenceError, "^" + code + "$"):
            helper.stamp(self.args)
        self.assertEqual((self.new / "index.html").read_bytes(), original)

    def test_exact_payload_edit_and_predecessor_preserved(self):
        result = helper.stamp(self.args)
        self.assertTrue(result["changed"])
        raw = (self.new / "index.html").read_bytes().decode()
        start, end = helper.block(raw)
        self.assertEqual(helper.parse_json(raw[start:end]), helper.read_json(self.new / "evidence.json"))
        self.assertEqual(helper.without_block(raw), helper.without_block(self.new_html))
        self.assertEqual((self.old / "index.html").read_bytes(), self.old_html.encode())

    def test_dry_run(self):
        self.args.check = True
        self.assertFalse(helper.stamp(self.args)["changed"])
        self.assertEqual((self.new / "index.html").read_bytes(), self.new_html.encode())

    def test_failed_job_cannot_supply_success(self):
        self.put(self.new.parent / "state.json", {**self.new_state, "status": "failed"})
        self.reject("target_must_be_idle_authored_revision")

    def test_test_mode_rejected(self):
        self.put(self.root / "config.private.json", {"test_mode": True})
        self.reject("test_mode_evidence_rejected")

    def test_active_worker_lock_prevents_mutation(self):
        original = (self.new / "index.html").read_bytes()
        with helper.FileLock(self.new.parent / "worker.lock"):
            with self.assertRaises(helper.EngineError):
                helper.stamp(self.args)
        self.assertEqual((self.new / "index.html").read_bytes(), original)

    def test_unmanaged_target_rejected(self):
        self.args.project = str(self.root)
        self.reject("managed_project_required")

    def test_embedded_script_terminator_is_escaped(self):
        value = copy.deepcopy(self.value)
        value["revision"]["request"] = "Keep </script> as literal text & no markup."
        self.put(self.root / "public.json", value)
        helper.stamp(self.args)
        text = (self.new / "index.html").read_bytes().decode()
        start, end = helper.block(text)
        self.assertNotIn("</script>", text[start:end])
        self.assertEqual(helper.parse_json(text[start:end])["revision"]["request"], value["revision"]["request"])

    def test_media_hash_mismatch(self):
        self.media_path.write_bytes(b"changed")
        self.reject("render_output_hash_mismatch")

    def test_passing_check_keeps_warning_and_info_count(self):
        self.report["check"]["findings"] = [
            {"code": "nested_structure_needs_subcomposition", "severity": "warning"},
            {"code": "timeline_track", "severity": "warning"},
            {"code": "example_information", "severity": "info"}]
        self.put(self.old / "output" / "render-report.json", self.report)
        self.assertTrue(helper.stamp(self.args)["changed"])
        result = helper.read_json(self.new / "evidence.json")
        labels = [check["label"] for check in result["verification"]["checks"]]
        self.assertIn("Prior preview HyperFrames check (2 warning(s); 1 informational finding(s))", labels)

    def test_error_unknown_and_malformed_findings_rejected(self):
        cases = [
            {"code": "runtime_error", "severity": "error"},
            {"code": "unspecified", "severity": "unknown"},
            {"code": "missing_severity"},
            {"code": "bad code", "severity": "warning"},
            {"code": "wrong_type", "severity": None},
            {"code": "wrong_container", "severity": []},
            "warning"]
        for finding in cases:
            with self.subTest(finding=finding):
                self.report["check"]["findings"] = [finding]
                self.put(self.old / "output" / "render-report.json", self.report)
                self.reject("blocking_or_invalid_hyperframes_finding")

    def test_warning_does_not_override_failed_check(self):
        self.report["check"] = {"ok": False, "findings": [{"code": "notice", "severity": "warning"}]}
        self.put(self.old / "output" / "render-report.json", self.report)
        self.reject("passing_hyperframes_check_required")

    def test_malformed_findings_container_rejected(self):
        for value in (None, {}, "warning"):
            with self.subTest(value=value):
                self.report["check"]["findings"] = value
                self.put(self.old / "output" / "render-report.json", self.report)
                self.reject("invalid_hyperframes_findings")

    def test_stale_source_rejected(self):
        (self.old / "index.html").write_bytes(self.old_html.replace("<h1>", "<h1 class='changed'>").encode())
        self.reject("render_source_changed")

    def test_invented_fps_rejected(self):
        bad = copy.deepcopy(self.value)
        bad["render"]["fps"] = 60
        self.put(self.root / "public.json", bad)
        self.reject("supplied_render_metadata_mismatch")

    def test_evidence_only_change_is_not_revision(self):
        (self.new / "index.html").write_bytes(self.old_html.encode())
        self.reject("revision_only_changed_evidence")

    def test_missing_or_duplicate_block(self):
        for text in ("<html></html>", self.old_html + self.old_html):
            with self.assertRaisesRegex(helper.EvidenceError, "unique_complete_run_evidence_block_required"):
                helper.block(text)

    def test_duplicate_keys_rejected(self):
        with self.assertRaisesRegex(helper.EvidenceError, "duplicate_json_key"):
            helper.parse_json('{"a":1,"a":2}')

    def test_secondary_write_failure_rolls_back(self):
        actual = helper.atomic_file
        failed = False
        def fail_once(path, payload):
            nonlocal failed
            if path.name == "evidence.json" and not failed:
                failed = True
                raise OSError("synthetic write failure")
            actual(path, payload)
        with patch.object(helper, "atomic_file", fail_once):
            self.reject("write_failed_rolled_back")

    def test_preview_has_no_script_or_network_resource(self):
        class Page(HTMLParser):
            def handle_starttag(self, tag, attrs):
                if tag == "script":
                    raise AssertionError("script")
                for key, value in attrs:
                    if key in {"src", "href", "poster"} and value.startswith(("https:", "http:", "//")):
                        raise AssertionError("remote resource")
        Page().feed((BASE / "docs" / "preview-template.html").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
