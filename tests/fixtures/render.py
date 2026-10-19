"""Offline render stand-in; deliberately never generates real media."""

import argparse
from pathlib import Path
import time

parser = argparse.ArgumentParser()
parser.add_argument("mode", choices=("fail", "slowfail", "hang", "empty", "fake"))
parser.add_argument("project")
parser.add_argument("output")
parser.add_argument("--trace")
args = parser.parse_args()
if args.trace:
    with Path(args.trace).open("a", encoding="utf-8") as stream:
        stream.write(f"start {time.time()} {args.project}\n")
if args.mode == "hang":
    while True:
        time.sleep(0.1)
if args.mode == "slowfail":
    time.sleep(0.8)
if args.mode == "empty":
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_bytes(b"")
if args.mode == "fake":
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_bytes(b"This is not an MP4.")
if args.trace:
    with Path(args.trace).open("a", encoding="utf-8") as stream:
        stream.write(f"end {time.time()} {args.project}\n")
raise SystemExit(19 if args.mode in ("fail", "slowfail") else 0)
