"""持久化层：轨迹用 JSONL，长输出用独立文本文件。

不把 API key、HTTP 请求头写进日志。注意：任务和工具结果仍可能含业务数据，
所以整个 .agent-lab 目录默认被 .gitignore 排除。
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


class RunStore:
    def __init__(self, workspace: Path):
        # 时间只用于磁盘目录，不放到 system prompt 前缀，避免无意义地破坏缓存。
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
        base = workspace / ".agent-lab"
        # 状态目录不允许通过符号链接指向工作区外。
        for path in (base, base / "runs"):
            if path.is_symlink():
                raise ValueError(".agent-lab 及 runs 不能是符号链接")
        self.directory = base / "runs" / run_id
        self.directory.mkdir(parents=True, exist_ok=False)
        self.trace = self.directory / "trace.jsonl"
        self.output_index = 0

    def log(self, event: str, **fields) -> None:
        """每个事件单独一行；模型调用、工具执行和终止原因可以分别统计。"""
        row = {"event": event, **fields}
        with self.trace.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    def save_output(self, output: str) -> str:
        """返回本次运行内的短 ID，避免让模型任意读取日志目录。"""
        self.output_index += 1
        output_id = f"output-{self.output_index:04d}.txt"
        (self.directory / output_id).write_text(output, encoding="utf-8")
        return output_id

    def read_output(self, output_id: str, offset: int, limit: int) -> str:
        # 只允许读取此实例确实创建过的文件；不接受 ../ 或任意路径。
        valid = {f"output-{i:04d}.txt" for i in range(1, self.output_index + 1)}
        if output_id not in valid:
            raise ValueError("output_id 不存在；请使用工具结果里返回的 ID")
        text = (self.directory / output_id).read_text(encoding="utf-8")
        return f"字符 {offset}:{min(offset + limit, len(text))} / 共 {len(text)}\n" + text[offset:offset + limit]

    def finish(self, **summary) -> None:
        self.log("run_end", **summary)
        (self.directory / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
