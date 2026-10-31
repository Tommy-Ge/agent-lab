"""端到端验证：演示前失败、实际修改磁盘、演示后通过。"""

import contextlib
import io
import json
import os
import shutil
import unittest
from unittest.mock import patch

from agent_lab.cli import main
from agent_lab.demo import run_demo


class DemoAndCliTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix" and shutil.which("bash"), "需要 bash")
    def test_demo_repairs_real_file_and_passes_independent_tests(self):
        result, before, after = run_demo()
        workspace = result.run_directory.parents[2]
        self.addCleanup(shutil.rmtree, workspace)
        self.assertIn("FAILED", before)
        self.assertIn("OK", after)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.steps, 4)
        self.assertIn("return sum(values) / len(values)", (workspace / "stats.py").read_text())
        rows = [json.loads(line) for line in (result.run_directory / "trace.jsonl").read_text().splitlines()]
        self.assertEqual(sum(row["event"] == "tool_result" for row in rows), 4)
        self.assertEqual([row["exit_code"] for row in rows if row["event"] == "verification"], [0])

    def test_missing_model_returns_friendly_configuration_error(self):
        stderr = io.StringIO()
        with patch.dict(os.environ, {"AGENT_MODEL": "", "AGENT_API_KEY": ""}), contextlib.redirect_stderr(stderr):
            code = main(["run", "分析仓库"])
        self.assertEqual(code, 2)
        self.assertIn("AGENT_MODEL", stderr.getvalue())
