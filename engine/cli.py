"""Local lifecycle client. Start accepts a private prompt file, never prompt argv."""
import argparse
import json
import sys
from pathlib import Path
from .core import Engine, EngineError, checked_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True)
    sub = parser.add_subparsers(dest="action", required=True)
    start = sub.add_parser("start")
    start.add_argument("--prompt-file", required=True)
    start.add_argument("--model", default="claude-opus-5-5")
    start.add_argument("--timeout", type=int, default=900)
    start.add_argument("--max-turns", type=int, default=12)
    start.add_argument("--require-existing-claude-login", action="store_true")
    start.add_argument("--seed-job", action="append", default=[])
    for name in ("status", "result", "cancel", "render"):
        command = sub.add_parser(name)
        command.add_argument("job_id")
    args = parser.parse_args()
    try:
        engine = Engine.from_data_root(args.data_root)
        if args.action == "start":
            path = checked_path(args.prompt_file, must_exist=True, directory=False)
            if path.stat().st_size > 65536:
                raise EngineError("Prompt file exceeds 64 KiB")
            result = engine.start_job(path.read_text(encoding="utf-8"), model=args.model, wall_timeout=args.timeout, max_turns=args.max_turns, require_existing_claude_login=args.require_existing_claude_login, seed_job_ids=args.seed_job)
        else:
            method = "render_job" if args.action == "render" else args.action
            result = getattr(engine, method)(args.job_id)
        print(json.dumps(result, indent=2))
        return 0
    except (EngineError, OSError, ValueError):
        print(json.dumps({"error": "Local job request rejected; check configuration and request constraints."}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
