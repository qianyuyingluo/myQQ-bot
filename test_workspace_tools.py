# Adapted from the supplied LabAI workspace-tool tests.
import asyncio
import hashlib
import json
import os
import shlex
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent / '.deps'))
import psutil

from workspace_agent.errors import AppError
from workspace_agent.workspace_tools import WorkspaceTools, validate_workspace_root, workspace_context


def python_command(source):
    if os.name == "nt":
        def quote(value):
            return "'" + value.replace("'", "''") + "'"
        return f"& {quote(sys.executable)} -c {quote(source)}"
    return shlex.join([sys.executable, "-c", source])


class WorkspaceToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "项目 folder"
        self.root.mkdir()
        self.tools = WorkspaceTools(self.root, run_id="test-run")
        self.settings_patch = patch("workspace_agent.workspace_tools.RUNTIME_DIR", self.base / "storage")
        self.settings_patch.start()

    def tearDown(self):
        self.settings_patch.stop()
        self.temp.cleanup()

    async def test_create_read_edit_search_and_diff_unicode(self):
        created = await self.tools.execute("workspace_write", {"path": "src/你好.py", "content": "名字 = '小明'\nprint(名字)\n"})
        self.assertTrue(created["ok"], created)
        self.assertTrue(created["created"])
        self.assertIn("+名字", created["diff"])
        read = await self.tools.execute("workspace_read", {"path": "src/你好.py", "start_line": 2})
        self.assertEqual(read["content"], "print(名字)\n")
        self.assertIn("2: print", read["numbered_content"])
        self.assertEqual(read["sha256"], hashlib.sha256((self.root / "src/你好.py").read_bytes()).hexdigest())
        edited = await self.tools.execute("workspace_edit", {
            "path": "src/你好.py", "old_text": "小明", "new_text": "小红", "expected_sha256": read["sha256"],
        })
        self.assertTrue(edited["ok"], edited)
        self.assertIn("小红", (self.root / "src/你好.py").read_text(encoding="utf-8"))
        search = await self.tools.execute("workspace_search", {"query": "小红"})
        self.assertEqual(search["matches"][0]["path"], "src/你好.py")
        self.assertEqual(search["matches"][0]["line"], 1)

    async def test_edit_protects_existing_and_changed_files(self):
        target = self.root / "a.txt"
        target.write_text("same same", encoding="utf-8")
        denied = await self.tools.execute("workspace_write", {"path": "a.txt", "content": "replacement"})
        self.assertEqual(denied["code"], "WORKSPACE_FILE_EXISTS")
        conflict = await self.tools.execute("workspace_edit", {"path": "a.txt", "old_text": "same", "new_text": "next"})
        self.assertEqual(conflict["code"], "WORKSPACE_EDIT_CONFLICT")
        conflict = await self.tools.execute("workspace_edit", {"path": "a.txt", "old_text": "same same", "new_text": "next", "expected_sha256": "wrong"})
        self.assertEqual(conflict["code"], "WORKSPACE_EDIT_CONFLICT")
        self.assertEqual(target.read_text(), "same same")
        updated = await self.tools.execute("workspace_edit", {"path": "a.txt", "old_text": "same", "new_text": "next", "replace_all": True})
        self.assertTrue(updated["ok"])
        self.assertEqual(updated["replacements"], 2)
        self.assertEqual(target.read_text(), "next next")

    async def test_bom_and_crlf_survive_edit(self):
        target = self.root / "a.txt"
        target.write_bytes(b"\xef\xbb\xbfhello\r\nworld\r\n")
        result = await self.tools.execute("workspace_edit", {"path": "a.txt", "old_text": "hello\nworld", "new_text": "hello\nproject"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(target.read_bytes(), b"\xef\xbb\xbfhello\r\nproject\r\n")

    async def test_paths_and_argument_types_are_checked(self):
        (self.base / "outside.txt").write_text("private", encoding="utf-8")
        for path in ("../outside.txt", str(self.base / "outside.txt"), "a.txt:stream", "CON", "NUL.txt", "bad. ", "\\\\?\\C:\\Windows"):
            with self.subTest(path=path):
                result = await self.tools.execute("workspace_read", {"path": path})
                self.assertFalse(result["ok"], result)
        for args in ({"path": 4}, {"path": "a.txt", "start_line": True}, {"path": "a.txt", "extra": 1}):
            self.assertFalse((await self.tools.execute("workspace_read", args))["ok"])
        self.assertFalse((await self.tools.execute("run_command", {"command": "echo hello", "cwd": ".."}))["ok"])
        with self.assertRaises(AppError):
            validate_workspace_root("relative")

    async def test_symlink_escape_is_rejected(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "a.txt").write_text("do not read", encoding="utf-8")
        link = self.root / "escape"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                self.skipTest("Symlinks unavailable")
            import subprocess
            # mklink creates only this explicit test junction; no filesystem deletion through cmd.
            created = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
            if created.returncode:
                self.skipTest("Junctions unavailable")
        try:
            self.assertFalse((await self.tools.execute("workspace_read", {"path": "escape/a.txt"}))["ok"])
            self.assertFalse((await self.tools.execute("workspace_write", {"path": "escape/new.txt", "content": "bad"}))["ok"])
            listing = await self.tools.execute("workspace_list", {"recursive": True})
            self.assertFalse(any("escape" in item["path"] for item in listing["entries"]))
        finally:
            if link.is_symlink():
                link.unlink()
            else:
                link.rmdir()

    async def test_pagination_and_bounded_responses(self):
        (self.root / "long.txt").write_text("needle\n" * 650, encoding="utf-8")
        first = await self.tools.execute("workspace_read", {"path": "long.txt", "end_line": 3})
        self.assertEqual(first["next_line"], 4)
        second = await self.tools.execute("workspace_read", {"path": "long.txt", "start_line": first["next_line"], "end_line": 6})
        self.assertEqual(second["start_line"], 4)
        first = await self.tools.execute("workspace_search", {"query": "needle", "limit": 2})
        second = await self.tools.execute("workspace_search", {"query": "needle", "limit": 2, "offset": first["next_offset"]})
        self.assertEqual([m["line"] for m in second["matches"]], [3, 4])
        (self.root / "one-line.txt").write_text("a" * 50000, encoding="utf-8")
        huge = await self.tools.execute("workspace_read", {"path": "one-line.txt"})
        self.assertTrue(huge["line_truncated"])
        self.assertIsNone(huge["next_line"])
        self.assertLess(len(json.dumps(huge)), 50000)
        (self.root / "node_modules").mkdir()
        (self.root / "node_modules" / "omit.txt").write_text("needle")
        search = await self.tools.execute("workspace_search", {"query": "needle", "limit": 100})
        self.assertFalse(any("node_modules" in m["path"] for m in search["matches"]))

    async def test_command_real_output_cwd_exit_code_logs_and_env(self):
        source = "import os,pathlib; print('你好'); print(pathlib.Path.cwd()); print(os.environ.get('OPENAI_COMPAT_API_KEY', 'absent')); pathlib.Path('actual.txt').write_text('created', encoding='utf-8')"
        with patch.dict(os.environ, {"OPENAI_COMPAT_API_KEY": "do-not-leak", "UNLISTED_SECRET": "do-not-leak"}):
            result = await self.tools.execute("run_command", {"command": python_command(source)})
        self.assertTrue(result["ok"], result)
        self.assertIn("你好", result["stdout"])
        self.assertIn(str(self.root), result["stdout"])
        self.assertIn("absent", result["stdout"])
        self.assertNotIn("do-not-leak", result["stdout"])
        self.assertEqual((self.root / "actual.txt").read_text(), "created")
        self.assertIn("你好", Path(result["log_paths"]["stdout"]).read_text(encoding="utf-8"))
        failure = await self.tools.execute("run_command", {"command": python_command("import sys; print('failed', file=sys.stderr); sys.exit(7)")})
        self.assertFalse(failure["ok"])
        self.assertEqual(failure["exit_code"], 7)
        self.assertIn("failed", failure["stderr"])

    async def test_command_output_is_drained_and_capped(self):
        result = await self.tools.execute("run_command", {"command": python_command("import sys; sys.stdout.write('x'*300000); sys.stderr.write('y'*300000)")})
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(result["stdout"]), 16000)
        self.assertEqual(len(result["stderr"]), 8000)
        self.assertTrue(result["output_truncated"])
        self.assertEqual(Path(result["log_paths"]["stdout"]).stat().st_size, 300000)

    async def test_timeout_stops_process_tree(self):
        source = "import subprocess,sys,pathlib,time; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); pathlib.Path('child.pid').write_text(str(p.pid)); time.sleep(60)"
        result = await self.tools.execute("run_command", {"command": python_command(source), "timeout_seconds": 2})
        self.assertFalse(result["ok"])
        self.assertTrue(result["timed_out"])
        child = int((self.root / "child.pid").read_text())
        self.assertFalse(psutil.pid_exists(child), f"Leaked child PID {child}")

    async def test_cancellation_stops_process_tree(self):
        source = "import subprocess,sys,pathlib,time; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); pathlib.Path('child.pid').write_text(str(p.pid)); time.sleep(60)"
        task = asyncio.create_task(self.tools.execute("run_command", {"command": python_command(source)}))
        deadline = time.monotonic() + 10
        while not (self.root / "child.pid").exists():
            if task.done():
                self.fail(str(task.result()))
            if time.monotonic() >= deadline:
                task.cancel()
                self.fail("Child process did not start")
            await asyncio.sleep(0.05)
        child = int((self.root / "child.pid").read_text())
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(psutil.pid_exists(child), f"Leaked child PID {child}")

    async def test_progress_arrives_before_command_finishes(self):
        outputs = []

        async def on_output(data):
            outputs.append(data)
            self.assertFalse((self.root / "finished.txt").exists())

        self.tools.on_output = on_output
        source = "import time,pathlib; print('progress', flush=True); time.sleep(1); pathlib.Path('finished.txt').write_text('done')"
        result = await self.tools.execute("run_command", {"command": python_command(source)})
        self.assertTrue(result["ok"], result)
        self.assertTrue(outputs)
        self.assertIn("progress", outputs[0]["stdout"])

    async def test_child_is_cleaned_up_when_shell_exits(self):
        source = "import subprocess,sys,pathlib; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); pathlib.Path('child.pid').write_text(str(p.pid))"
        result = await self.tools.execute("run_command", {"command": python_command(source)})
        self.assertTrue(result["ok"], result)
        child = int((self.root / "child.pid").read_text())
        deadline = time.monotonic() + 3
        while psutil.pid_exists(child) and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        self.assertFalse(psutil.pid_exists(child), f"Leaked detached child PID {child}")

    async def test_workspace_context_reads_project_instructions(self):
        (self.root / "AGENTS.md").write_text("Run the project tests before finishing.\n", encoding="utf-8")
        context = workspace_context(self.root)
        self.assertIn("Run the project tests", context)
        self.assertIn(str(self.root), context)


if __name__ == "__main__":
    unittest.main()
