import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server.mcp_server import Service, TOOLS


class DenyEngine:
    def start_job(self, **kwargs):
        raise AssertionError("Unvalidated request reached engine")


class MCPTests(unittest.TestCase):
    def setUp(self):
        self.service = Service(str(ROOT / "unused-tests-data"))
        self.service.engine = DenyEngine()

    def request(self, name, arguments):
        return self.service.dispatch({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": name, "arguments": arguments}})

    def test_unknown_argument_is_not_echoed(self):
        response = self.request("start_video_job", {"prompt": "PRIVATE-DO-NOT-ECHO", "shell": "SECRET-KEY"})
        self.assertTrue(response["result"]["isError"])
        self.assertNotIn("PRIVATE", json.dumps(response))
        self.assertNotIn("SECRET", json.dumps(response))

    def test_boolean_is_not_turn_count(self):
        self.assertTrue(self.request("start_video_job", {"prompt": "x", "max_turns": True})["result"]["isError"])

    def test_caller_cannot_set_directory_or_executable(self):
        for key in ("cwd", "data_root", "seed_directory", "claude_path", "render_command"):
            self.assertTrue(self.request("start_video_job", {"prompt": "x", key: "value"})["result"]["isError"])

    def test_unknown_tool(self):
        self.assertTrue(self.request("shell", {})["result"]["isError"])

    def test_notifications_have_no_response(self):
        self.assertIsNone(self.service.dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_real_stdio_handshake_and_inventory_without_model(self):
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}}, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
        p = subprocess.run([sys.executable, str(ROOT / "server/mcp_server.py")], input="\n".join(json.dumps(r) for r in requests) + "\n", text=True, capture_output=True, timeout=10)
        self.assertEqual(p.returncode, 0)
        replies = [json.loads(line) for line in p.stdout.splitlines()]
        self.assertEqual(replies[0]["result"]["serverInfo"]["name"], "scene-relay")
        self.assertEqual(len(replies[1]["result"]["tools"]), 6)
        self.assertFalse((ROOT / "unused-tests-data").exists())

    def test_no_arbitrary_shell_tool(self):
        names = {t["name"] for t in TOOLS}
        self.assertEqual(names, {"scene_relay_health", "start_video_job", "get_video_job", "get_video_result", "render_video_job", "cancel_video_job"})

    def test_malformed_numeric_message_does_not_kill_server(self):
        hostile = '{"jsonrpc":"2.0","id":' + ('1' * 5000) + ',"method":"ping"}\n'
        valid = '{"jsonrpc":"2.0","id":2,"method":"ping"}\n'
        p = subprocess.run([sys.executable, str(ROOT / "server/mcp_server.py")], input=hostile + valid, text=True, capture_output=True, timeout=10)
        self.assertEqual(p.returncode, 0)
        replies = [json.loads(line) for line in p.stdout.splitlines()]
        self.assertEqual(replies[0]["error"]["code"], -32700)
        self.assertEqual(replies[1]["result"], {})


if __name__ == "__main__":
    unittest.main()
