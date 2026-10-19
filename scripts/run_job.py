"""Keep a local execution host alive while exercising an asynchronous MCP job.

No registration, implicit retry or fallback. Useful when a sandbox closes all
descendants at the end of a shell command. Does not bypass that boundary.
"""
import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.client import call_tool


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=["author", "render"])
    p.add_argument("--data-root", required=True)
    p.add_argument("--seed-directory", required=True)
    p.add_argument("--input")
    p.add_argument("--job-id")
    p.add_argument("--record", required=True, help="Sanitized result JSON destination")
    a = p.parse_args()
    tool = "start_video_job" if a.action == "author" else "render_video_job"
    arguments = json.loads(Path(a.input).read_text(encoding="utf-8")) if a.action == "author" else {"job_id": a.job_id}
    value = call_tool(tool, arguments, a.data_root, a.seed_directory)
    record = Path(a.record)
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps(value, indent=2), encoding="utf-8")
    job_id = value["job_id"]
    print(json.dumps({"job_id": job_id, "status": value["status"]}), flush=True)
    deadline = time.monotonic() + 2 * value.get("wall_timeout", 900) + 120
    previous = None
    while time.monotonic() < deadline:
        value = call_tool("get_video_job", {"job_id": job_id}, a.data_root, a.seed_directory)
        signature = (value["status"], value.get("phase"), len(value.get("events", [])), value.get("events", [])[-1:] )
        if signature != previous:
            print(json.dumps({key: value[key] for key in ("job_id", "status", "phase", "authentication", "observed_model", "error") if key in value} | {"last_event": value.get("events", [])[-1:]}), flush=True)
            previous = signature
        record.write_text(json.dumps(value, indent=2), encoding="utf-8")
        if value["status"] in {"authored", "completed", "failed", "cancelled"}:
            return 0 if value["status"] in {"authored", "completed"} else 1
        time.sleep(3)
    print(json.dumps({"job_id": job_id, "error": "monitor_deadline_reached", "note": "Inspect status before any new request; no duplicate submission performed."}), flush=True)
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print(json.dumps({"error": str(error)[:500]}), flush=True)
        raise SystemExit(1)
