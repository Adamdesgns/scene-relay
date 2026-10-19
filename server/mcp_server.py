"""Dependency-free, newline-delimited JSON-RPC MCP transport. No model on startup.

Scene Relay by @adamdesgns. Only the engine creates/controls managed jobs.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PROTOCOLS = {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}
MAX_MESSAGE = 256 * 1024
JOB = {"type": "string", "description": "Scene Relay job ID returned by start_video_job"}


def schema(properties=None, required=None):
    return {"type": "object", "properties": properties or {}, "required": required or [], "additionalProperties": False}


def tool(name, description, fields=None, required=None, read=False, destructive=False):
    return {"name": name, "description": description, "inputSchema": schema(fields, required),
            "annotations": {"readOnlyHint": read, "destructiveHint": destructive, "idempotentHint": read, "openWorldHint": name == "start_video_job"}}


TOOLS = [
    tool("scene_relay_health", "Check local prerequisites without sending a model prompt. Never returns credentials.", read=True),
    tool("start_video_job", "Start one authorized Claude authoring job in a managed folder; returns immediately. Consumes the user's authorized Claude access. No automatic retries, model/provider fallback or publishing.", {
        "prompt": {"type": "string", "minLength": 1, "maxLength": 65536},
        "model": {"type": "string", "default": "claude-opus-5-5"},
        "wall_timeout": {"type": "integer", "minimum": 60, "maximum": 1800, "default": 900},
        "max_turns": {"type": "integer", "minimum": 1, "maximum": 30, "default": 12},
        "require_existing_claude_login": {"type": "boolean", "default": False, "description": "Per-job constraint when the user authorized their existing personal login only. Does not change credentials or disable official alternatives globally."},
        "seed_job_ids": {"type": "array", "items": JOB, "maxItems": 2, "default": []}
    }, ["prompt"]),
    tool("get_video_job", "Read sanitized status and bounded progress for one managed job.", {"job_id": JOB}, ["job_id"], read=True),
    tool("get_video_result", "Get verified source/artifact metadata for one job. Authoring success is separate from rendered-video success.", {"job_id": JOB}, ["job_id"], read=True),
    tool("render_video_job", "After source review and render authorization, queue the controlled local render. One render at a time; verifies actual MP4 video/audio.", {"job_id": JOB}, ["job_id"]),
    tool("cancel_video_job", "Cancel only this Scene Relay job when requested or under an agreed limit; never target unrelated processes.", {"job_id": JOB}, ["job_id"], destructive=True)
]


def existing_path(name, candidates):
    override = os.environ.get(name)
    if override:
        return Path(override).resolve()
    for value in candidates:
        if value and Path(value).is_file():
            return Path(value).resolve()
    return None


def paths():
    home = Path.home()
    claude = existing_path("SCENE_RELAY_CLAUDE", [
        home / "AppData/Roaming/npm/node_modules/@anthropic-ai/claude-code/bin/claude.exe",
        home / ".local/bin/claude.exe", shutil.which("claude.exe"), shutil.which("claude")])
    hf = existing_path("SCENE_RELAY_HYPERFRAMES_CLI", [ROOT / "node_modules/hyperframes/bin/hyperframes.mjs"])
    if not hf:
        cache = home / "AppData/Local/npm-cache/_npx"
        choices = list(cache.glob("*/node_modules/hyperframes/bin/hyperframes.mjs")) if cache.is_dir() else []
        choices.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        hf = choices[0] if choices else None
    return {"claude": claude, "hyperframes": hf, "node": shutil.which("node"), "ffmpeg": shutil.which("ffmpeg"), "ffprobe": shutil.which("ffprobe")}


def health():
    resolved = paths()
    checks = {name: bool(path and Path(path).is_file()) for name, path in resolved.items()}
    result = {"product": "Scene Relay", "version": "0.1.1", "creator": "@adamdesgns", "python": sys.version.split()[0], "dependencies": checks,
              "ready": all(checks.values()), "model_request_sent": False,
              "note": "Authentication and billing-route checks occur before each author job. Existing login alone does not verify quota or extra-usage settings."}
    return result


def build_engine(data_root, seed_directory=None):
    from engine import Engine, Config
    resolved = paths()
    if not resolved["claude"]:
        raise ValueError("Official Claude Code executable not found; configure SCENE_RELAY_CLAUDE.")
    if resolved["hyperframes"]:
        os.environ["SCENE_RELAY_HYPERFRAMES_CLI"] = str(resolved["hyperframes"])
    config = Config(data_root=str(Path(data_root).resolve()), seed_directory=str(Path(seed_directory).resolve()) if seed_directory else None,
                          claude_path=str(resolved["claude"]),
                          render_command=(sys.executable, str(ROOT / "server/render.py"), "--project", "{project}", "--output", "{output}"),
                          ffprobe_path=resolved["ffprobe"] or "ffprobe")
    return Engine(config)


def validate_args(name, arguments):
    spec = next((item for item in TOOLS if item["name"] == name), None)
    if spec is None:
        raise ValueError("Unknown tool")
    if not isinstance(arguments, dict):
        raise ValueError("Arguments must be an object")
    allowed = spec["inputSchema"]["properties"]
    if any(key not in allowed for key in arguments):
        raise ValueError("Unknown argument")
    if any(key not in arguments for key in spec["inputSchema"]["required"]):
        raise ValueError("Missing required argument")
    for key, value in arguments.items():
        kind = allowed[key]["type"]
        valid = ((kind == "string" and isinstance(value, str)) or (kind == "boolean" and isinstance(value, bool)) or
                 (kind == "integer" and type(value) is int) or (kind == "array" and isinstance(value, list) and all(isinstance(x, str) for x in value)))
        if not valid:
            raise ValueError("Invalid argument type")
    return arguments


class Service:
    def __init__(self, data_root, seed_directory=None):
        self.data_root, self.seed_directory, self.engine = data_root, seed_directory, None

    def call(self, name, arguments):
        args = validate_args(name, arguments)
        if name == "scene_relay_health":
            return health()
        if self.engine is None:
            if name != "start_video_job" and (Path(self.data_root) / "config.private.json").is_file():
                from engine import Engine
                self.engine = Engine.from_data_root(str(Path(self.data_root).resolve()))
            else:
                self.engine = build_engine(self.data_root, self.seed_directory)
        if name == "start_video_job":
            return self.engine.start_job(**args)
        methods = {"get_video_job": "status", "get_video_result": "result", "render_video_job": "render_job", "cancel_video_job": "cancel"}
        return getattr(self.engine, methods[name])(args["job_id"])

    def dispatch(self, request):
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}}
        method, rid, params = request.get("method"), request.get("id"), request.get("params", {})
        if rid is None:
            return None
        if method == "initialize":
            version = params.get("protocolVersion", "2025-03-26")
            payload = {"protocolVersion": version if version in PROTOCOLS else "2025-03-26", "capabilities": {"tools": {}},
                       "serverInfo": {"name": "scene-relay", "version": "0.1.1"},
                       "instructions": "Scene Relay by @adamdesgns. Use health, then start only an authorized video job. Poll its ID; inspect source before authorized render. No automatic retry/provider fallback. Cancel only its own requested job. Authentication stays in the official Claude CLI."}
        elif method == "ping":
            payload = {}
        elif method == "tools/list":
            payload = {"tools": TOOLS}
        elif method == "tools/call":
            try:
                value = self.call(params.get("name"), params.get("arguments", {}))
                payload = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}], "isError": False}
            except (ValueError, FileNotFoundError) as exc:
                # No prompt, argument dump, environment values, or traceback.
                payload = {"content": [{"type": "text", "text": str(exc)[:500]}], "isError": True}
            except Exception:
                payload = {"content": [{"type": "text", "text": "Local operation failed. Inspect the managed job status; no retry or fallback was performed."}], "isError": True}
        else:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "Method not found"}}
        return {"jsonrpc": "2.0", "id": rid, "result": payload}


def main():
    parser = argparse.ArgumentParser(description="Scene Relay local stdio MCP server · @adamdesgns")
    parser.add_argument("--data-root", default=str(ROOT / ".data/jobs"))
    parser.add_argument("--seed-directory", default=os.environ.get("SCENE_RELAY_SEED_DIRECTORY"))
    args = parser.parse_args()
    if "${" in args.data_root:
        parser.error("Unexpanded plugin data path; launch through a supported local host or pass --data-root.")
    service = Service(args.data_root, args.seed_directory)
    while True:
        raw = sys.stdin.buffer.readline(MAX_MESSAGE + 1)
        if not raw:
            break
        if len(raw) > MAX_MESSAGE:
            while raw and not raw.endswith(b"\n"):
                raw = sys.stdin.buffer.readline(MAX_MESSAGE + 1)
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Message exceeds transport limit"}}
        else:
            try:
                reply = service.dispatch(json.loads(raw))
            except (ValueError, UnicodeDecodeError, TypeError, AttributeError, RecursionError):
                reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid JSON-RPC message"}}
        if reply is not None:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
