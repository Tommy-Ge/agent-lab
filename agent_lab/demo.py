"""离线教学演示：用确定性脚本替代 LLM，其余 harness 与真实模式共用。

脚本知道本例应如何修复，不能用这个演示衡量任何模型的编码能力。
它的用途是让你不用付费或配置密钥就能看到真实的文件修改与测试过程。
"""

import json
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

from .agent import Agent, RunResult
from .llm import ModelReply
from .storage import RunStore
from .tools import Toolset


BUGGY = '''def mean(values):
    """计算算术平均值；这里故意留了一个分母错误，供 Agent 修复。"""
    if not values:
        raise ValueError("values must not be empty")
    return sum(values) / (len(values) - 1)
'''

CHECKS = '''import unittest
from stats import mean

class MeanTests(unittest.TestCase):
    def test_many(self):
        self.assertEqual(mean([2, 4, 6]), 4)
    def test_single(self):
        self.assertEqual(mean([7]), 7)
    def test_empty(self):
        with self.assertRaises(ValueError):
            mean([])

if __name__ == "__main__":
    unittest.main()
'''


def tool_call(call_id: str, name: str, **arguments) -> dict:
    return {"id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}


class DemoModel:
    def __init__(self, command: str):
        self.command = command
        self.index = 0

    def complete(self, messages: list[dict], tools: list[dict]) -> ModelReply:
        self.index += 1
        actions = {
            1: [tool_call("demo-list", "list_files", path="."),
                tool_call("demo-read", "read_file", path="stats.py")],
            2: [tool_call("demo-edit", "edit_file", path="stats.py",
                          old_str="(len(values) - 1)", new_str="len(values)")],
            3: [tool_call("demo-test", "bash", command=self.command)],
        }
        if self.index in actions:
            return ModelReply({"role": "assistant", "content": None, "tool_calls": actions[self.index]}, {})
        return ModelReply({"role": "assistant", "content":
                           "离线演示已把 mean 的分母改为 len(values)。请结合验证门的实际测试结果确认修复。"}, {})


def run_demo() -> tuple[RunResult, str, str]:
    # 每次创建独立临时工作区，既不修改 examples，也不覆盖用户现有代码。
    workspace = Path(tempfile.mkdtemp(prefix="agent-lab-demo-"))
    (workspace / "stats.py").write_text(BUGGY, encoding="utf-8")
    (workspace / "test_stats.py").write_text(CHECKS, encoding="utf-8")
    argv = [sys.executable, "-m", "unittest", "-v"]
    before = subprocess.run(argv, cwd=workspace, capture_output=True, text=True, timeout=15)
    if before.returncode == 0:
        raise RuntimeError("演示前置条件不成立：故意有 bug 的代码竟通过了测试")
    command = shlex.join(argv)
    store = RunStore(workspace)
    tools = Toolset(workspace, store, allow_write=True, allow_shell=True)
    result = Agent(DemoModel(command), tools, store, verify_command=command).run(
        "stats.py 中 mean 的测试失败，请阅读、修复并运行测试。"
    )
    # 再用独立子进程读磁盘上的最终文件；不是检查模型有没有说“通过”。
    after = subprocess.run(argv, cwd=workspace, capture_output=True, text=True, timeout=15)
    if after.returncode != 0 or result.status != "completed":
        raise RuntimeError("离线演示未通过最终验证，请查看 " + str(store.directory))
    return result, before.stdout + before.stderr, after.stdout + after.stderr
