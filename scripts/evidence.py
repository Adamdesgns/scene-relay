"""Stamp verified prior-run evidence into an authored managed revision project.

Usage:
  python scripts/evidence.py --data-root ABS_ROOT --project ABS_PROJECT \
    --author-job-id UUID --render-job-id UUID --evidence ABS_PUBLIC_JSON

The target must be an authored, idle revision of the completed render job. The
input follows production/CLAUDE-BRIEF.md's complete evidence schema. Positive
author/model/media values are cross-checked against engine state and the actual
render report/file. Verification checks are derived here, never accepted as
arbitrary prose. The supplied sanitized revision request is an operator-provided
record; this helper does not attest prompt history or human playback review.
Revision proof checks changed source and exact phrase presence; timing and the
visible after-state still require render/playback review.

Only index.html's unique <script id="run-evidence" type="application/json">
payload and evidence.json are changed. Existing engine locks serialize the edit;
temporary files are removed. The resulting project MUST be rendered again: its
embedded media facts describe the prior verified preview, not its future output.
No model, renderer, job engine instance, network or subprocess is invoked.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
import html
from html.parser import HTMLParser
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine.core import EngineError, FileLock, checked_path

LIMIT = 2 * 1024 * 1024
PUBLIC_KEYS = {"schema_version", "product", "authoring", "revision", "render", "verification"}


class EvidenceError(ValueError):
    pass


class WriteFailure(EvidenceError):
    def __init__(self, restored):
        super().__init__("write_failed_rolled_back" if restored else "write_outcome_uncertain")
        self.changed = False if restored else "uncertain"


def require(condition, code):
    if not condition:
        raise EvidenceError(code)


def read_bytes(path: Path, limit=LIMIT):
    checked_path(path, must_exist=True, directory=False)
    require(path.stat().st_size <= limit, "input_too_large")
    return path.read_bytes()


def parse_json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "duplicate_json_key")
            result[key] = value
        return result
    try:
        value = json.loads(raw, object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(EvidenceError("nonfinite_json")))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise EvidenceError("invalid_json") from error
    require(isinstance(value, dict), "json_object_required")
    return value


def read_json(path):
    return parse_json(read_bytes(path))


def keyset(value, keys, name):
    require(isinstance(value, dict) and set(value) == set(keys), "invalid_" + name + "_fields")


def public_text(value, limit, name):
    require(isinstance(value, str) and 0 < len(value) <= limit, "invalid_" + name)
    require(not any(ord(c) < 32 for c in value), "control_character_in_" + name)
    require(not re.search(r"(?:[A-Za-z]:[\\/]|\\\\|sk-[A-Za-z0-9]|Bearer\s|BEGIN .*PRIVATE KEY)", value),
            "private_material_in_" + name)
    return value


def job_id(value):
    try:
        parsed = uuid.UUID(value)
    except (ValueError, TypeError, AttributeError) as error:
        raise EvidenceError("invalid_job_id") from error
    require(parsed.version == 4 and str(parsed) == value, "canonical_uuid4_required")
    return value


def relative_file(project, value):
    require(isinstance(value, str) and 0 < len(value) <= 240 and "\\" not in value and ":" not in value,
            "invalid_relative_file")
    parts = PurePosixPath(value)
    require(not parts.is_absolute() and all(p not in {".", "..", ""} for p in value.split("/")),
            "invalid_relative_file")
    require(not any(p.startswith(".") or p in {"node_modules", "__pycache__"} for p in parts.parts),
            "private_or_dependency_file")
    path = checked_path(project / value, must_exist=True, directory=False)
    require(path.is_relative_to(project), "file_outside_project")
    return path


def digest(path):
    checked_path(path, must_exist=True, directory=False)
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


class EvidenceBlock(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=False)
        self.text = text
        self.offsets = [0]
        for match in re.finditer("\n", text):
            self.offsets.append(match.end())
        self.count = 0
        self.start = self.end = None
        self.inside = False

    def character_offset(self):
        line, column = self.getpos()
        return self.offsets[line - 1] + column

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if values.get("id") != "run-evidence":
            return
        self.count += 1
        require(tag == "script" and values == {"id": "run-evidence", "type": "application/json"}
                and len(attrs) == 2, "invalid_run_evidence_tag")
        require(not self.inside, "nested_run_evidence")
        self.start = self.character_offset() + len(self.get_starttag_text())
        self.inside = True

    def handle_endtag(self, tag):
        if self.inside and tag == "script":
            self.end = self.character_offset()
            self.inside = False


def block(text):
    parser = EvidenceBlock(text)
    parser.feed(text)
    parser.close()
    require(parser.count == 1 and parser.start is not None and parser.end is not None and not parser.inside,
            "unique_complete_run_evidence_block_required")
    parse_json(text[parser.start:parser.end])
    return parser.start, parser.end


def without_block(text):
    start, end = block(text)
    return text[:start] + text[end:]


def job(root, identifier):
    directory = checked_path(root / "jobs" / job_id(identifier), must_exist=True, directory=True)
    state = read_json(directory / "state.json")
    require(state.get("job_id") == identifier, "job_state_id_mismatch")
    project = checked_path(directory / "project", must_exist=True, directory=True)
    return directory, project, state


def author_success(state):
    result = state.get("author_result", {})
    require(state.get("status") in {"authored", "completed"} and
            result.get("subtype") == "success" and result.get("is_error") is False and
            type(state.get("author_finished_at")) in (float, int), "author_success_not_proven")
    model = public_text(state.get("observed_model"), 100, "observed_model")
    require(re.fullmatch(r"[a-zA-Z0-9._:-]+", model) is not None, "invalid_observed_model")
    return model


def validate_input(value):
    keyset(value, PUBLIC_KEYS, "evidence")
    require(value["schema_version"] == 1, "unsupported_evidence_schema")
    keyset(value["product"], {"name", "author", "free_plugin", "local_only"}, "product")
    require(value["product"] == {"name": "Scene Relay", "author": "@adamdesgns",
                                  "free_plugin": True, "local_only": True}, "unexpected_product")
    keyset(value["authoring"], {"status", "provider", "model", "job_id", "files"}, "authoring")
    keyset(value["revision"], {"status", "request", "before", "after"}, "revision")
    keyset(value["render"], {"status", "width", "height", "fps", "duration_seconds", "audio_present"}, "render")
    keyset(value["verification"], {"status", "checks"}, "verification")
    require(value["authoring"]["status"] == "completed" and value["revision"]["status"] == "completed"
            and value["render"]["status"] == "completed" and value["verification"]["status"] == "passed",
            "complete_verified_evidence_required")
    require(value["authoring"]["provider"] == "Claude Code", "unexpected_provider")
    for key in ("request", "before", "after"):
        public_text(value["revision"][key], 400 if key == "request" else 160, "revision_" + key)
    require(value["revision"]["before"] != value["revision"]["after"], "revision_must_change_text")
    require(isinstance(value["verification"]["checks"], list) and len(value["verification"]["checks"]) <= 20,
            "invalid_verification_checks")


def verify(root, target, value, author_id, render_id, revision_file):
    validate_input(value)
    target_id = target.parent.name
    _, target_project, current = job(root, target_id)
    require(target == target_project and current.get("status") == "authored" and current.get("phase") == "author",
            "target_must_be_idle_authored_revision")
    author_dir, author_project, author = job(root, author_id)
    render_dir, render_project, rendered = job(root, render_id)
    require(target_id != render_id and render_id in current.get("revision_of", []), "revision_parent_mismatch")
    author_model = author_success(author)
    author_success(current)
    require(value["authoring"]["job_id"] == author_id and value["authoring"]["model"] == author_model,
            "author_metadata_mismatch")
    files = value["authoring"]["files"]
    require(isinstance(files, list) and 1 <= len(files) <= 12 and len(set(files)) == len(files), "invalid_source_files")
    require("index.html" in files, "authored_index_required")
    for name in files:
        source = relative_file(author_project, name)
        require(source.suffix.lower() in {".html", ".css", ".js", ".json"} and source.stat().st_size > 0,
                "invalid_authored_file")
        relative_file(target, name)
    require(rendered.get("status") == "completed" and rendered.get("phase") == "render"
            and rendered.get("exit_code") == 0, "completed_render_required")
    report = read_json(render_project / "output" / "render-report.json")
    require(report.get("schema_version") == 1 and report.get("ok") is True, "verified_render_report_required")
    check = report.get("check", {})
    require(isinstance(check, dict) and check.get("ok") is True, "passing_hyperframes_check_required")
    findings = check.get("findings")
    require(isinstance(findings, list) and len(findings) <= 100, "invalid_hyperframes_findings")
    for finding in findings:
        require(isinstance(finding, dict) and set(finding) == {"code", "severity"}
                and isinstance(finding["code"], str)
                and re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", finding["code"]) is not None
                and isinstance(finding["severity"], str)
                and finding["severity"] in {"warning", "info"}, "blocking_or_invalid_hyperframes_finding")
    warning_count = sum(finding["severity"] == "warning" for finding in findings)
    info_count = sum(finding["severity"] == "info" for finding in findings)
    check_label = "Prior preview HyperFrames check"
    if findings:
        check_label += f" ({warning_count} warning(s); {info_count} informational finding(s))"
    output = report.get("output", {})
    path = relative_file(render_project, output.get("file"))
    require(path.suffix == ".mp4" and path.stat().st_size == output.get("bytes") and
            digest(path) == output.get("sha256"), "render_output_hash_mismatch")
    hashes = report.get("source", {}).get("source_sha256")
    require(isinstance(hashes, dict) and "index.html" in hashes, "render_source_hashes_required")
    for name, expected in hashes.items():
        source = relative_file(render_project, name)
        # The renderer hashes decoded UTF-8 text rather than original file bytes.
        read_bytes(source)  # Enforce the size/path bound before decoding.
        actual = hashlib.sha256(source.read_text(encoding="utf-8-sig").encode()).hexdigest()
        require(actual == expected, "render_source_changed")
    media = report.get("media", {})
    for field in ("width", "height"):
        require(type(media.get(field)) is int and media[field] > 0, "incomplete_media_dimensions")
    for field in ("fps", "duration"):
        require(type(media.get(field)) in (float, int) and math.isfinite(media[field]) and media[field] > 0,
                "incomplete_media_timing")
    require(isinstance(media.get("video_codec"), str) and isinstance(media.get("audio_codec"), str)
            and type(media.get("audio_channels")) is int and media["audio_channels"] > 0,
            "verified_video_and_audio_required")
    expected_render = {"status": "completed", "width": media["width"], "height": media["height"],
                       "fps": media["fps"], "duration_seconds": media["duration"], "audio_present": True}
    require(value["render"] == expected_render, "supplied_render_metadata_mismatch")
    before_file = relative_file(render_project, revision_file)
    after_file = relative_file(target, revision_file)
    require(before_file.suffix == ".html" and after_file.suffix == ".html", "revision_html_required")
    before = read_bytes(before_file).decode("utf-8")
    after = read_bytes(after_file).decode("utf-8")
    before_clean = without_block(before) if revision_file == "index.html" else before
    after_clean = without_block(after) if revision_file == "index.html" else after
    require(before_clean != after_clean, "revision_only_changed_evidence")
    require(value["revision"]["before"] in html.unescape(before_clean) and
            value["revision"]["after"] in html.unescape(after_clean), "revision_text_not_in_source")
    # Restrict public successes to checks this helper actually established. The
    # renderer's ok flag is emitted only after decoding its picture and sound.
    value["verification"] = {"status": "passed", "checks": [
        {"label": "Official CLI author result", "status": "passed"},
        {"label": "Changed revision source", "status": "passed"},
        {"label": check_label, "status": "passed"},
        {"label": "Prior preview video and audio decode", "status": "passed"},
        {"label": "Prior preview output SHA-256", "status": "passed"}]}
    return {"author": author_dir / "state.json", "render": render_dir / "state.json",
            "target": target.parent / "state.json"}


def atomic_file(path, payload):
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def stamp(args):
    root = checked_path(args.data_root, must_exist=True, directory=True)
    target = checked_path(args.project, must_exist=True, directory=True)
    require(target.name == "project" and target.parent.parent == root / "jobs", "managed_project_required")
    job_id(target.parent.name)
    config = read_json(root / "config.private.json")
    require(config.get("test_mode") is False, "test_mode_evidence_rejected")
    value = read_json(checked_path(args.evidence, must_exist=True, directory=False))
    # Match engine lock order; acquiring these cannot start, stop or repair a job.
    with ExitStack() as stack:
        stack.enter_context(FileLock(target.parent / "operation.lock"))
        stack.enter_context(FileLock(target.parent / "worker.lock"))
        state_paths = verify(root, target, value, args.author_job_id, args.render_job_id, args.revision_file)
        state_hashes = {key: digest(path) for key, path in state_paths.items()}
        index, evidence_path = target / "index.html", target / "evidence.json"
        original_index, original_evidence = read_bytes(index), read_bytes(evidence_path)
        source = original_index.decode("utf-8")
        start, end = block(source)
        parse_json(original_evidence)
        payload = json.dumps(value, ensure_ascii=True, allow_nan=False, indent=2)
        payload = payload.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
        modified = (source[:start] + "\n" + payload + "\n" + source[end:]).encode("utf-8")
        require(without_block(modified.decode()) == without_block(source), "unexpected_source_change")
        require(all(digest(path) == state_hashes[key] for key, path in state_paths.items()), "job_state_changed")
        require(read_bytes(index) == original_index and read_bytes(evidence_path) == original_evidence,
                "source_changed_during_validation")
        if args.check:
            return {"ok": True, "changed": False, "mode": "check", "evidence_scope": "prior_verified_preview"}
        try:
            atomic_file(index, modified)
            atomic_file(evidence_path, (payload + "\n").encode())
            require(read_bytes(index) == modified and parse_json(read_bytes(evidence_path)) == value,
                    "evidence_write_verification_failed")
        except (OSError, EvidenceError):
            restored = True
            for path, original in ((index, original_index), (evidence_path, original_evidence)):
                try:
                    atomic_file(path, original)
                    restored = restored and read_bytes(path) == original
                except (OSError, EvidenceError):
                    restored = False
            raise WriteFailure(restored) from None
        return {"ok": True, "changed": True, "files": ["index.html", "evidence.json"],
                "evidence_scope": "prior_verified_preview", "next_step": "Render and verify the stamped project."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("data-root", "project", "author-job-id", "render-job-id", "evidence"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--revision-file", default="index.html", help="Relative HTML source containing the changed text")
    parser.add_argument("--check", action="store_true", help="Validate without changing the two evidence files")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(stamp(args)))
        return 0
    except (EvidenceError, EngineError, OSError, UnicodeError, TypeError, KeyError) as error:
        # Exceptions never expose private paths, source strings, prompts or config.
        code = str(error) if isinstance(error, EvidenceError) else type(error).__name__
        print(json.dumps({"ok": False, "error": code,
                          "changed": error.changed if isinstance(error, WriteFailure) else False}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
