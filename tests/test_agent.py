"""验证教程强调的不变量与停止条件；不依赖真实模型的随机输出。"""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from agent_lab.agent import Agent
from agent_lab.demo import tool_call
from agent_lab.llm import ModelError, ModelReply
from agent_lab.storage import RunStore
from agent_lab.tools import Toolset


class ScriptedModel:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []

    def complete(self, messages, tools):
        self.requests.append(copy.deepcopy(messages))
        value = next(self.replies)
        if isinstance(value, Exception):
            raise value
        return ModelReply(value, {"prompt_tokens": 10, "completion_tokens": 5})


def final(text="完成"):
    return {"role": "assistant", "content": text}


def action(*calls):
    return {"role": "assistant", "content": None, "tool_calls": list(calls)}


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = RunStore(self.root)
        self.tools = Toolset(self.root, self.store, allow_write=True)
        (self.root / "note.txt").write_text("hello", encoding="utf-8")

    def run_agent(self, replies, **kwargs):
        self.model = ScriptedModel(replies)
        return Agent(self.model, self.tools, self.store, **kwargs).run("检查 note.txt")

    def events(self):
        return [json.loads(line) for line in self.store.trace.read_text().splitlines()]

    def test_multiple_calls_and_append_only_protocol(self):
        original = action(tool_call("a", "read_file", path="note.txt"), tool_call("b", "list_files", path="."))
        result = self.run_agent([original, final()])
        self.assertEqual(result.status, "completed")
        first, second = self.model.requests
        self.assertEqual(first, second[:len(first)])
        self.assertEqual(second[2], original)
        self.assertEqual([m["tool_call_id"] for m in second[3:]], ["a", "b"])
        self.assertEqual(sum(e["event"] == "model_request" for e in self.events()), result.steps)
        self.assertEqual(sum(e["event"] == "model_response" for e in self.events()), result.steps)

    def test_tool_error_is_available_for_recovery(self):
        bad = tool_call("bad", "read_file", path="missing.txt")
        result = self.run_agent([action(bad), action(tool_call("good", "read_file", path="note.txt")), final()])
        self.assertEqual(result.status, "completed")
        self.assertIn("Error:", self.model.requests[1][-1]["content"])
        self.assertIn("hello", self.model.requests[2][-1]["content"])

    def test_max_steps_still_replies_to_every_tool_call(self):
        result = self.run_agent([action(tool_call("a", "list_files", path="."))], max_steps=1)
        summary = json.loads((self.store.directory / "summary.json").read_text())
        self.assertEqual(result.status, "max_steps")
        self.assertEqual(summary["messages"][-1]["tool_call_id"], "a")

    def test_budget_stops_before_any_api_request(self):
        result = self.run_agent([], max_context_chars=1000)
        self.assertEqual(result.status, "context_limit")
        self.assertEqual(result.steps, 0)
        self.assertEqual(self.model.requests, [])

    def test_invalid_batch_never_partially_writes(self):
        write = tool_call("a", "write_file", path="unwanted.txt", content="x")
        malformed = tool_call("a", "list_files", path=".")  # 重复 ID 导致整批拒绝。
        result = self.run_agent([action(write, malformed)])
        self.assertEqual(result.status, "model_error")
        self.assertFalse((self.root / "unwanted.txt").exists())

    def test_network_failure_has_terminal_event(self):
        result = self.run_agent([ModelError("连接失败")])
        self.assertEqual(result.status, "model_error")
        self.assertEqual(self.events()[-1]["event"], "run_end")

    def test_empty_answer_is_not_completion(self):
        result = self.run_agent([final("  ")])
        self.assertEqual(result.status, "model_error")

    def test_verifier_failure_returns_to_model(self):
        self.tools.allow_shell = True
        results = iter([("exit_code=1\n失败", 1), ("exit_code=0\n通过", 0)])
        self.tools.run_command = lambda command: next(results)
        result = self.run_agent([final(), final("已修正")], verify_command="trusted check")
        self.assertEqual(result.steps, 2)
        self.assertEqual(result.answer, "已修正")
        self.assertIn("验证门未通过", self.model.requests[1][-1]["content"])
        self.assertEqual([e["exit_code"] for e in self.events() if e["event"] == "verification"], [1, 0])

    def test_verifier_failure_cannot_bypass_step_limit(self):
        self.tools.allow_shell = True
        self.tools.run_command = lambda command: ("失败", 1)
        result = self.run_agent([final()], max_steps=1, verify_command="trusted check")
        self.assertEqual(result.status, "max_steps")

    def test_verifier_requires_shell_permission(self):
        with self.assertRaisesRegex(ValueError, "allow-shell"):
            Agent(ScriptedModel([]), self.tools, self.store, verify_command="echo test")
