from __future__ import annotations
import argparse
import contextlib
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from .core import Config, Engine, EngineError, FileLock, MODEL_RE, atomic_json, checked_path, process_identity, read_json, resource_error
from .supervisor import OwnedJob, kill_owned

OVERRIDE_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS", "ANTHROPIC_API_KEY_FILE", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "AWS_BEARER_TOKEN_BEDROCK")
RESULT_SUBTYPES = {"success", "error_max_turns", "error_during_execution", "error_max_budget_usd", "error_max_structured_output_retries"}
TOOL_NAMES = {"Read", "Write", "Edit", "Glob", "Grep"}
RENDER_STAGES = {"check", "render", "mux", "mux_audio", "probe", "probe_sound", "probe_final", "decode_verify", "complete", "failed"}
ERROR_CODES = (("rate_limit", "quota_or_rate_limit"), ("rate limit", "quota_or_rate_limit"), ("usage limit", "quota_or_rate_limit"), ("credit balance", "billing_limit"), ("insufficient credits", "billing_limit"), ("billing_error", "billing_limit"), ("authentication", "authentication_failed"), ("not logged", "authentication_failed"), ("unauthorized", "authentication_failed"), ("invalid api key", "authentication_failed"), ("model_not_found", "model_unavailable"), ("model not found", "model_unavailable"), ("not have access", "model_access_denied"), ("permission", "permission_denied"), ("forbidden", "permission_denied"), ("unable to connect", "network_error"), ("connection error", "network_error"), ("connection failed", "network_error"), ("connection refused", "network_error"), ("econnrefused", "network_error"), ("enotfound", "network_error"), ("getaddrinfo", "network_error"), ("etimedout", "network_error"), ("network error", "network_error"), ("fetch failed", "network_error"), ("certificate verify", "network_error"), ("tls handshake", "network_error"), ("overloaded", "service_unavailable"), ("service unavailable", "service_unavailable"), ("server_error", "service_unavailable"))


def classify(text):
    if not isinstance(text, str):
        return None
    text = text.lower()
    for needle, code in ERROR_CODES:
        if needle in text:
            return code
    return None


def classify_error_payload(value):
    """Inspect bounded error fields in memory; return only a finite safe code."""
    queue = [(value, 0)]
    remaining = 8192
    http_code = None
    while queue and remaining > 0:
        item, depth = queue.pop(0)
        if isinstance(item, str):
            text = item[:remaining]
            remaining -= len(text)
            code = classify(text)
            if code:
                return code
        elif type(item) is int:
            http_code = http_code or {401: "authentication_failed", 403: "permission_denied", 429: "quota_or_rate_limit", 500: "service_unavailable", 502: "service_unavailable", 503: "service_unavailable", 504: "network_error", 529: "service_unavailable"}.get(item)
        elif depth < 3 and isinstance(item, dict):
            queue.extend((item[key], depth + 1) for key in ("type", "code", "error", "message", "text", "status", "status_code", "errors") if key in item)
        elif depth < 3 and isinstance(item, list):
            queue.extend((entry, depth + 1) for entry in item[:20])
    return http_code


class ModelPolicy:
    """Constant-size model evidence, independent of the progress event ring.

    Only exact equality is accepted. Unmatched identifiers and usage payloads
    remain in memory for this comparison and are never persisted.
    """
    MAX_USAGE_MODELS = 16

    def __init__(self, requested):
        self.requested = requested
        self.initialized = False
        self.assistant_count = 0
        self.final_usage = False
        self.result_count = 0
        self.mismatch = False
        self.invalid = False
        self.incomplete = False

    def observe(self, model):
        if not isinstance(model, str) or not MODEL_RE.fullmatch(model):
            self.invalid = True
            return False
        if model != self.requested:
            self.mismatch = True
        return True

    def initialization(self, event):
        if "model" in event:
            self.initialized |= self.observe(event["model"])

    def assistant(self, message):
        # Some supported CLI events omit message.model; the mandatory terminal
        # modelUsage map supplies the aggregate evidence for those messages.
        if isinstance(message, dict) and "model" in message:
            if self.observe(message["model"]):
                self.assistant_count = min(self.assistant_count + 1, 1000000)

    def result(self, event):
        self.result_count = min(self.result_count + 1, 2)
        if self.result_count != 1:
            self.invalid = True
        self.final_usage = False
        if "modelUsage" not in event:
            return
        usage = event["modelUsage"]
        if not isinstance(usage, dict):
            self.invalid = True
            return
        if not usage:
            return
        if len(usage) > self.MAX_USAGE_MODELS:
            self.invalid = True
            return
        valid = True
        for model, values in usage.items():
            valid = self.observe(model) and valid
            if not isinstance(values, dict):
                self.invalid = True
                valid = False
        self.final_usage = valid

    def error(self):
        if self.mismatch:
            return "model_mismatch"
        if self.invalid:
            return "model_observation_invalid"
        if self.incomplete:
            return "model_observation_incomplete"
        if not self.initialized or not self.final_usage:
            return "model_observation_missing"
        return None

    def summary(self):
        return {"initialization_observed": self.initialized,
                "assistant_observations": self.assistant_count,
                "final_usage_observed": self.final_usage,
                "mismatch": self.mismatch, "invalid": self.invalid,
                "incomplete": self.incomplete}


class Collector:
    """Bounded allowlisted metadata; never retains model text or tool inputs."""
    def __init__(self, allow_render=False, requested_model=None):
        self.lock = threading.Lock()
        self.events = []
        self.result = None
        self.error = None
        self.byte_count = 0
        self.dropped_lines = 0
        self.allow_render = allow_render
        self.model_policy = ModelPolicy(requested_model) if requested_model is not None else None

    def add(self, event):
        self.events.append(event)
        self.events = self.events[-100:]

    def consume(self, raw, stream):
        with self.lock:
            self.byte_count += len(raw)
            if stream != "stdout":
                self.error = self.error or classify(raw[:4096].decode("utf-8", "replace"))
                return
            try:
                event = json.loads(raw)
            except (ValueError, UnicodeError):
                if self.model_policy:
                    self.model_policy.incomplete = True
                return
            if not isinstance(event, dict):
                if self.model_policy:
                    self.model_policy.incomplete = True
                return
            if self.allow_render and isinstance(event.get("stage"), str) and event["stage"] in RENDER_STAGES:
                progress = event.get("state")
                if not isinstance(progress, str) or progress not in {"running", "succeeded", "failed"}:
                    progress = "failed" if event["stage"] == "failed" else "running"
                self.add({"type": "render_progress", "stage": event["stage"], "state": progress})
                return
            kind = event.get("type")
            if kind == "system" and event.get("subtype") == "init":
                data = {"type": "initialized"}
                model = event.get("model")
                if self.model_policy:
                    self.model_policy.initialization(event)
                if isinstance(model, str) and MODEL_RE.fullmatch(model) and (not self.model_policy or model == self.model_policy.requested):
                    data["model"] = model
                self.add(data)
            elif kind == "assistant":
                self.add({"type": "assistant_turn"})
                message = event.get("message")
                if self.model_policy:
                    self.model_policy.assistant(message)
                error_marker = event.get("error") or (message.get("error") if isinstance(message, dict) else None)
                if error_marker:
                    self.error = self.error or classify_error_payload(error_marker)
                    if isinstance(message, dict) and isinstance(message.get("content"), list):
                        error_content = [{"text": part.get("text")} for part in message["content"][:20] if isinstance(part, dict) and part.get("type") == "text"]
                        self.error = self.error or classify_error_payload(error_content)
                    if self.error:
                        self.add({"type": "author_error", "code": self.error})
                if isinstance(message, dict) and isinstance(message.get("content"), list):
                    for part in message["content"][:100]:
                        if isinstance(part, dict) and part.get("type") == "tool_use" and isinstance(part.get("name"), str) and part["name"] in TOOL_NAMES:
                            self.add({"type": "tool_use", "tool": part["name"]})
            elif kind == "result":
                if self.model_policy:
                    self.model_policy.result(event)
                subtype = event.get("subtype")
                data = {"type": "result", "is_error": event.get("is_error") is True, "subtype": subtype if isinstance(subtype, str) and subtype in RESULT_SUBTYPES else "unknown"}
                for field in ("duration_ms", "duration_api_ms", "num_turns", "total_cost_usd"):
                    value = event.get(field)
                    if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 10 ** 12:
                        data[field] = value
                usage = event.get("usage")
                if isinstance(usage, dict):
                    data["usage"] = {key: value for key, value in usage.items() if key in {"input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"} and type(value) is int and 0 <= value <= 10 ** 12}
                if data["is_error"] or data["subtype"] != "success":
                    self.error = self.error or classify_error_payload([event.get("result"), event.get("errors"), event.get("error")])
                self.result = data
                self.add(data)
            elif kind == "error":
                self.error = self.error or classify_error_payload(event)
                if self.error:
                    self.add({"type": "author_error", "code": self.error})

    def snapshot(self):
        with self.lock:
            metadata = {"events": list(self.events), "stream_bytes_discarded": self.byte_count, "oversized_lines_discarded": self.dropped_lines}
            if self.model_policy:
                metadata["model_evidence"] = self.model_policy.summary()
            return metadata, self.result, self.error

    def model_error(self):
        with self.lock:
            return self.model_policy.error() if self.model_policy else "model_observation_missing"


def stream_reader(pipe, collector, name):
    buffer = bytearray()
    dropping = False
    try:
        while True:
            # BufferedReader.read(n) can wait for n bytes or EOF even after a
            # child flushes a complete short JSON event. read1 returns the
            # currently available raw-pipe chunk so progress remains live.
            chunk = pipe.read1(4096)
            if not chunk:
                break
            for segment in chunk.splitlines(keepends=True):
                ends = segment.endswith(b"\n")
                if not dropping:
                    buffer.extend(segment)
                    if len(buffer) > 65536:
                        with collector.lock:
                            collector.dropped_lines += 1
                            collector.byte_count += len(buffer)
                            if name == "stdout" and collector.model_policy:
                                collector.model_policy.incomplete = True
                        buffer.clear()
                        dropping = True
                else:
                    with collector.lock:
                        collector.byte_count += len(segment)
                if ends:
                    if not dropping:
                        collector.consume(bytes(buffer), name)
                    buffer.clear()
                    dropping = False
        if buffer and not dropping:
            collector.consume(bytes(buffer), name)
    except Exception:
        # A broken reader cannot establish that all reported models matched.
        with collector.lock:
            if name == "stdout" and collector.model_policy:
                collector.model_policy.incomplete = True
    finally:
        pipe.close()


def launch(directory, command, cwd, *, stdin=subprocess.PIPE):
    spec = directory / ("command." + uuid.uuid4().hex + ".private.json")
    atomic_json(spec, {"command": list(command), "cwd": str(cwd)})
    kwargs = {"stdin": stdin, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "cwd": str(Path(__file__).resolve().parent.parent), "close_fds": True, "shell": False}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen([sys.executable, "-m", "engine.child_runner", str(spec)], **kwargs)
    return process, spec


def bounded_capture(directory, command, cwd, timeout=15):
    process, spec = launch(directory, command, cwd, stdin=subprocess.DEVNULL)
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    def drain(pipe, key):
        try:
            while True:
                chunk = pipe.read(4096)
                if not chunk:
                    break
                remaining = 65536 - len(buffers[key])
                if remaining > 0:
                    buffers[key].extend(chunk[:remaining])
        finally:
            pipe.close()
    readers = [threading.Thread(target=drain, args=(getattr(process, key), key), daemon=True) for key in buffers]
    for reader in readers:
        reader.start()
    try:
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_owned(process)
            return None, b""
        for reader in readers:
            reader.join(timeout=1)
        return process.returncode, bytes(buffers["stdout"])
    finally:
        kill_owned(process)
        spec.unlink(missing_ok=True)


def effective_settings(directory):
    return ["--safe-mode", "--restricted", "--strict-mcp-config", "--mcp-config", str(directory / "empty-mcp.json"), "--no-chrome", "--disable-slash-commands"]


def auth_status(config, directory, project, *, effective):
    overrides = [name for name in OVERRIDE_VARS if os.environ.get(name)]
    if config.test_mode and config.test_auth_status is not None:
        value = config.test_effective_auth_status if effective and config.test_effective_auth_status is not None else config.test_auth_status
    else:
        command = [config.claude_path] + (effective_settings(directory) if effective else []) + ["auth", "status"]
        code, raw = bounded_capture(directory, command, project)
        if code != 0:
            return {"logged_in": False, "api_overrides_present": bool(overrides)}, "authentication_status_failed", None
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            return {"logged_in": False, "api_overrides_present": bool(overrides)}, "authentication_status_invalid", None
    if not isinstance(value, dict):
        return {}, "authentication_status_invalid", None
    safe = {"logged_in": value.get("loggedIn") is True, "auth_method": "unknown", "provider": "unknown", "api_overrides_present": bool(overrides), "override_variable_names": overrides}
    # Values selected from known enums, never arbitrary CLI strings (e.g. email).
    if isinstance(value.get("authMethod"), str) and value["authMethod"] in {"claude.ai", "api_key", "api-key", "apiKey", "oauth", "external", "none", "console"}:
        safe["auth_method"] = value["authMethod"]
    if isinstance(value.get("apiProvider"), str) and value["apiProvider"] in {"firstParty", "bedrock", "vertex", "foundry"}:
        safe["provider"] = value["apiProvider"]
    if isinstance(value.get("subscriptionType"), str) and value["subscriptionType"] in {"max", "pro", "team", "enterprise", "free"}:
        safe["subscription_type"] = value["subscriptionType"]
    # Compare original values in memory as well as the public allowlisted route;
    # an unfamiliar future official auth method must not mask a route change.
    route = (value.get("loggedIn") is True, value.get("authMethod"), value.get("apiProvider"))
    return safe, None if safe["logged_in"] else "not_authenticated", route


def preflight(config, directory, project):
    original, original_error, original_route = auth_status(config, directory, project, effective=False)
    if original_error and original_error != "not_authenticated":
        return original, original_error
    effective, effective_error, effective_route = auth_status(config, directory, project, effective=True)
    safe = dict(effective)
    safe["original_route"] = {key: original.get(key) for key in ("logged_in", "auth_method", "provider")}
    if effective_error and effective_error != "not_authenticated":
        return safe, effective_error
    if original_route != effective_route:
        return safe, "authentication_route_changed"
    safe["route_verified"] = True
    return safe, effective_error


def author_command(config, state, directory):
    if config.test_mode and config.test_author_command:
        return list(config.test_author_command)
    return [config.claude_path, "-p", "--model", state["model"], "--output-format", "stream-json", "--verbose", "--max-turns", str(state["max_turns"])] + effective_settings(directory) + ["--permission-mode", "acceptEdits", "--permission-prompts", "none", "--tools", "Read,Write,Edit,Glob,Grep", "--no-session-persistence"]


def validate_media(config, directory, project, output):
    checked_path(output, must_exist=True, directory=False)
    if output.stat().st_size < 512:
        return None, "invalid_media"
    code, raw = bounded_capture(directory, [config.ffprobe_path, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(output)], project, timeout=30)
    if code != 0:
        return None, "media_probe_failed"
    try:
        info = json.loads(raw)
        streams = info["streams"]
        videos = [s for s in streams if s.get("codec_type") == "video"]
        audios = [s for s in streams if s.get("codec_type") == "audio"]
        duration = float(info["format"]["duration"])
        if not videos or not audios or not math.isfinite(duration) or not 0 < duration <= 600:
            return None, "invalid_media"
        width, height = int(videos[0]["width"]), int(videos[0]["height"])
        if (width, height) != (config.expected_width, config.expected_height):
            return None, "media_dimensions_mismatch"
        if config.expected_duration is not None and abs(duration - config.expected_duration) > 0.5:
            return None, "media_duration_mismatch"
        return {"bytes": output.stat().st_size, "duration_seconds": duration, "width": width, "height": height, "video_streams": len(videos), "audio_streams": len(audios)}, None
    except (ValueError, TypeError, KeyError, UnicodeError):
        return None, "invalid_media"


def execute(engine, directory, state):
    config = engine.config
    project = checked_path(directory / "project", must_exist=True, directory=True)
    cancel = directory / "cancel.requested"
    def save():
        state["updated_at"] = time.time()
        atomic_json(directory / "state.json", state)
    def fail(code, cancelled=False):
        state.update(status="cancelled" if cancelled else "failed", error={"code": code}, finished_at=time.time())
        save()
    global_lock = FileLock(engine.root / "active-worker.lock")
    try:
        # Queueing itself is bounded, separately from the active-stage deadline.
        queue_deadline = time.monotonic() + state["wall_timeout"]
        while not global_lock.acquire():
            if cancel.exists():
                fail("cancelled", True)
                return
            if time.monotonic() >= queue_deadline:
                fail("queue_timeout")
                return
            time.sleep(0.1)
        if cancel.exists():
            fail("cancelled", True)
            return
        resource = resource_error(config, engine.root)
        if resource:
            fail(resource)
            return
        if state["phase"] == "author":
            auth, error = preflight(config, directory, project)
            state["authentication"] = auth
            save()
            if error:
                fail(error)
                return
            if state["require_existing_claude_login"] and (auth["auth_method"] != "claude.ai" or auth["provider"] != "firstParty" or auth["api_overrides_present"]):
                fail("authentication_policy_mismatch")
                return
            if cancel.exists():
                fail("cancelled", True)
                return
            command = author_command(config, state, directory)
            state["status"] = "authoring"
        else:
            output = project / "output" / "final.mp4"
            checked_path(output)
            output.parent.mkdir(exist_ok=True)
            command = [part.replace("{project}", str(project)).replace("{output}", str(output)) for part in config.render_command]
            state["status"] = "rendering"
        state["started_at"] = time.time()
        save()
        process, spec = launch(directory, command, project)
        collector = Collector(allow_render=state["phase"] == "render",
                              requested_model=state["model"] if state["phase"] == "author" else None)
        readers = [threading.Thread(target=stream_reader, args=(getattr(process, key), collector, key), daemon=True) for key in ("stdout", "stderr")]
        for reader in readers:
            reader.start()
        def feed():
            try:
                if state["phase"] == "author":
                    prompt = checked_path(directory / "prompt.private.txt", must_exist=True, directory=False)
                    with prompt.open("rb") as handle:
                        payload = handle.read(65537)
                    if len(payload) > 65536:
                        return
                    process.stdin.write(payload)
                    process.stdin.flush()
            except (OSError, ValueError):
                pass
            finally:
                with contextlib.suppress(OSError, ValueError):
                    process.stdin.close()
                (directory / "prompt.private.txt").unlink(missing_ok=True)
        feeder = threading.Thread(target=feed, daemon=True)
        feeder.start()
        deadline = time.monotonic() + (config.test_timeout_seconds if config.test_mode and config.test_timeout_seconds is not None else state["wall_timeout"])
        last_save = time.monotonic()
        failure = None
        try:
            while process.poll() is None:
                if cancel.exists():
                    failure = "cancelled"
                    kill_owned(process)
                    break
                if time.monotonic() >= deadline:
                    failure = "wall_timeout"
                    kill_owned(process)
                    break
                if time.monotonic() - last_save > 0.5:
                    metadata, _, _ = collector.snapshot()
                    state.update(metadata)
                    save()
                    last_save = time.monotonic()
                time.sleep(0.05)
            for reader in readers:
                reader.join(timeout=2)
            if readers[0].is_alive():
                with collector.lock:
                    if collector.model_policy:
                        collector.model_policy.incomplete = True
            feeder.join(timeout=1)
            metadata, result, classified = collector.snapshot()
            state.update(metadata)
            state["exit_code"] = process.returncode
            if failure:
                fail(failure, failure == "cancelled")
            elif process.returncode != 0:
                fail(classified or ("author_process_failed" if state["phase"] == "author" else "render_process_failed"))
            elif state["phase"] == "author":
                if result is None:
                    fail("missing_author_result")
                elif result["is_error"] or result["subtype"] != "success":
                    fail(classified or "author_reported_failure")
                else:
                    model_error = collector.model_error()
                    if model_error:
                        fail(model_error)
                        return
                    state["author_result"] = dict(result)
                    state["observed_model"] = state["model"]
                    state["model_verified"] = True
                    state.update(status="authored", finished_at=time.time(), author_finished_at=time.time())
                    save()
            else:
                media, error = validate_media(config, directory, project, output)
                if error:
                    fail(error)
                else:
                    state.update(status="completed", media=media, finished_at=time.time())
                    save()
        finally:
            kill_owned(process)
            spec.unlink(missing_ok=True)
    finally:
        (directory / "prompt.private.txt").unlink(missing_ok=True)
        global_lock.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args()
    directory = None
    try:
        # Fail closed before even auth inspection if owned-tree containment fails.
        owned = OwnedJob()
        engine = Engine.from_data_root(args.data_root)
        directory = engine._directory(args.job_id)
        operation = FileLock(directory / "operation.lock")
        try:
            if not operation.acquire(blocking=True, timeout=10):
                raise EngineError("Startup operation lock unavailable")
            state = read_json(directory / "state.json")
            if state["status"] != "queued":
                return 0
            state.update(worker_pid=os.getpid(), worker_identity=process_identity(os.getpid()), updated_at=time.time())
            atomic_json(directory / "state.json", state)
        finally:
            operation.close()
        execute(engine, directory, state)
        return 0
    except Exception as error:
        if directory is not None:
            with contextlib.suppress(Exception):
                state = read_json(directory / "state.json")
                exception_type = type(error).__name__
                if exception_type not in {"PermissionError", "FileNotFoundError", "OSError", "EngineError", "ValueError", "KeyError", "TypeError"}:
                    exception_type = "Exception"
                state.update(status="failed", error={"code": "worker_internal_error", "exception_type": exception_type}, updated_at=time.time(), finished_at=time.time())
                atomic_json(directory / "state.json", state)
                (directory / "prompt.private.txt").unlink(missing_ok=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
