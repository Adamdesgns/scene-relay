from __future__ import annotations

import contextlib
import ctypes
import dataclasses
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

GIB = 1024 ** 3
TERMINAL = {"authored", "completed", "failed", "cancelled"}
UNKNOWN_PROCESS = "unavailable"
MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:\[\]-]{0,199}\Z")
# Keep subprocess-facing paths compatible with ordinary Win32 tools. This is a
# bounded contract, independent of the machine's long-path policy or Python's
# own support. The root budget leaves 139 UTF-16 units for job/workspace paths.
WINDOWS_FILE_PATH_LIMIT = 259
WINDOWS_DIRECTORY_PATH_LIMIT = 247
WINDOWS_DATA_ROOT_LIMIT = 120
WINDOWS_DATA_ROOT_GUIDANCE = (
    "Windows data root exceeds 120 UTF-16 code units; choose a shorter writable "
    "--data-root, for example C:/Users/<you>/sr-data. The install folder may stay in place."
)


class EngineError(ValueError):
    """Safe, fixed-message validation failure."""


@dataclasses.dataclass(frozen=True)
class Config:
    data_root: str
    claude_path: str = ""
    seed_directory: str | None = None
    render_command: tuple[str, ...] = ()
    ffprobe_path: str | None = None
    min_free_disk_bytes: int = GIB
    min_free_memory_bytes: int = GIB
    expected_width: int = 1920
    expected_height: int = 1080
    expected_duration: float | None = None
    test_mode: bool = False
    test_author_command: tuple[str, ...] = ()
    test_auth_status: dict | None = None
    test_effective_auth_status: dict | None = None
    test_timeout_seconds: float | None = None

    def validate(self):
        checked_data_root(self.data_root)
        if not self.test_mode and (self.test_author_command or self.test_auth_status is not None or self.test_effective_auth_status is not None or self.test_timeout_seconds is not None):
            raise EngineError("Test injection requires test_mode")
        if not self.test_mode and (self.min_free_disk_bytes < GIB or self.min_free_memory_bytes < GIB):
            raise EngineError("Production resource thresholds cannot be lowered")
        if self.test_mode and self.test_timeout_seconds is not None and not 0.1 <= self.test_timeout_seconds <= 1800:
            raise EngineError("Invalid test timeout")
        if self.expected_width < 1 or self.expected_height < 1:
            raise EngineError("Invalid output dimensions")
        if self.expected_duration is not None and not 0 < self.expected_duration <= 600:
            raise EngineError("Invalid expected duration")
        if not self.test_mode:
            checked_path(self.claude_path, directory=False)
        if self.seed_directory:
            checked_path(self.seed_directory, directory=True)
        if self.render_command:
            checked_path(self.render_command[0], directory=False)
        if self.ffprobe_path:
            checked_path(self.ffprobe_path, directory=False)


def is_reparse(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def windows_path_units(path: Path) -> int:
    return len(str(path).encode("utf-16-le", errors="surrogatepass")) // 2


def check_windows_path_budget(path: Path, *, directory=None):
    if os.name != "nt":
        return
    if str(path).startswith(("\\\\?\\", "\\\\.\\")):
        raise EngineError("Windows device and extended-length paths are not permitted")
    limit = WINDOWS_DIRECTORY_PATH_LIMIT if directory else WINDOWS_FILE_PATH_LIMIT
    if windows_path_units(path) > limit:
        raise EngineError(
            "Windows path exceeds the supported path budget; use a shorter "
            "--data-root or shorter project paths"
        )


def checked_path(value, *, must_exist=False, directory=None) -> Path:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise EngineError("Absolute non-traversing path required")
    check_windows_path_budget(path, directory=directory)
    for candidate in [*reversed(path.parents), path]:
        if candidate.exists() and is_reparse(candidate):
            raise EngineError("Links and reparse points are not permitted")
        if candidate.is_symlink():
            raise EngineError("Links and reparse points are not permitted")
    if must_exist and not path.exists():
        raise EngineError("Required local path does not exist")
    if path.exists() and directory is not None and path.is_dir() != directory:
        raise EngineError("Unexpected local path type")
    resolved = path.resolve()
    check_windows_path_budget(resolved, directory=directory)
    return resolved


def checked_data_root(value, *, must_exist=False) -> Path:
    # Reject before any mkdir, lock, config, prompt, or worker is created. Check
    # the lexical spelling too, so a very long missing path gets useful advice.
    path = Path(value)
    if os.name == "nt" and windows_path_units(path) > WINDOWS_DATA_ROOT_LIMIT:
        raise EngineError(WINDOWS_DATA_ROOT_GUIDANCE)
    path = checked_path(path, must_exist=must_exist, directory=True)
    if os.name == "nt" and windows_path_units(path) > WINDOWS_DATA_ROOT_LIMIT:
        raise EngineError(WINDOWS_DATA_ROOT_GUIDANCE)
    return path


def atomic_json(path: Path, value: dict):
    checked_path(path)
    checked_path(path.parent, must_exist=True, directory=True)
    # Eleven-character sibling names fit even a 247-unit parent directory.
    # Exclusive creation plus bounded retries handles collisions without ever
    # deleting or overwriting another writer's temporary file.
    for _ in range(16):
        temp = path.with_name("." + uuid.uuid4().hex[:10])
        check_windows_path_budget(temp)
        try:
            handle = temp.open("x", encoding="utf-8")
            break
        except FileExistsError:
            continue
    else:
        raise EngineError("Atomic state temporary file is busy")
    try:
        with handle:
            json.dump(value, handle, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        # Windows readers can briefly hold a handle without delete sharing.
        # Publish atomically after that reader closes; never truncate live state.
        deadline = time.monotonic() + 2
        while True:
            try:
                os.replace(temp, path)
                break
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
    finally:
        temp.unlink(missing_ok=True)


def read_json(path: Path) -> dict:
    checked_path(path, must_exist=True, directory=False)
    if path.stat().st_size > 1024 * 1024:
        raise EngineError("Oversized state file")
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise EngineError("Invalid state file")
    return value


class FileLock:
    """An OS-held lock releases automatically if the holder crashes."""
    def __init__(self, path: Path):
        checked_path(path)
        self.handle = path.open("a+b")
        self.handle.seek(0, 2)
        if self.handle.tell() == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.locked = False

    def acquire(self, blocking=False, timeout=None):
        deadline = time.monotonic() + timeout if timeout is not None else None
        while True:
            try:
                self.handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.locked = True
                return True
            except (BlockingIOError, OSError):
                if not blocking or (deadline is not None and time.monotonic() >= deadline):
                    return False
                time.sleep(0.05)

    def close(self):
        if self.locked:
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()

    def __enter__(self):
        if not self.acquire():
            self.close()
            raise EngineError("Job operation already in progress")
        return self

    def __exit__(self, *_):
        self.close()


def process_identity(pid: int) -> str | None:
    """Creation identity avoids treating a reused PID as the original worker."""
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        kernel.GetProcessTimes.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x1000, False, int(pid))
        if not handle:
            # Access-denied observation from a more constrained host does not
            # establish that its worker exited. Only known missing-PID errors do.
            return None if ctypes.get_last_error() in {87, 1168} else UNKNOWN_PROCESS
        try:
            code = ctypes.c_uint32()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                return UNKNOWN_PROCESS
            if code.value != 259:
                return None
            times = [ctypes.c_uint64() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                return UNKNOWN_PROCESS
            return str(times[0].value)
        finally:
            kernel.CloseHandle(handle)
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
        rest = text[text.rfind(")") + 2:].split()
        return None if rest[0] == "Z" else rest[19]
    except (OSError, IndexError):
        try:
            os.kill(int(pid), 0)
            return str(pid)
        except PermissionError:
            return UNKNOWN_PROCESS
        except OSError:
            return None


def worker_is_dead(state: dict) -> bool:
    pid, identity = state.get("worker_pid"), state.get("worker_identity")
    if pid is None:
        return time.time() - state["updated_at"] > 15
    observed = process_identity(pid)
    if observed is None:
        return True
    if observed == UNKNOWN_PROCESS or identity in {None, UNKNOWN_PROCESS}:
        return False
    return observed != identity


def free_memory() -> int:
    if os.name == "nt":
        class Memory(ctypes.Structure):
            _fields_ = [("length", ctypes.c_uint32), ("load", ctypes.c_uint32)] + [(name, ctypes.c_uint64) for name in ("total", "available", "page_total", "page_available", "virtual_total", "virtual_available", "extended")]
        value = Memory()
        value.length = ctypes.sizeof(value)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(value)):
            raise EngineError("Cannot determine available memory")
        return int(value.available)
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except OSError:
        pass
    return os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")


def resource_error(config: Config, root: Path) -> str | None:
    if shutil.disk_usage(root).free < config.min_free_disk_bytes:
        return "insufficient_disk"
    if free_memory() < config.min_free_memory_bytes:
        return "insufficient_memory"
    return None


def copy_seed(source: Path, destination: Path):
    """Only regular bounded files. Never follows links, junctions or devices."""
    checked_path(source, must_exist=True, directory=True)
    count, total = 0, 0
    generated_dirs = {".git", "node_modules", "output", ".cache", ".claude", ".hyperframes", "renders"}
    for base, dirs, files in os.walk(source, followlinks=False):
        dirs[:] = [d for d in dirs if d.casefold() not in generated_dirs]
        for name in dirs + files:
            if is_reparse(Path(base) / name):
                raise EngineError("Seed contains a link or reparse point")
        relative = Path(base).relative_to(source)
        target = destination / relative
        checked_path(target)
        target.mkdir(parents=True, exist_ok=True)
        for name in files:
            source_file = Path(base) / name
            if not stat.S_ISREG(source_file.stat().st_mode):
                raise EngineError("Seed contains a special file")
            count += 1
            total += source_file.stat().st_size
            if count > 2000 or total > 200 * 1024 * 1024:
                raise EngineError("Seed exceeds file or size limits")
            target_file = target / name
            checked_path(target_file)
            shutil.copyfile(source_file, target_file)


def public_state(state: dict, directory: Path) -> dict:
    result = {k: v for k, v in state.items() if k not in {"worker_identity"}}
    result["project_dir"] = str(directory / "project")
    if state["status"] == "completed":
        result["output_path"] = str(directory / "project" / "output" / "final.mp4")
    return result


class Engine:
    def __init__(self, config: Config):
        config.validate()
        root = checked_data_root(config.data_root)
        root.mkdir(parents=True, exist_ok=True)
        self.root = checked_path(root, must_exist=True, directory=True)
        self.config = config
        config_path = self.root / "config.private.json"
        config_lock = FileLock(self.root / "config.lock")
        deadline = time.monotonic() + 5
        while not config_lock.acquire():
            if time.monotonic() > deadline:
                config_lock.close()
                raise EngineError("Configuration is busy")
            time.sleep(0.02)
        try:
            wanted = json.loads(json.dumps(dataclasses.asdict(config)))
            if config_path.exists():
                stored = Config(**read_json(config_path))
                normalized = json.loads(json.dumps(dataclasses.asdict(stored)))
                if normalized != wanted:
                    raise EngineError("Data root already belongs to a different configuration")
            if not config_path.exists():
                atomic_json(config_path, wanted)
        finally:
            config_lock.close()
        (self.root / "jobs").mkdir(exist_ok=True)
        checked_path(self.root / "jobs", directory=True)

    @classmethod
    def from_data_root(cls, data_root: str):
        root = checked_data_root(data_root, must_exist=True)
        return cls(Config(**read_json(root / "config.private.json")))

    def _directory(self, job_id: str):
        if not isinstance(job_id, str):
            raise EngineError("Invalid job ID")
        try:
            parsed = uuid.UUID(job_id)
        except (ValueError, AttributeError):
            raise EngineError("Invalid job ID") from None
        if parsed.version != 4 or str(parsed) != job_id:
            raise EngineError("Invalid job ID")
        return checked_path(self.root / "jobs" / job_id, must_exist=True, directory=True)

    def start_job(self, prompt: str, *, model="claude-opus-5-5", wall_timeout=900, max_turns=12, require_existing_claude_login=False, seed_job_ids=()):
        if not self.config.test_mode:
            checked_path(self.config.claude_path, must_exist=True, directory=False)
        if self.config.seed_directory:
            checked_path(self.config.seed_directory, must_exist=True, directory=True)
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode("utf-8")) > 65536 or "\x00" in prompt:
            raise EngineError("Prompt must be nonempty UTF-8 text of at most 64 KiB")
        if not isinstance(model, str) or not MODEL_RE.fullmatch(model):
            raise EngineError("Invalid model identifier")
        if isinstance(wall_timeout, bool) or not isinstance(wall_timeout, (int, float)) or not math.isfinite(wall_timeout) or not 60 <= wall_timeout <= 1800:
            raise EngineError("Wall timeout must be 60 to 1800 seconds")
        if type(max_turns) is not int or not 1 <= max_turns <= 30:
            raise EngineError("Max turns must be 1 to 30")
        if type(require_existing_claude_login) is not bool:
            raise EngineError("Authentication policy must be boolean")
        if not isinstance(seed_job_ids, (list, tuple)) or len(seed_job_ids) > 2:
            raise EngineError("At most two revision references are permitted")
        seeds = []
        for reference in seed_job_ids:
            prior = self.status(reference)
            author_result = prior.get("author_result")
            author_finished = prior.get("author_finished_at")
            retained_author_success = (
                prior["status"] in {"failed", "cancelled"}
                and prior.get("phase") == "render"
                and isinstance(author_result, dict)
                and author_result.get("subtype") == "success"
                and author_result.get("is_error") is False
                and type(author_finished) in (int, float)
                and math.isfinite(author_finished)
                and author_finished > 0
            )
            if prior["status"] not in {"authored", "completed"} and not retained_author_success:
                raise EngineError("Revision source is not complete")
            seeds.append(checked_path(self._directory(reference) / "project", must_exist=True, directory=True))
        job_id = str(uuid.uuid4())
        directory = self.root / "jobs" / job_id
        directory.mkdir()
        project = directory / "project"
        project.mkdir()
        try:
            if self.config.seed_directory:
                copy_seed(Path(self.config.seed_directory), project)
            for seed in seeds:
                copy_seed(seed, project)
            prompt_path = directory / "prompt.private.txt"
            with prompt_path.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(prompt)
            try:
                os.chmod(prompt_path, 0o600)
            except OSError:
                pass
            atomic_json(directory / "empty-mcp.json", {"mcpServers": {}})
            state = {"job_id": job_id, "status": "queued", "phase": "author", "created_at": time.time(), "updated_at": time.time(), "model": model, "wall_timeout": wall_timeout, "max_turns": max_turns, "require_existing_claude_login": require_existing_claude_login, "revision_of": list(seed_job_ids), "events": []}
            atomic_json(directory / "state.json", state)
            with FileLock(directory / "operation.lock"):
                self._spawn(job_id)
            return self.status(job_id)
        except Exception:
            (directory / "prompt.private.txt").unlink(missing_ok=True)
            if (directory / "state.json").exists():
                state = read_json(directory / "state.json")
                state.update(status="failed", error={"code": "worker_launch_failed"})
                atomic_json(directory / "state.json", state)
            raise

    def _spawn(self, job_id):
        kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "close_fds": True, "cwd": str(Path(__file__).resolve().parent.parent)}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        process = subprocess.Popen([sys.executable, "-m", "engine.worker", "--data-root", str(self.root), "--job-id", job_id], **kwargs)
        # The caller holds operation.lock. Publish process identity before the
        # worker takes the same lock to publish startup, avoiding a stale queued
        # observation racing a newly initialized worker.
        try:
            state_path = self._directory(job_id) / "state.json"
            state = read_json(state_path)
            state.update(worker_pid=process.pid, worker_identity=process_identity(process.pid), updated_at=time.time())
            atomic_json(state_path, state)
        except Exception:
            # Worker is still waiting on this caller's operation lock, so it
            # cannot have launched authoring when publication fails.
            process.kill()
            process.wait(timeout=5)
            raise
        # Detach intentionally: the OS-held worker lock and identity supervise lifecycle.
        if os.name == "nt":
            process._handle.Close()
        process.returncode = 0

    def status(self, job_id):
        directory = self._directory(job_id)
        state = read_json(directory / "state.json")
        if state["status"] not in TERMINAL:
            dead = worker_is_dead(state)
            if dead:
                operation = FileLock(directory / "operation.lock")
                try:
                    if not operation.acquire(blocking=True, timeout=2):
                        return public_state(read_json(directory / "state.json"), directory)
                    state = read_json(directory / "state.json")
                    still_dead = worker_is_dead(state)
                    if state["status"] not in TERMINAL and still_dead:
                        state.update(status="failed", error={"code": "worker_exited"}, updated_at=time.time())
                        atomic_json(directory / "state.json", state)
                        (directory / "prompt.private.txt").unlink(missing_ok=True)
                finally:
                    operation.close()
        return public_state(state, directory)

    def result(self, job_id):
        result = self.status(job_id)
        if result["status"] in {"authored", "completed"}:
            project = self._directory(job_id) / "project"
            artifacts = []
            for base, dirs, files in os.walk(project, followlinks=False):
                for name in dirs + files:
                    if is_reparse(Path(base) / name):
                        raise EngineError("Project contains a link or reparse point")
                dirs[:] = [d for d in dirs if d not in {"node_modules", ".cache", ".git"}]
                for name in files:
                    file = Path(base) / name
                    if not file.is_file():
                        continue
                    artifacts.append({"path": file.relative_to(project).as_posix(), "bytes": file.stat().st_size})
                    if len(artifacts) > 2000:
                        raise EngineError("Artifact inventory limit exceeded")
            result["artifacts"] = artifacts
        return result

    def cancel(self, job_id):
        directory = self._directory(job_id)
        state = self.status(job_id)
        if state["status"] not in TERMINAL:
            checked_path(directory / "cancel.requested")
            (directory / "cancel.requested").touch(exist_ok=True)
        return self.status(job_id)

    def render_job(self, job_id):
        directory = self._directory(job_id)
        if not self.config.render_command or not self.config.ffprobe_path:
            raise EngineError("Controlled renderer and ffprobe must be configured")
        checked_path(self.config.render_command[0], must_exist=True, directory=False)
        checked_path(self.config.ffprobe_path, must_exist=True, directory=False)
        with FileLock(directory / "operation.lock"):
            state = read_json(directory / "state.json")
            if state["status"] != "authored":
                raise EngineError("Job must be authored before rendering")
            # Validate every project entry before any renderer may execute authored code.
            self.result(job_id)
            state.update(status="queued", phase="render", updated_at=time.time())
            state.pop("worker_pid", None)
            state.pop("worker_identity", None)
            state.pop("error", None)
            (directory / "cancel.requested").unlink(missing_ok=True)
            atomic_json(directory / "state.json", state)
            self._spawn(job_id)
        return self.status(job_id)
