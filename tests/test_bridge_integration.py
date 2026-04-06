from __future__ import annotations

import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
BRIDGE = ROOT / "scripts" / "legacy_sse_mcp_bridge.py"


def write_message(stdin, payload: dict) -> None:
    encoded = json.dumps(payload).encode("utf-8")
    stdin.write(f"Content-Length: {len(encoded)}\r\n\r\n".encode("ascii"))
    stdin.write(encoded)
    stdin.flush()


def read_message(stdout) -> dict:
    headers = {}
    while True:
        line = stdout.readline()
        if not line:
            raise RuntimeError("Bridge stdout closed unexpectedly")
        if line in (b"\r\n", b"\n"):
            break
        key, value = line.decode("ascii").split(":", 1)
        headers[key.lower().strip()] = value.strip()
    length = int(headers["content-length"])
    body = stdout.read(length)
    return json.loads(body.decode("utf-8"))


class BridgeIntegrationTest(unittest.TestCase):
    def test_bridge_lists_tools_and_calls_profile(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(
                {
                    "upstream_url": "https://ava.reforge.vc/sse/",
                    "connect_timeout_sec": 30,
                    "read_timeout_sec": 300,
                    "request_timeout_sec": 120,
                },
                handle,
            )
            config_path = handle.name

        process = subprocess.Popen(
            ["python3", str(BRIDGE), "--config", config_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        def cleanup() -> None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
            if process.stdin is not None:
                process.stdin.close()
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()

        self.addCleanup(cleanup)
        assert process.stdin is not None
        assert process.stdout is not None

        write_message(
            process.stdin,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "integration-test", "version": "0"},
                },
            },
        )
        init_response = read_message(process.stdout)
        self.assertEqual(init_response["id"], 1)
        self.assertIn("result", init_response)
        self.assertEqual(init_response["result"]["protocolVersion"], "2024-11-05")

        write_message(
            process.stdin,
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        )
        time.sleep(0.2)

        write_message(
            process.stdin,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        tools_response = read_message(process.stdout)
        self.assertEqual(tools_response["id"], 2)
        tool_names = {tool["name"] for tool in tools_response["result"]["tools"]}
        self.assertIn("get_twitter_profile", tool_names)
        self.assertIn("get_twitter_following", tool_names)

        write_message(
            process.stdin,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "get_twitter_profile", "arguments": {"username": "OpenAI"}},
            },
        )
        call_response = read_message(process.stdout)
        self.assertEqual(call_response["id"], 3)
        structured = call_response["result"]["structuredContent"]
        self.assertEqual(structured["username"], "OpenAI")
        self.assertIn("followers_count", structured)
        self.assertFalse(call_response["result"]["isError"])
