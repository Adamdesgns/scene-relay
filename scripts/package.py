"""Build a source-only Scene Relay archive. Does not run jobs or install anything."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = {"plugin.json", "mcp.json", ".mcp.json", "README.md", "LICENSE", "docs/preview-template.html"}
MANIFEST = ".codex-plugin/plugin.json"
ALLOWED_SUFFIXES = {
    "server": {".py"}, "engine": {".py"}, "scripts": {".py"},
    "tests": {".py"}, "docs": {".md"}, "skills": {".md"},
}
EXCLUDED_DIRECTORIES = {
    "runtime", ".runtime", ".data", "data", "jobs", "__pycache__", "dist",
    "production", "assets", "output", "outputs", "out", "renders", "node_modules",
    ".git", ".cache", ".venv", "venv", ".claude", ".hyperframes",
}
REQUIRED_FILES = ROOT_FILES | {MANIFEST, "server/mcp_server.py", "engine/__init__.py", "skills/scene-relay/SKILL.md"}
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_FILES = 2000
ZIP_TIMESTAMP = (2026, 1, 1, 0, 0, 0)


class PackageError(ValueError):
    """A fixed, safe packaging diagnostic."""


def is_link_or_reparse(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def ensure_plain_path(path: Path) -> None:
    for candidate in reversed((path, *path.parents)):
        if (candidate.exists() or candidate.is_symlink()) and is_link_or_reparse(candidate):
            raise PackageError("Links and reparse points are not permitted")


def private_name(name: str) -> bool:
    lowered = name.casefold()
    return (
        lowered.startswith(".") or "private" in lowered or "credential" in lowered
        or "secret" in lowered or lowered.endswith((".log", ".lock", ".tmp", ".pyc"))
    )


def collect_sources(root: Path) -> list[tuple[str, bytes]]:
    root = Path(os.path.abspath(root))
    ensure_plain_path(root)
    if not root.is_dir():
        raise PackageError("Source root is missing")
    selected: list[tuple[str, bytes]] = []
    total = 0

    def include(path: Path):
        nonlocal total
        ensure_plain_path(path)
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
            raise PackageError("Source file is not a bounded regular file")
        if len(selected) >= MAX_FILES or total + info.st_size > MAX_TOTAL_BYTES:
            raise PackageError("Source package exceeds limits")
        with path.open("rb") as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
        after = path.stat()
        if len(data) != info.st_size or (info.st_mtime_ns, info.st_size) != (after.st_mtime_ns, after.st_size):
            raise PackageError("Source changed while packaging; retry after edits finish")
        total += len(data)
        selected.append((path.relative_to(root).as_posix(), data))

    for name in sorted(ROOT_FILES | {MANIFEST}):
        path = root / name
        if path.exists() or path.is_symlink():
            include(path)
    for dirname, suffixes in ALLOWED_SUFFIXES.items():
        folder = root / dirname
        if not folder.exists() and not folder.is_symlink():
            continue
        ensure_plain_path(folder)
        for base, directories, files in os.walk(folder, followlinks=False):
            directories[:] = sorted(
                name for name in directories
                if name.casefold() not in EXCLUDED_DIRECTORIES and not private_name(name)
            )
            for name in directories:
                ensure_plain_path(Path(base) / name)
            for name in sorted(files):
                if private_name(name) or Path(name).suffix.casefold() not in suffixes:
                    continue
                include(Path(base) / name)
    names = {name for name, _ in selected}
    if not REQUIRED_FILES <= names:
        raise PackageError("Required source or manifest files are missing")
    return sorted(selected)


def build_package(root: Path = ROOT, output: Path | None = None) -> dict:
    root = Path(os.path.abspath(root))
    output = Path(os.path.abspath(output or root / "dist/source-plugin.zip"))
    destination = root / "dist"
    try:
        output.relative_to(destination)
    except ValueError:
        raise PackageError("Package output must be inside the project's dist directory") from None
    if output.suffix.casefold() != ".zip":
        raise PackageError("Package output must be a ZIP file")
    ensure_plain_path(output)
    sources = collect_sources(root)
    output.parent.mkdir(parents=True, exist_ok=True)
    ensure_plain_path(output.parent)
    temporary = output.with_name(output.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as handle:
            with zipfile.ZipFile(handle, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
                for name, data in sources:
                    entry = zipfile.ZipInfo(name, ZIP_TIMESTAMP)
                    entry.create_system = 3
                    entry.external_attr = (stat.S_IFREG | 0o644) << 16
                    entry.compress_type = zipfile.ZIP_DEFLATED
                    archive.writestr(entry, data, compresslevel=9)
            handle.flush()
            os.fsync(handle.fileno())
        ensure_plain_path(output)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "package": str(output), "files": len(sources), "bytes": output.stat().st_size,
        "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "contents": [name for name, _ in sources],
        "scope": "source_only", "installs_or_jobs_started": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Optional ZIP path inside this project's dist directory")
    args = parser.parse_args()
    try:
        print(json.dumps(build_package(output=args.output), indent=2))
        return 0
    except (PackageError, OSError):
        print(json.dumps({"ok": False, "error": "Source packaging failed; inspect source/path constraints. No install or job was started."}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
