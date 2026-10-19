"""Bounded, local-only HyperFrames adapter. No npm commands or project scripts.

This is a conservative source gate, not an OS sandbox for hostile JavaScript.
The enclosing job supervisor must contain/cancel this entire process tree.
"""
from __future__ import annotations

import argparse
import hashlib
from html.parser import HTMLParser
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from urllib.parse import unquote, urlsplit

VERSION = "0.8.169"
MAX_FILES = 3000
MAX_BYTES = 512 * 1024 * 1024
MAX_CAPTURE = 4 * 1024 * 1024
MAX_DURATION = 180
TEXT_EXTENSIONS = {".html", ".htm", ".js", ".mjs", ".css", ".svg"}
EXCLUDED_DIRS = {"output", "renders", ".hyperframes", ".git", "node_modules"}
NETWORK_JS = re.compile(
    r"\b(?:fetch|XMLHttpRequest|WebSocket|EventSource|WebTransport|importScripts|sendBeacon)\b"
    r"|\b(?:eval|Function|require)\s*\(|\bimport\b\s*(?:\(|[\w*{])"
    r"|\b(?:child_process|Deno|Bun|process\.env)\b"
    r"|\b(?:window\s*\.\s*open|location\s*\.\s*(?:assign|replace))\s*\("
    r"|\b(?:window\s*\.\s*)?location(?:\s*\.\s*href)?\s*=",
)
CSS_URL = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.I | re.S)
CSS_IMPORT = re.compile(r"@import\s+(?:url\(\s*)?(['\"]?)([^'\"\s;)]+)\1", re.I)


class RenderError(Exception):
    def __init__(self, code: str, detail: str = "", cleanup: dict | None = None):
        self.code, self.detail, self.cleanup = code, detail, cleanup
        super().__init__(code)


def safe_token(value: object, limit: int = 80) -> str:
    return re.sub(r"[^a-zA-Z0-9_.:# -]", "?", str(value))[:limit]


def ensure_no_links(path: Path) -> None:
    """Inspect lexical ancestors too: resolve() alone hides linked ancestors."""
    absolute = Path(os.path.abspath(path))
    for candidate in reversed((absolute, *absolute.parents)):
        if not candidate.exists() and not candidate.is_symlink():
            continue
        info = candidate.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise RenderError("linked_path_rejected")


def inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def local_url(value: str, source: Path, project: Path) -> Path | None:
    value = value.strip()
    if not value or value.startswith("#"):
        return
    if re.match(r"data:(?:image/(?:png|jpeg|webp|gif)|font/(?:woff2?|ttf));base64,", value, re.I):
        return
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or value.startswith(("/", "\\")) or "\\" in value:
        raise RenderError("external_resource_rejected", source.name)
    decoded = unquote(parsed.path)
    if "\x00" in decoded or ":" in decoded:
        raise RenderError("external_resource_rejected", source.name)
    target = source.parent / decoded
    if not inside(target, project):
        raise RenderError("resource_outside_project", source.name)
    if any(part.lower() in EXCLUDED_DIRS for part in target.resolve().relative_to(project.resolve()).parts):
        raise RenderError("resource_in_excluded_directory", source.name)
    ensure_no_links(target)
    if not target.is_file():
        raise RenderError("missing_local_resource", source.name)
    return target


def require_resource_type(target: Path | None, extensions: set[str], role: str) -> None:
    # Every executable/stylesheet reference must enter the same inventory audit
    # used for local source files. Unknown extensions cannot evade that audit.
    if target is None or target.suffix.lower() not in extensions:
        raise RenderError(f"unsupported_{role}_extension")


def inspect_css(content: str, source: Path, project: Path) -> None:
    for match in CSS_URL.finditer(content):
        local_url(match.group(2), source, project)
    for match in CSS_IMPORT.finditer(content):
        target = local_url(match.group(2), source, project)
        require_resource_type(target, {".css"}, "stylesheet")


def inspect_script(content: str, source: Path) -> None:
    # Keep detection intentionally conservative: authored project code may not
    # perform network I/O, dynamic code loading, navigation or Node execution.
    if NETWORK_JS.search(content):
        raise RenderError("script_capability_rejected", source.name)
    # SVG namespace strings and library documentation URLs are not requests.
    # Reject external literals when assigned to a resource or CSS url instead.
    external_assignment = (
        r"(?:\b(?:src|href|srcset|poster)\s*[:=]\s*|"
        r"\bsetAttribute\s*\(\s*['\"](?:src|href|srcset|poster)['\"]\s*,\s*)"
        r"['\"`]\s*(?:https?:|wss?:|file:|//)"
        r"|url\(\s*['\"]?\s*(?:https?:|file:|//)"
    )
    if re.search(external_assignment, content, re.I):
        raise RenderError("external_script_url_rejected", source.name)


class SourceParser(HTMLParser):
    def __init__(self, source: Path, project: Path):
        super().__init__(convert_charrefs=True)
        self.source, self.project = source, project
        self.roots: list[dict[str, str]] = []
        self.mode = ""

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag in {"iframe", "frame", "object", "embed", "base", "form"}:
            raise RenderError("active_html_rejected", self.source.name)
        if tag == "meta" and values.get("http-equiv", "").lower() == "refresh":
            raise RenderError("navigation_rejected", self.source.name)
        if "data-composition-id" in values:
            self.roots.append(values)
        for key, value in attrs:
            if not value:
                continue
            if key.startswith("on") or key in {"srcdoc", "ping", "formaction"}:
                raise RenderError("active_html_rejected", self.source.name)
            if key in {"src", "poster", "data-composition-src", "xlink:href"} or (key == "href" and tag != "a"):
                # SVG uses the standard namespace as metadata, not a resource.
                target = local_url(value, self.source, self.project)
                if tag == "script" and key in {"src", "href", "xlink:href"}:
                    require_resource_type(target, {".js", ".mjs"}, "script")
                if tag == "link" and "stylesheet" in (values.get("rel") or "").lower().split():
                    require_resource_type(target, {".css"}, "stylesheet")
                if key == "data-composition-src":
                    require_resource_type(target, {".html", ".htm"}, "composition")
            if key == "srcset":
                for item in value.split(","):
                    local_url(item.strip().split()[0], self.source, self.project)
            if key == "style":
                inspect_css(value, self.source, self.project)
        if tag in {"script", "style"}:
            self.mode = tag

    def handle_endtag(self, tag):
        if tag == self.mode:
            self.mode = ""

    def handle_data(self, data):
        if self.mode == "script":
            inspect_script(data, self.source)
        elif self.mode == "style":
            inspect_css(data, self.source, self.project)


def validate_project(project: Path) -> dict:
    ensure_no_links(project)
    if not project.is_dir():
        raise RenderError("project_missing")
    entry, config_path = project / "index.html", project / "hyperframes.json"
    if not entry.is_file() or not config_path.is_file():
        raise RenderError("project_files_missing")
    try:
        config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError):
        raise RenderError("invalid_project_config") from None
    if not isinstance(config, dict):
        raise RenderError("invalid_project_config")
    if any(key.lower() in {"scripts", "hooks", "plugins", "prerender", "postrender"} for key in config):
        raise RenderError("executable_config_rejected")
    for value in config.get("paths", {}).values():
        if not isinstance(value, str) or not inside(project / value, project) or ":" in value:
            raise RenderError("config_path_outside_project")
    total, count, digests = 0, 0, {}
    root_attrs = None
    for directory, dirs, files in os.walk(project, followlinks=False):
        parent = Path(directory)
        for name in dirs + files:
            ensure_no_links(parent / name)
        dirs[:] = [name for name in dirs if name.lower() not in EXCLUDED_DIRS]
        for name in files:
            path = parent / name
            count += 1
            total += path.stat().st_size
            if count > MAX_FILES or total > MAX_BYTES:
                raise RenderError("project_size_limit")
            if path.suffix.lower() not in TEXT_EXTENSIONS:
                continue
            if path.stat().st_size > 8 * 1024 * 1024:
                raise RenderError("source_size_limit")
            content = path.read_text(encoding="utf-8-sig")
            digests[path.relative_to(project).as_posix()] = hashlib.sha256(content.encode()).hexdigest()
            if path.suffix.lower() in {".html", ".htm", ".svg"}:
                parser = SourceParser(path, project)
                parser.feed(content)
                if path == entry:
                    if not parser.roots:
                        raise RenderError("composition_root_missing")
                    root_attrs = parser.roots[0]
            elif path.suffix.lower() == ".css":
                inspect_css(content, path, project)
            else:
                inspect_script(content, path)
    try:
        duration = float(root_attrs["data-duration"])
        width = int(root_attrs.get("data-width", "1920"))
        height = int(root_attrs.get("data-height", "1080"))
    except (KeyError, TypeError, ValueError):
        raise RenderError("invalid_composition_dimensions") from None
    if not math.isfinite(duration) or not 0 < duration <= MAX_DURATION:
        raise RenderError("duration_limit")
    if not 320 <= width <= 3840 or not 180 <= height <= 3840 or width * height > 3840 * 2160 or width % 2 or height % 2:
        raise RenderError("dimension_limit")
    return {"duration": duration, "width": width, "height": height, "fps": 30,
            "source_files": count, "source_bytes": total, "source_sha256": digests}


def find_cli() -> Path:
    explicit = os.getenv("SCENE_RELAY_HYPERFRAMES_CLI")
    candidates = [Path(explicit)] if explicit else []
    if not explicit:
        cache = Path(os.getenv("LOCALAPPDATA", str(Path.home() / ".cache"))) / "npm-cache" / "_npx"
        candidates += sorted(cache.glob("*/node_modules/hyperframes/bin/hyperframes.mjs"))
        candidates += [Path.home() / ".local/lib/node_modules/hyperframes/bin/hyperframes.mjs"]
    for candidate in candidates:
        if candidate.is_file():
            try:
                package = json.loads((candidate.parent.parent / "package.json").read_text())
            except (OSError, ValueError):
                continue
            if package.get("name") == "hyperframes" and package.get("version") == VERSION:
                return candidate.resolve()
    raise RenderError("hyperframes_0_8_169_not_installed")


def find_browser() -> Path:
    explicit = os.getenv("SCENE_RELAY_BROWSER_PATH") or os.getenv("HYPERFRAMES_BROWSER_PATH")
    if explicit:
        candidates = [Path(explicit)]
    else:
        candidates = sorted((Path.home() / ".cache/hyperframes/chrome").glob("**/chrome-headless-shell.exe"))
        candidates += sorted((Path.home() / ".cache/hyperframes/chrome").glob("**/chrome-headless-shell"))
        candidates += [Path(p) for p in (r"C:\Program Files\Google\Chrome\Application\chrome.exe", "/usr/bin/chromium", "/usr/bin/google-chrome")]
    for candidate in candidates:
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate.resolve()
    raise RenderError("installed_browser_missing_no_download")


def renderer_env(browser: Path, project: Path | None = None) -> dict[str, str]:
    env = os.environ.copy()
    for name in tuple(env):
        if name.startswith(("PRODUCER_", "HF_", "HYPERFRAMES_")) or name in {"NODE_OPTIONS", "NODE_PATH"}:
            env.pop(name, None)
    env.update({"HYPERFRAMES_NO_TELEMETRY": "1", "DO_NOT_TRACK": "1",
                "HYPERFRAMES_NO_UPDATE_CHECK": "1", "HYPERFRAMES_NO_AUTO_INSTALL": "1",
                "HYPERFRAMES_SKIP_SKILLS": "1", "HYPERFRAMES_EXTRACT_CACHE_DIR": "off",
                "HYPERFRAMES_BROWSER_PATH": str(browser), "CI": "1",
                "PRODUCER_WORKERS": "1"})
    if project is not None:
        # Supported by installed 0.8.169 historyOwner/font resolver. Keep cache
        # below .hyperframes, which its own history inventory explicitly skips.
        cache = project.resolve() / ".hyperframes" / "scene-relay-cache"
        ensure_no_links(cache)
        for name in ("tmp", "history", "fonts"):
            directory = cache / name
            ensure_no_links(directory)
            directory.mkdir(parents=True, exist_ok=True)
        env.update({"HYPERFRAMES_HISTORY_ROOT": str(cache / "history"),
                    "HYPERFRAMES_FONT_CACHE_DIR": str(cache / "fonts"),
                    "TMP": str(cache / "tmp"), "TEMP": str(cache / "tmp"),
                    "TMPDIR": str(cache / "tmp")})
    return env


def terminate_tree(proc: subprocess.Popen) -> dict:
    outcome = {"pid": proc.pid, "root_exited": proc.poll() is not None,
               "tree_cleanup_confirmed": False, "method": "already_exited"}
    if outcome["root_exited"]:
        outcome["exit_code"] = proc.poll()
        return outcome
    try:
        if os.name == "nt":
            outcome["method"] = "taskkill_owned_tree"
            result = subprocess.run([str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/taskkill.exe"),
                                     "/PID", str(proc.pid), "/T", "/F"], stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=15, check=False)
            outcome["tree_command_exit_code"] = result.returncode
            outcome["tree_cleanup_confirmed"] = result.returncode == 0
        else:
            outcome["method"] = "kill_owned_process_group"
            os.killpg(proc.pid, signal.SIGKILL)
            outcome["tree_cleanup_confirmed"] = True
    except (OSError, subprocess.TimeoutExpired):
        outcome["cleanup_error"] = "tree_termination_not_confirmed"
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        outcome["fallback"] = "kill_owned_root"
        try:
            proc.kill()
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            outcome["cleanup_error"] = "root_termination_not_confirmed"
    outcome["root_exited"] = proc.poll() is not None
    outcome["exit_code"] = proc.poll()
    outcome["tree_cleanup_confirmed"] &= outcome["root_exited"]
    return outcome


class Runner:
    def __init__(self, project: Path, env: dict, total_timeout: int = 1800):
        self.project, self.env = project, env
        self.deadline = time.monotonic() + total_timeout
        self.active = None

    def run(self, args: list[str], stage: str, timeout: int) -> tuple[int, str]:
        print(json.dumps({"stage": stage, "state": "running"}), flush=True)
        remaining = min(timeout, self.deadline - time.monotonic())
        if remaining <= 0:
            raise RenderError("render_deadline_exceeded")
        capture = bytearray()
        overflow = threading.Event()
        self.active = subprocess.Popen(args, cwd=self.project, env=self.env, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, shell=False,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                                       start_new_session=os.name != "nt")
        proc = self.active

        def read_output():
            while True:
                chunk = proc.stdout.read(16384)
                if not chunk:
                    break
                if len(capture) + len(chunk) <= MAX_CAPTURE:
                    capture.extend(chunk)
                else:
                    overflow.set()
            proc.stdout.close()

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        try:
            returncode = proc.wait(timeout=remaining)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            cleanup = terminate_tree(proc)
            raise RenderError("stage_timeout", stage, cleanup=cleanup) from None
        finally:
            reader.join(timeout=3)
            self.active = None
        if overflow.is_set():
            raise RenderError("stage_output_limit", stage)
        return returncode, capture.decode("utf-8", errors="replace")


def parse_json_output(text: str) -> dict:
    decoder = json.JSONDecoder()
    for offset, character in enumerate(text):
        if character == "{":
            try:
                payload, _ = decoder.raw_decode(text[offset:])
                if isinstance(payload, dict):
                    return payload
            except ValueError:
                pass
    raise RenderError("invalid_stage_json")


def probe_media(runner: Runner, ffprobe: str, path: Path, stage="probe") -> dict:
    code, text = runner.run([ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)], stage, 30)
    if code:
        raise RenderError("media_probe_failed")
    return parse_json_output(text)


def verify_media(data: dict, expected: dict, require_audio: bool = True) -> dict:
    videos = [s for s in data.get("streams", []) if s.get("codec_type") == "video"]
    audios = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
    if len(videos) != 1 or (require_audio and not audios):
        raise RenderError("required_media_stream_missing")
    video = videos[0]
    if video.get("width") != expected["width"] or video.get("height") != expected["height"]:
        raise RenderError("render_dimension_mismatch")
    try:
        duration = float(data["format"]["duration"])
        numerator, denominator = map(float, video["avg_frame_rate"].split("/"))
        fps = numerator / denominator
    except (KeyError, ValueError, ZeroDivisionError):
        raise RenderError("invalid_media_metadata") from None
    if not math.isfinite(duration) or abs(duration - expected["duration"]) > .15:
        raise RenderError("render_duration_mismatch")
    if abs(fps - 30) > .01:
        raise RenderError("render_fps_mismatch")
    if video.get("codec_name") != "h264":
        raise RenderError("render_codec_mismatch")
    return {"duration": duration, "width": video["width"], "height": video["height"],
            "fps": fps, "video_codec": video.get("codec_name"),
            "audio_codec": audios[0].get("codec_name") if audios else None,
            "audio_channels": audios[0].get("channels") if audios else None}


def check_summary(payload: dict) -> dict:
    # Never persist arbitrary CLI output, source text, prompts or absolute paths.
    findings = []
    def visit(value):
        if isinstance(value, dict):
            code = value.get("code")
            if isinstance(code, str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", code):
                findings.append({"code": code, "severity": safe_token(value.get("severity", "unknown"))})
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value[:100]:
                visit(child)
    visit(payload)
    return {"ok": payload.get("ok") is True, "findings": findings[:100]}


def render(project: Path, output: Path, report: dict, runner_factory=Runner) -> None:
    expected = validate_project(project)
    report["source"] = expected
    output = Path(os.path.abspath(output))
    if not inside(output, project) or output.suffix.lower() != ".mp4":
        raise RenderError("output_must_be_project_mp4")
    ensure_no_links(output)
    if output.exists():
        raise RenderError("output_already_exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    cli, browser = find_cli(), find_browser()
    binaries = {name: shutil.which(name) for name in ("node", "ffmpeg", "ffprobe")}
    if not all(binaries.values()):
        raise RenderError("required_binary_missing")
    if shutil.disk_usage(project).free < max(2 * 1024**3, expected["source_bytes"] * 3):
        raise RenderError("insufficient_disk_space")
    runner = runner_factory(project, renderer_env(browser, project))
    report["renderer"] = {"hyperframes_version": VERSION, "quality": "delivery", "workers": 1,
                          "telemetry": False, "downloads": False}
    base = [binaries["node"], str(cli)]
    code, text = runner.run(base + ["check", str(project), "--json", "--timeout", "30000"], "check", 180)
    gate = check_summary(parse_json_output(text))
    report["check"] = gate
    if code or not gate["ok"]:
        raise RenderError("hyperframes_check_failed")
    # Recheck editable sources immediately before execution to detect revisions.
    if validate_project(project)["source_sha256"] != expected["source_sha256"]:
        raise RenderError("source_changed_during_check")
    temporary = output.with_name(output.stem + ".rendering.mp4")
    if temporary.exists():
        raise RenderError("temporary_output_already_exists")
    code, text = runner.run(base + ["render", str(project), "--quality", "delivery", "--fps", "30",
                                  "--workers", "1", "--format", "mp4", "--output", str(temporary),
                                  "--frames-cache-dir", "off"], "render", 1500)
    if code or not temporary.is_file() or temporary.stat().st_size < 1024:
        raise RenderError("hyperframes_render_failed")
    data = probe_media(runner, binaries["ffprobe"], temporary)
    verify_media(data, expected, require_audio=False)
    has_audio = any(s.get("codec_type") == "audio" for s in data.get("streams", []))
    if not has_audio:
        sound = project / "assets" / "sound-design.wav"
        if not sound.is_file():
            raise RenderError("audio_missing")
        ensure_no_links(sound)
        sound_info = probe_media(runner, binaries["ffprobe"], sound, "probe_sound")
        if not any(s.get("codec_type") == "audio" for s in sound_info.get("streams", [])):
            raise RenderError("sound_design_invalid")
        muxed = output.with_name(output.stem + ".muxing.mp4")
        if muxed.exists():
            raise RenderError("temporary_output_already_exists")
        code, _ = runner.run([binaries["ffmpeg"], "-nostdin", "-hide_banner", "-v", "error", "-n",
                              "-i", str(temporary), "-i", str(sound), "-map", "0:v:0", "-map", "1:a:0",
                              "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-af", "apad",
                              "-t", str(expected["duration"]), "-movflags", "+faststart", str(muxed)], "mux_audio", 90)
        if code or not muxed.is_file():
            raise RenderError("audio_mux_failed")
        temporary = muxed
        data = probe_media(runner, binaries["ffprobe"], temporary, "probe_final")
    report["media"] = verify_media(data, expected)
    # A stream is not enough: decode both picture and sound to catch corruption.
    code, _ = runner.run([binaries["ffmpeg"], "-nostdin", "-hide_banner", "-v", "error", "-xerror",
                          "-i", str(temporary), "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-"], "decode_verify", 120)
    if code:
        raise RenderError("media_decode_failed")
    if output.exists():
        raise RenderError("output_already_exists")
    temporary.rename(output)
    with output.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    report["output"] = {"file": output.relative_to(project).as_posix(), "bytes": output.stat().st_size,
                        "sha256": digest}
    report["ok"] = True
    print(json.dumps({"stage": "complete", "state": "succeeded"}), flush=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    project = Path(os.path.abspath(args.project))
    report = {"schema_version": 1, "ok": False, "started_at": int(time.time())}
    report_path = project / "output" / "render-report.json"
    try:
        ensure_no_links(project)
        ensure_no_links(report_path)
        render(project, args.output, report)
    except RenderError as error:
        report["error"] = {"code": error.code, "detail": safe_token(error.detail)}
        if error.cleanup is not None:
            report["cleanup"] = error.cleanup
        print(json.dumps({"stage": "failed", "code": error.code}), flush=True)
    except Exception:
        report["error"] = {"code": "renderer_internal_error"}
        print(json.dumps({"stage": "failed", "code": "renderer_internal_error"}), flush=True)
    finally:
        report["finished_at"] = int(time.time())
        try:
            ensure_no_links(report_path)
            if project.is_dir():
                report_path.parent.mkdir(parents=True, exist_ok=True)
                report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        except (OSError, RenderError):
            pass
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
