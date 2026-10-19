"""Internal helper. Establish confinement before launching any job command."""
import json
import os
import subprocess
import sys
from pathlib import Path
from .core import checked_path, read_json
from .supervisor import OwnedJob


def main():
    try:
        owned = OwnedJob()
        spec = read_json(checked_path(sys.argv[1], must_exist=True, directory=False))
        cwd = checked_path(spec["cwd"], must_exist=True, directory=True)
        command = spec["command"]
        checked_path(command[0], must_exist=True, directory=False)
        child = subprocess.Popen(command, cwd=str(cwd), stdin=sys.stdin.buffer, stdout=sys.stdout.buffer, stderr=sys.stderr.buffer, shell=False, close_fds=True)
        return child.wait()
    except Exception:
        # Do not forward exception text: command paths or input might be private.
        return 125


if __name__ == "__main__":
    sys.exit(main())
