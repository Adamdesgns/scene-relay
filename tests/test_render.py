"""Synthetic renderer gates; these tests never invoke HyperFrames or Claude."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

spec = importlib.util.spec_from_file_location("render", Path(__file__).parents[1] / "server/render.py")
render = importlib.util.module_from_spec(spec)
spec.loader.exec_module(render)

HTML = '<html><head></head><body><div data-composition-id="demo" data-duration="4" data-width="1920" data-height="1080"><span>Demo</span></div><script>const tl = function(){ return 1; }; window.__timelines.demo = tl;</script></body></html>'


def media(audio=True, duration=4, width=1920):
    streams = [{"codec_type": "video", "codec_name": "h264", "width": width, "height": 1080, "avg_frame_rate": "30/1"}]
    if audio:
        streams += [{"codec_type": "audio", "codec_name": "aac", "channels": 2}]
    return {"format": {"duration": str(duration)}, "streams": streams}


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent, prefix="render-test-")
        self.project = Path(self.temporary.name)
        (self.project / "index.html").write_text(HTML, encoding="utf-8")
        (self.project / "hyperframes.json").write_text('{}', encoding="utf-8")
        self.expected = {"duration": 4, "width": 1920, "height": 1080}

    def tearDown(self):
        self.temporary.cleanup()

    def inject(self, markup):
        (self.project / "index.html").write_text(HTML.replace('</body>', markup + '</body>'), encoding="utf-8")

    def assert_gate(self, markup, code):
        self.inject(markup)
        with self.assertRaises(render.RenderError) as caught:
            render.validate_project(self.project)
        self.assertEqual(caught.exception.code, code)

    def test_valid_local_project(self):
        self.assertEqual(render.validate_project(self.project)["duration"], 4)

    def test_remote_image(self):
        self.assert_gate('<img src="https://example.test/x.png">', "external_resource_rejected")

    def test_external_css(self):
        self.assert_gate('<style>body{background:url(//example.test/x.png)}</style>', "external_resource_rejected")

    def test_local_font_missing(self):
        self.assert_gate('<style>@font-face{font-family:Demo;src:url(assets/demo.woff2)}</style>', "missing_local_resource")

    def test_css_import(self):
        self.assert_gate('<style>@import "https://example.test/font.css";</style>', "external_resource_rejected")

    def test_external_anchor_is_not_asset(self):
        self.inject('<a href="https://example.test">Read more</a>')
        render.validate_project(self.project)

    def test_js_network_rejected(self):
        self.assert_gate('<script>fetch("/api")</script>', "script_capability_rejected")

    def test_dynamic_import_rejected(self):
        self.assert_gate('<script>import("./thing.js")</script>', "script_capability_rejected")

    def test_dynamic_external_resource_rejected(self):
        self.assert_gate('<script>img.src = "https://example.test/x.png"</script>', "external_script_url_rejected")

    def test_script_reference_into_excluded_directories_rejected(self):
        for folder in ('output', 'renders', '.hyperframes', '.git', 'node_modules', 'Output'):
            with self.subTest(folder=folder):
                target = self.project / folder / 'helper.js'
                target.parent.mkdir(exist_ok=True)
                target.write_text('fetch("https://example.test")')
                self.assert_gate(f'<script src="{folder}/helper.js"></script>', 'resource_in_excluded_directory')

    def test_script_unknown_extension_rejected(self):
        assets = self.project / 'assets'
        assets.mkdir()
        for extension in ('.cjs', '.txt', ''):
            with self.subTest(extension=extension):
                target = assets / ('helper' + extension)
                target.write_text('fetch("https://example.test")')
                self.assert_gate(f'<script src="assets/{target.name}"></script>', 'unsupported_script_extension')

    def test_local_script_content_audited(self):
        assets = self.project / 'assets'
        assets.mkdir()
        (assets / 'helper.js').write_text('fetch("https://example.test")')
        self.assert_gate('<script src="assets/helper.js"></script>', 'script_capability_rejected')

    def test_stylesheet_unknown_extension_rejected(self):
        (self.project / 'style.txt').write_text('body{background:url(https://example.test/image.png)}')
        self.assert_gate('<link rel="stylesheet" href="style.txt">', 'unsupported_stylesheet_extension')

    def test_css_import_unknown_extension_rejected(self):
        (self.project / 'style.txt').write_text('body{background:url(https://example.test/image.png)}')
        for value in ('"style.txt"', 'url(style.txt)', 'url("style.txt")'):
            with self.subTest(value=value):
                self.assert_gate(f'<style>@import {value};</style>', 'unsupported_stylesheet_extension')

    def test_stylesheet_excluded_directory_rejected(self):
        (self.project / 'output').mkdir()
        (self.project / 'output/helper.css').write_text('body{background:red}')
        self.assert_gate('<link rel="stylesheet" href="output/helper.css">', 'resource_in_excluded_directory')

    def test_composition_unknown_extension_rejected(self):
        (self.project / 'child.txt').write_text('<script>fetch("https://example.test")</script>')
        self.assert_gate('<div data-composition-src="child.txt"></div>', 'unsupported_composition_extension')

    def test_svg_namespace_is_not_request(self):
        self.inject('<script>document.createElementNS("http://www.w3.org/2000/svg", "svg")</script>')
        render.validate_project(self.project)

    def test_node_execution_rejected(self):
        self.assert_gate('<script>require("child_process").exec("cmd")</script>', "script_capability_rejected")

    def test_dynamic_eval_rejected(self):
        self.assert_gate('<script>new Function("alert(1)")</script>', "script_capability_rejected")

    def test_navigation_rejected(self):
        self.assert_gate('<script>window.location.href="https://example.test"</script>', "script_capability_rejected")

    def test_iframe_rejected(self):
        self.assert_gate('<iframe src="index.html"></iframe>', "active_html_rejected")

    def test_encoded_traversal_rejected(self):
        self.assert_gate('<img src="%2e%2e/private.png">', "resource_outside_project")

    def test_encoded_drive_rejected(self):
        self.assert_gate('<img src="C%3A/private.png">', "external_resource_rejected")

    def test_symlink_rejected(self):
        link = self.project / "linked.html"
        try:
            link.symlink_to(self.project / "index.html")
        except OSError:
            self.skipTest("Symlink creation unavailable for this Windows user")
        with self.assertRaises(render.RenderError) as caught:
            render.validate_project(self.project)
        self.assertEqual(caught.exception.code, "linked_path_rejected")

    def test_bad_duration(self):
        for duration in ('nan', '181', '-1'):
            (self.project / "index.html").write_text(HTML.replace('data-duration="4"', f'data-duration="{duration}"'), encoding="utf-8")
            with self.assertRaises(render.RenderError):
                render.validate_project(self.project)

    def test_config_hooks_rejected(self):
        (self.project / "hyperframes.json").write_text('{"hooks":{"render":"evil"}}')
        with self.assertRaises(render.RenderError) as caught:
            render.validate_project(self.project)
        self.assertEqual(caught.exception.code, "executable_config_rejected")

    def test_config_external_path_rejected(self):
        (self.project / "hyperframes.json").write_text('{"paths":{"assets":"../outside"}}')
        with self.assertRaises(render.RenderError):
            render.validate_project(self.project)

    def test_valid_media(self):
        self.assertEqual(render.verify_media(media(), self.expected)["fps"], 30)

    def test_invalid_media(self):
        for data in (media(audio=False), media(duration=4.2), media(width=1280), {"streams": []}):
            with self.assertRaises(render.RenderError):
                render.verify_media(data, self.expected)

    def test_no_secret_or_source_text_in_report(self):
        source = {"ok": False, "prompt": "secret prompt", "path": "C:/private/file", "errors": [{"code": "layout_error", "message": "secret prompt", "severity": "error"}]}
        report = json.dumps(render.check_summary(source))
        self.assertNotIn("secret", report)
        self.assertNotIn("private", report)
        self.assertIn("layout_error", report)

    def run_fake(self, check_ok=True, check_code=0, render_code=0, audio=True):
        calls = []
        class FakeRunner:
            def __init__(_, *args): pass
            def run(_, args, stage, timeout):
                calls.append((args, stage))
                if stage == "check":
                    return check_code, json.dumps({"ok": check_ok})
                if stage == "render":
                    Path(args[args.index("--output") + 1]).write_bytes(b"fake-mp4" * 1024)
                    return render_code, "secret prompt must never persist"
                if stage == "probe":
                    return 0, json.dumps(media(audio=audio))
                if stage == "decode_verify":
                    return 0, ""
                raise AssertionError(stage)
        report = {"ok": False}
        with patch.object(render, "find_cli", return_value=Path('hyperframes.mjs')), patch.object(render, "find_browser", return_value=Path('chrome.exe')), patch.object(render.shutil, "which", side_effect=lambda name: name), patch.object(render.shutil, "disk_usage", return_value=type('Disk', (), {'free': 10 * 1024**3})()):
            try:
                render.render(self.project, self.project / 'output/final.mp4', report, FakeRunner)
            except render.RenderError as error:
                return calls, report, error.code
        return calls, report, None

    def test_check_failure_stops_before_render(self):
        calls, report, code = self.run_fake(check_ok=False)
        self.assertEqual(code, 'hyperframes_check_failed')
        self.assertEqual([stage for _, stage in calls], ['check'])
        self.assertFalse(report['ok'])

    def test_nonzero_check_even_when_json_claims_success(self):
        calls, report, code = self.run_fake(check_code=1)
        self.assertEqual(code, 'hyperframes_check_failed')
        self.assertEqual(len(calls), 1)

    def test_render_failure_cannot_succeed(self):
        _, report, code = self.run_fake(render_code=1)
        self.assertEqual(code, 'hyperframes_render_failed')
        self.assertFalse(report['ok'])
        self.assertFalse((self.project / 'output/final.mp4').exists())

    def test_missing_audio_fails(self):
        _, report, code = self.run_fake(audio=False)
        self.assertEqual(code, 'audio_missing')
        self.assertFalse(report['ok'])

    def test_success_uses_fixed_render_flags(self):
        calls, report, code = self.run_fake()
        self.assertIsNone(code)
        self.assertTrue(report['ok'])
        args = next(args for args, stage in calls if stage == 'render')
        self.assertEqual(args[args.index('--workers') + 1], '1')
        self.assertEqual(args[args.index('--fps') + 1], '30')
        self.assertEqual(args[args.index('--quality') + 1], 'delivery')
        self.assertNotIn('secret', json.dumps(report))
        self.assertTrue((self.project / 'output/final.mp4').exists())

    def test_existing_output_untouched(self):
        (self.project / 'output').mkdir()
        output = self.project / 'output/final.mp4'
        output.write_bytes(b'existing')
        _, _, code = self.run_fake()
        self.assertEqual(code, 'output_already_exists')
        self.assertEqual(output.read_bytes(), b'existing')

    def test_renderer_environment_disables_telemetry_and_updates(self):
        with patch.dict(os.environ, {'NODE_OPTIONS': '--import evil', 'PRODUCER_WORKERS': '9'}):
            env = render.renderer_env(Path('chrome.exe'))
        self.assertNotIn('NODE_OPTIONS', env)
        self.assertEqual(env['HYPERFRAMES_NO_TELEMETRY'], '1')
        self.assertEqual(env['HYPERFRAMES_NO_AUTO_INSTALL'], '1')
        self.assertEqual(env['PRODUCER_WORKERS'], '1')

    def test_renderer_cache_and_temp_scoped_to_project(self):
        env = render.renderer_env(Path('chrome.exe'), self.project)
        for key in ('HYPERFRAMES_HISTORY_ROOT', 'HYPERFRAMES_FONT_CACHE_DIR', 'TMP', 'TEMP', 'TMPDIR'):
            with self.subTest(key=key):
                self.assertTrue(render.inside(Path(env[key]), self.project / '.hyperframes/scene-relay-cache'))
                self.assertTrue(Path(env[key]).is_dir())
        self.assertEqual(env['HYPERFRAMES_EXTRACT_CACHE_DIR'], 'off')
        self.assertEqual(env['HYPERFRAMES_SKIP_SKILLS'], '1')

    def test_cleanup_waits_after_root_kill_and_reports_uncertain_tree(self):
        proc = Mock(pid=12345)
        proc.poll.side_effect = [None, 1, 1]
        proc.wait.side_effect = [subprocess.TimeoutExpired('synthetic', 10), 1]
        with patch.object(render.os, 'name', 'nt'), patch.object(render.subprocess, 'run', return_value=Mock(returncode=5)):
            result = render.terminate_tree(proc)
        proc.kill.assert_called_once()
        self.assertEqual(proc.wait.call_count, 2)
        self.assertTrue(result['root_exited'])
        self.assertFalse(result['tree_cleanup_confirmed'])

    def test_cleanup_denial_is_not_reported_as_exit(self):
        proc = Mock(pid=12345)
        proc.poll.return_value = None
        proc.wait.side_effect = subprocess.TimeoutExpired('synthetic', 10)
        proc.kill.side_effect = PermissionError()
        with patch.object(render.os, 'name', 'nt'), patch.object(render.subprocess, 'run', return_value=Mock(returncode=5)):
            result = render.terminate_tree(proc)
        self.assertFalse(result['root_exited'])
        self.assertFalse(result['tree_cleanup_confirmed'])
        self.assertEqual(result['cleanup_error'], 'root_termination_not_confirmed')

    def test_successful_tree_cleanup_reported(self):
        proc = Mock(pid=12345)
        proc.poll.side_effect = [None, 1, 1]
        with patch.object(render.os, 'name', 'nt'), patch.object(render.subprocess, 'run', return_value=Mock(returncode=0)):
            result = render.terminate_tree(proc)
        self.assertTrue(result['root_exited'])
        self.assertTrue(result['tree_cleanup_confirmed'])

    def test_synthetic_subprocess_output_is_bounded(self):
        runner = render.Runner(self.project, os.environ.copy(), 20)
        with patch.object(render, 'MAX_CAPTURE', 1024):
            with self.assertRaises(render.RenderError) as caught:
                runner.run([sys.executable, '-c', 'print("x" * 10000)'], 'synthetic_flood', 10)
        self.assertEqual(caught.exception.code, 'stage_output_limit')

    def test_synthetic_subprocess_timeout(self):
        runner = render.Runner(self.project, os.environ.copy(), 20)
        with self.assertRaises(render.RenderError) as caught:
            runner.run([sys.executable, '-c', 'import time; time.sleep(20)'], 'synthetic_timeout', .2)
        self.assertEqual(caught.exception.code, 'stage_timeout')


if __name__ == '__main__':
    unittest.main()
