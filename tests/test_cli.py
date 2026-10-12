import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class LocalAPI(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        last = request["messages"][-1]
        if last["role"] == "user":
            name, arguments = "file", {
                "action": "write", "path": "hello.txt", "content": "hello"
            }
            if last["content"] == "wait":
                name, arguments = "shell", {"command": "echo $$ > pid; sleep 60"}
            delta = {"role": "assistant", "tool_calls": [{
                "index": 0, "id": f"call_{len(request['messages'])}", "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }]}
            reason = "tool_calls"
        else:
            delta, reason = {"role": "assistant", "content": "done"}, "stop"
        body = "data: " + json.dumps({
            "choices": [{"index": 0, "delta": delta, "finish_reason": reason}]
        }) + "\n\ndata: [DONE]\n\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body.encode())))
        self.end_headers()
        self.wfile.write(body.encode())


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), LocalAPI)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.env = {
            **os.environ,
            "MINIAGENT_MODEL": "local-test-model",
            "OPENAI_API_KEY": "test-only-credential",
            "OPENAI_BASE_URL": f"http://127.0.0.1:{self.server.server_port}/v1",
            "NO_PROXY": "127.0.0.1",
        }
        self.command = [
            sys.executable, "-m", "miniagent", "--config", str(ROOT / "miniagent.toml")
        ]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def test_one_shot_and_interactive_resume_through_real_http(self):
        first = subprocess.run(
            [*self.command, "write hello"], cwd=self.root, env=self.env,
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("done", first.stdout)
        self.assertEqual((self.root / "hello.txt").read_text(), "hello")
        second = subprocess.run(
            self.command, cwd=self.root, env=self.env, input="again\n/exit\n",
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("you> ", second.stdout)
        self.assertIn("done", second.stdout)
        log = (self.root / ".miniagent/session.jsonl").read_text()
        self.assertNotIn(self.env["OPENAI_API_KEY"], log)
        records = [json.loads(line) for line in log.splitlines()]
        self.assertEqual(sum(r["kind"] == "run_ended" for r in records), 2)

    def test_ctrl_c_cancels_tool_and_exits_without_replay(self):
        process = subprocess.Popen(
            [*self.command, "wait"], cwd=self.root, env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            deadline = time.monotonic() + 10
            while not (self.root / "pid").exists():
                if process.poll() is not None or time.monotonic() > deadline:
                    self.fail("CLI did not start the shell")
                time.sleep(0.01)
            pid = int((self.root / "pid").read_text())
            process.send_signal(signal.SIGINT)
            _, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 130, stderr)
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
            retry = subprocess.run(
                [*self.command, "continue"], cwd=self.root, env=self.env,
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(retry.returncode, 1)
            self.assertIn("unresolved tool calls", retry.stderr)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()


if __name__ == "__main__":
    unittest.main()
