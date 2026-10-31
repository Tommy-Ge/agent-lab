"""检查真实文件和进程行为，避免只检查函数是否被调用。"""

import json
import os
import shlex
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_lab.demo import tool_call
from agent_lab.storage import RunStore
from agent_lab.tools import Toolset


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = RunStore(self.root)
        self.tools = Toolset(self.root, self.store, allow_write=True, output_limit=500)
        (self.root / "note.txt").write_text("first\nsecond\nfirst\n", encoding="utf-8")

    def call(self, name, **args):
        return self.tools.dispatch(tool_call("test", name, **args))

    def test_paged_read_has_line_numbers(self):
        output = self.call("read_file", path="note.txt", start_line=2, end_line=2)
        self.assertIn("2 | second", output)
        self.assertNotIn("first", output)

    def test_path_traversal_and_prefix_sibling_rejected(self):
        for path in ("../outside.txt", str(self.root) + "-other/file.txt", ".git/config", ".env", ".env.local"):
            with self.subTest(path=path):
                self.assertTrue(self.call("read_file", path=path).startswith("Error:"))

    def test_symlink_is_rejected(self):
        (self.root / "link.txt").symlink_to(self.root / "note.txt")
        self.assertIn("符号链接", self.call("read_file", path="link.txt"))

    def test_state_directory_symlink_is_rejected(self):
        other = self.root / "other"
        other.mkdir()
        (other / ".agent-lab").symlink_to(self.root / ".agent-lab", target_is_directory=True)
        with self.assertRaises(ValueError):
            RunStore(other)

    def test_edit_requires_unique_nonempty_match(self):
        for old in ("first", "absent", ""):
            self.assertTrue(self.call("edit_file", path="note.txt", old_str=old, new_str="changed").startswith("Error:"))
        self.assertIn("OK", self.call("edit_file", path="note.txt", old_str="second", new_str="changed"))
        self.assertEqual((self.root / "note.txt").read_text(), "first\nchanged\nfirst\n")

    def test_write_never_overwrites_existing_file(self):
        self.assertTrue(self.call("write_file", path="note.txt", content="bad").startswith("Error:"))
        self.assertIn("OK", self.call("write_file", path="new.txt", content="你好"))
        self.assertEqual((self.root / "new.txt").read_text(), "你好")

    def test_default_permissions_are_enforced(self):
        self.tools.allow_write = False
        self.assertIn("未启用", self.call("write_file", path="new.txt", content="x"))
        self.assertIn("未启用", self.call("bash", command="echo x"))
        self.assertFalse((self.root / "new.txt").exists())

    def test_malformed_json_and_unknown_tools_become_observations(self):
        call = tool_call("bad", "read_file", path="note.txt")
        call["function"]["arguments"] = "{bad json"
        self.assertTrue(self.tools.dispatch(call).startswith("Error:"))
        self.assertIn("可用工具", self.call("unknown"))

    def test_argument_schema_rejects_wrong_types_extra_and_missing_keys(self):
        cases = ({"path": 123}, {"path": "note.txt", "start_line": True},
                 {"path": "note.txt", "extra": "bad"}, {})
        for args in cases:
            with self.subTest(args=args):
                self.assertTrue(self.call("read_file", **args).startswith("Error:"))

    def test_long_output_can_be_retrieved_without_mutating_original(self):
        original = "中文" * 1000
        clipped = self.tools.shorten(original)
        self.assertLessEqual(len(clipped), 500)
        self.assertIn("output-0001.txt", clipped)
        recovered = self.call("read_output", output_id="output-0001.txt", offset=1000, limit=100)
        self.assertTrue(recovered.endswith(original[1000:1100]))
        self.assertEqual((self.store.directory / "output-0001.txt").read_text(), original)
        self.assertTrue(self.call("read_output", output_id="../trace.jsonl").startswith("Error:"))

    def test_large_file_is_bounded(self):
        (self.root / "big.txt").write_bytes(b"x" * (self.tools.MAX_FILE_BYTES + 1))
        self.assertIn("超过 1 MB", self.call("read_file", path="big.txt"))

    @unittest.skipUnless(os.name == "posix" and shutil.which("bash"), "需要 bash")
    def test_shell_exit_code_timeout_and_api_key_not_inherited(self):
        self.tools.allow_shell = True
        self.tools.command_timeout = 0.1
        with patch.dict(os.environ, {"AGENT_API_KEY": "unit-test-secret"}):
            text, code = self.tools.run_command('printf "%s" "${AGENT_API_KEY-unset}"; exit 7')
        self.assertEqual(code, 7)
        self.assertIn("unset", text)
        self.assertNotIn("unit-test-secret", text)
        text, code = self.tools.run_command("sleep 5")
        self.assertEqual(code, 124)
        self.assertIn("超时", text)

    @unittest.skipUnless(os.name == "posix" and shutil.which("bash"), "需要 bash")
    def test_command_output_is_bounded(self):
        self.tools.allow_shell = True
        command = shlex.join([sys.executable, "-c", "print('x' * 1100000)"])
        text, code = self.tools.run_command(command)
        self.assertEqual(code, 124)
        self.assertIn("超过 1 MB", text)
        self.assertLess(len(text), 1_001_000)
