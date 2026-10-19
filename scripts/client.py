"""Exercise the real stdio MCP server without registering it persistently."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def call_tool(name, arguments, data_root, seed_directory=None):
    command = [sys.executable, "-u", str(ROOT / "server/mcp_server.py"), "--data-root", str(Path(data_root).resolve())]
    if seed_directory:
        command += ["--seed-directory", str(Path(seed_directory).resolve())]
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "scene-relay-local-client", "version": "0.1.1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    ]
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    completed = subprocess.run(command, input="\n".join(json.dumps(x, ensure_ascii=False) for x in messages) + "\n", text=True, encoding="utf-8", capture_output=True, timeout=35, creationflags=flags)
    replies = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
    reply = next((r for r in replies if r.get("id") == 2), None)
    if reply is None:
        raise RuntimeError("MCP server did not return a result; no retry performed")
    if "error" in reply or reply.get("result", {}).get("isError"):
        message = reply.get("error", {}).get("message") or reply["result"]["content"][0]["text"]
        raise RuntimeError(message)
    return json.loads(reply["result"]["content"][0]["text"])


def main():
    p = argparse.ArgumentParser(description="Scene Relay MCP client; does not register a server")
    p.add_argument("tool", choices=["scene_relay_health", "start_video_job", "get_video_job", "get_video_result", "render_video_job", "cancel_video_job"])
    p.add_argument("--data-root", default=str(ROOT / ".data/jobs"))
    p.add_argument("--seed-directory")
    p.add_argument("--input", help="UTF-8 JSON argument file; prompts stay out of command arguments")
    p.add_argument("--job-id")
    a = p.parse_args()
    arguments = json.loads(Path(a.input).read_text(encoding="utf-8")) if a.input else ({"job_id": a.job_id} if a.job_id else {})
    try:
        print(json.dumps(call_tool(a.tool, arguments, a.data_root, a.seed_directory), indent=2))
    except (RuntimeError, subprocess.TimeoutExpired):
        # Full requests and raw stderr may contain private data. Do not echo them.
        print(json.dumps({"ok": False, "error": "MCP operation failed. Check managed status before retrying."}))
        sys.exit(1)


if __name__ == "__main__":
    main()
