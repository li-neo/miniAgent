import asyncio
import json
import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path

from miniagent import Kernel
from miniagent.plugins.tools import LocalTools, ToolsPlugin


class ToolsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.kernel = Kernel()
        self.addCleanup(self.temp.cleanup)
        self.addAsyncCleanup(self.kernel.close)
        await self.kernel.start([ToolsPlugin(cwd=str(self.root))])
        self.tools = self.kernel.require("tools")

    async def test_schema_rejects_missing_fields_without_writing(self):
        result = await self.tools.execute("file", json.dumps({
            "action": "write", "path": "file.txt"
        }))
        self.assertEqual(result["status"], "error")
        self.assertFalse((self.root / "file.txt").exists())
        result = await self.tools.execute("shell", "{invalid json")
        self.assertEqual(result["status"], "error")

    async def test_replace_requires_unique_match(self):
        path = self.root / "file.txt"
        path.write_text("same same")
        result = await self.tools.execute("file", json.dumps({
            "action": "replace", "path": "file.txt", "old": "same", "new": "other"
        }))
        self.assertEqual(result["status"], "error")
        self.assertEqual(path.read_text(), "same same")
        result = await self.tools.execute("file", json.dumps({
            "action": "replace", "path": "file.txt", "old": "same same", "new": "changed"
        }))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(path.read_text(), "changed")

    async def test_shell_reports_exit_code_stderr_and_truncation(self):
        local = LocalTools(str(self.root), timeout=2, output_limit=3)
        result = await local.shell({"command": "printf abcdef; printf error >&2; exit 7"})
        self.assertEqual(result["exit_code"], 7)
        self.assertEqual(result["stdout"], "abc")
        self.assertEqual(result["stderr"], "err")
        self.assertTrue(result["truncated"])

    async def test_timeout_drains_pipes_and_reaps_child(self):
        local = LocalTools(str(self.root), timeout=0.2, output_limit=10)
        code = "import os,time; os.write(1,b'x'*500000); time.sleep(60)"
        command = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"
        with self.assertRaisesRegex(TimeoutError, "Shell exceeded"):
            await asyncio.wait_for(local.shell({"command": command}), timeout=3)

    async def test_cancellation_terminates_shell(self):
        local = LocalTools(str(self.root), timeout=60, output_limit=10)
        task = asyncio.create_task(local.shell({"command": "echo $$ > pid; sleep 60"}))
        try:
            async with asyncio.timeout(3):
                while not (self.root / "pid").exists():
                    await asyncio.sleep(0.01)
            pid = int((self.root / "pid").read_text())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=3)
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
