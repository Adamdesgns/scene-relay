"""Offline Claude stand-in. Never contacts a provider or launches a renderer."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time


parser = argparse.ArgumentParser()
parser.add_argument("mode", choices=("success", "fail", "raw", "oversize", "hang", "child", "slow", "quota", "billing", "access", "model"))
parser.add_argument("--marker")
parser.add_argument("--trace")
args, extra = parser.parse_known_args()
prompt = sys.stdin.read()
root = Path.cwd()
(root / "fixture-argv.json").write_text(json.dumps(sys.argv), encoding="utf-8")
if args.marker:
    Path(args.marker).write_text(str(os.getpid()), encoding="ascii")
if args.trace:
    with Path(args.trace).open("a", encoding="utf-8") as stream:
        stream.write(f"start {time.time()} {os.getpid()}\n")

if args.mode in ("success", "slow", "raw"):
    (root / "index.html").write_text("<!doctype html><title>Offline fixture</title>", encoding="utf-8")
    (root / "editable-scene.json").write_text('{"title":"Synthetic scene"}', encoding="utf-8")
if args.mode == "slow":
    time.sleep(0.8)
if args.mode == "fail":
    print("PRIVATE-STDERR-DO-NOT-EXPOSE " + prompt, file=sys.stderr, flush=True)
    raise SystemExit(17)
if args.mode in ("quota", "billing", "access", "model"):
    reason = {"quota": "Rate limit", "billing": "Insufficient credits", "access": "You do not have access", "model": "model_not_found"}[args.mode]
    print(json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True,
                      "result": reason + " PRIVATE-RESULT-DO-NOT-EXPOSE " + prompt}), flush=True)
    raise SystemExit(1)
if args.mode == "raw":
    print("PRIVATE-STDOUT-DO-NOT-EXPOSE " + prompt, flush=True)
    print("PRIVATE-STDERR-DO-NOT-EXPOSE " + prompt, file=sys.stderr, flush=True)
if args.mode == "oversize":
    sys.stdout.write("OVERSIZE-PRIVATE-PAYLOAD-" * 300000)
    sys.stdout.flush()
    sys.stderr.write("OVERSIZE-PRIVATE-STDERR-" * 300000)
    sys.stderr.flush()
if args.mode == "child":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], stdin=subprocess.DEVNULL)
    (root / "fixture-child.pid").write_text(str(child.pid), encoding="ascii")
if args.mode in ("hang", "child"):
    while True:
        time.sleep(0.1)

if args.trace:
    with Path(args.trace).open("a", encoding="utf-8") as stream:
        stream.write(f"end {time.time()} {os.getpid()}\n")
print(json.dumps({"type": "system", "subtype": "init", "model": "claude-opus-5-5", "session_id": "offline-fixture"}), flush=True)
print(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "PRIVATE-ASSISTANT-DO-NOT-EXPOSE " + prompt}]}}), flush=True)
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "num_turns": 1, "duration_ms": 3, "result": "PRIVATE-RESULT-DO-NOT-EXPOSE " + prompt,
                  "modelUsage": {"claude-opus-5-5": {"inputTokens": 1, "outputTokens": 1}}}), flush=True)
