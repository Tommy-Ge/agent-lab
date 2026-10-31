"""给模型使用的工具接口（ACI）：参数、权限、执行、错误与长输出处理。

文件工具限制在 workspace 内，默认只读。bash 是独立的显式授权能力，
不受 Python 路径检查约束；设置 cwd 并不等于操作系统沙箱。
"""

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from .storage import RunStore


def schema(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties,
                       "required": required, "additionalProperties": False},
    }}


def field(kind: str, description: str) -> dict:
    return {"type": kind, "description": description}


class Toolset:
    MAX_FILE_BYTES = 1_000_000
    MAX_COMMAND_BYTES = 1_000_000
    HIDDEN = {".git", ".agent-lab", ".ssh", ".aws", ".venv", "venv", "node_modules", "__pycache__"}

    def __init__(self, workspace: Path, store: RunStore, *, allow_write: bool = False,
                 allow_shell: bool = False, output_limit: int = 6000, command_timeout: float = 30):
        if output_limit < 500 or not 0 < command_timeout <= 300:
            raise ValueError("output_limit 至少为 500，command_timeout 在 (0, 300] 秒内")
        self.root = workspace.resolve(strict=True)
        self.store = store
        self.allow_write, self.allow_shell = allow_write, allow_shell
        self.output_limit, self.command_timeout = output_limit, command_timeout
        path = field("string", "工作区内的相对路径或绝对路径；不能使用符号链接")
        # 工具列表在整次 run 中固定；禁用的动作由 dispatch 返回权限错误。
        self.schemas = [
            schema("list_files", "列出某个目录的直接子项；分目录探索，避免一次加载整个仓库。", {"path": path}, ["path"]),
            schema("read_file", "读取 UTF-8 文本，附 1-based 行号；大文件按 start_line/end_line 分页。", {
                "path": path, "start_line": field("integer", "起始行（含），默认 1"),
                "end_line": field("integer", "结束行（含），默认起始行 + 199")}, ["path"]),
            schema("edit_file", "替换文件中唯一出现的原文。先 read_file；old_str 不含显示用行号，必须精确匹配一次。", {
                "path": path, "old_str": field("string", "需要替换的非空原文，必要时多带上下文"),
                "new_str": field("string", "替换后的文本")}, ["path", "old_str", "new_str"]),
            schema("write_file", "创建一个新 UTF-8 文件；不覆盖已有文件，修改已有文件请用 edit_file。", {
                "path": path, "content": field("string", "文件完整内容")}, ["path", "content"]),
            schema("bash", "在工作区启动独立 bash 子进程执行命令；需用户启用 --allow-shell；不保留 cd/export 状态。", {
                "command": field("string", "要执行的 bash 命令")}, ["command"]),
            schema("read_output", "分页读取本次运行中落盘的工具输出。offset 和 limit 的单位是字符。", {
                "output_id": field("string", "工具返回的 output-0001.txt 形式 ID"),
                "offset": field("integer", "起始字符，默认 0"),
                "limit": field("integer", "读取字符数，默认 3000，最大 10000")}, ["output_id"]),
        ]
        self.handlers = {name: getattr(self, name) for name in
                         ("list_files", "read_file", "edit_file", "write_file", "bash", "read_output")}

    @classmethod
    def blocked_name(cls, name: str) -> bool:
        return name in cls.HIDDEN or name == ".env" or name.startswith(".env.") or name.endswith((".pem", ".key"))

    def path(self, raw: str) -> Path:
        """检查路径组件而不是字符串前缀：/repo-other 并不属于 /repo。"""
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        # 先检查原路径组件，再 resolve；否则符号链接的信息会被 resolve 抹掉。
        try:
            parts = candidate.relative_to(self.root).parts
        except ValueError:
            raise ValueError("路径必须位于当前工作区") from None
        current = self.root
        for part in parts:
            if part == ".." or self.blocked_name(part):
                raise ValueError("不允许 ..、内部状态目录或常见凭据文件路径")
            current /= part
            if current.is_symlink():
                raise ValueError("文件工具不访问符号链接；请使用工作区内的真实文件")
        resolved = candidate.resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError("路径越出了工作区")
        return resolved

    def list_files(self, path: str) -> str:
        folder = self.path(path)
        if not folder.is_dir():
            raise ValueError("path 必须是目录")
        entries = []
        # 最多遍历 1000 项，避免在意外巨大的目录里耗尽内存。
        with os.scandir(folder) as scan:
            for index, entry in enumerate(scan):
                if index >= 1000:
                    entries.append("[目录过大：仅扫描前 1000 项，请指定更小的目录]")
                    break
                if not self.blocked_name(entry.name) and not entry.is_symlink():
                    entries.append(entry.name + ("/" if entry.is_dir() else ""))
        return "\n".join(sorted(entries)) or "（空目录或仅含已过滤项）"

    def _read(self, path: Path) -> str:
        if not path.is_file():
            raise ValueError("需要一个已存在的普通文件")
        with path.open("rb") as stream:
            raw = stream.read(self.MAX_FILE_BYTES + 1)
        if len(raw) > self.MAX_FILE_BYTES:
            raise ValueError("文件超过 1 MB；请先在工作区准备较小的文本片段")
        return raw.decode("utf-8")

    def read_file(self, path: str, start_line: int = 1, end_line: int | None = None) -> str:
        end_line = start_line + 199 if end_line is None else end_line
        if start_line < 1 or end_line < start_line or end_line - start_line >= 1000:
            raise ValueError("行号从 1 开始，每次最多读取 1000 行")
        lines = self._read(self.path(path)).splitlines()
        body = "\n".join(f"{i + 1:4d} | {lines[i]}" for i in range(start_line - 1, min(end_line, len(lines))))
        return f"共 {len(lines)} 行；本页 {start_line}-{min(end_line, len(lines))}\n{body}"

    def _require_write(self):
        if not self.allow_write:
            raise PermissionError("文件写入未启用；需要用户以 --allow-write 重新启动")

    def edit_file(self, path: str, old_str: str, new_str: str) -> str:
        self._require_write()
        target = self.path(path)
        text = self._read(target)
        if not old_str:
            raise ValueError("old_str 不能为空；请先 read_file 取得原文")
        count = text.count(old_str)
        if count != 1:
            raise ValueError(f"old_str 匹配 {count} 次；请先 read_file，多带几行上下文使它唯一")
        updated = text.replace(old_str, new_str, 1)
        if len(updated.encode("utf-8")) > self.MAX_FILE_BYTES:
            raise ValueError("修改后的文件不能超过 1 MB")
        # 原子替换避免写到一半留下残缺文件，同时保留原权限位。
        temp_name = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent, delete=False) as temp:
                temp_name = temp.name
                temp.write(updated)
            os.chmod(temp_name, target.stat().st_mode & 0o777)
            os.replace(temp_name, target)
        finally:
            if temp_name and os.path.exists(temp_name):
                os.unlink(temp_name)
        return "OK: 已完成 1 处替换"

    def write_file(self, path: str, content: str) -> str:
        self._require_write()
        target = self.path(path)
        if len(content.encode("utf-8")) > self.MAX_FILE_BYTES:
            raise ValueError("新文件不能超过 1 MB")
        # x 模式拒绝覆盖；父目录需要已经存在，以便模型明确理解工作区结构。
        with target.open("x", encoding="utf-8") as stream:
            stream.write(content)
        return "OK: 已创建文件"

    def run_command(self, command: str) -> tuple[str, int]:
        """bash 工具与验证门共用执行器；退出码由程序判断，不能让模型自报成功。"""
        if not self.allow_shell:
            raise PermissionError("bash 未启用；需要用户以 --allow-shell 重新启动")
        executable = shutil.which("bash")
        if not executable or os.name != "posix":
            raise ValueError("bash 工具需要 macOS、Linux 或 WSL")
        if not command.strip():
            raise ValueError("command 不能为空")
        # 不继承 AGENT_API_KEY 等变量。子进程仍能访问当前用户的文件，因此不是沙箱。
        env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TMPDIR") if key in os.environ}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        reason = ""
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen([executable, "--noprofile", "--norc", "-c", command],
                                       cwd=self.root, env=env, stdin=subprocess.DEVNULL,
                                       stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            started = time.monotonic()
            try:
                while process.poll() is None:
                    if time.monotonic() - started >= self.command_timeout:
                        reason = "命令超时"
                        break
                    if os.fstat(output.fileno()).st_size > self.MAX_COMMAND_BYTES:
                        reason = "命令输出超过 1 MB"
                        break
                    time.sleep(0.02)
            finally:
                # 清理整个进程组，也包括父 shell 已退出但仍在运行的后台子进程。
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            output.seek(0)
            raw = output.read(self.MAX_COMMAND_BYTES + 1)
        if len(raw) > self.MAX_COMMAND_BYTES:
            reason = reason or "命令输出超过 1 MB"
        text = raw[:self.MAX_COMMAND_BYTES].decode("utf-8", errors="replace")
        code = 124 if reason else process.returncode
        return f"exit_code={code}" + (f"；{reason}，仅保留已捕获前缀" if reason else "") + "\n" + text, code

    def bash(self, command: str) -> str:
        return self.run_command(command)[0]

    def read_output(self, output_id: str, offset: int = 0, limit: int = 3000) -> str:
        if offset < 0 or not 1 <= limit <= 10000:
            raise ValueError("offset 至少为 0，limit 应在 1..10000")
        return self.store.read_output(output_id, offset, limit)

    def shorten(self, output: str) -> str:
        """只缩短新观测，不改旧历史；完整的已捕获输出仍可按 ID 检索。"""
        if len(output) <= self.output_limit:
            return output
        output_id = self.store.save_output(output)
        kept = self.output_limit - 250
        return output[:kept] + (f"\n[输出原长 {len(output)} 字符，省略 {len(output) - kept} 字符；"
                               f"已保存为 {output_id}，用 read_output 分页读取。]")

    def dispatch(self, call: dict) -> str:
        """模型输出不是可信函数调用；先校验，再分发，错误作为下一轮的观测。"""
        name = call["function"]["name"]
        try:
            if name not in self.handlers:
                raise ValueError("未知工具；可用工具：" + ", ".join(self.handlers))
            args = json.loads(call["function"]["arguments"])
            definition = next(s["function"]["parameters"] for s in self.schemas if s["function"]["name"] == name)
            if not isinstance(args, dict):
                raise ValueError("arguments 必须解析为 JSON 对象")
            if set(args) - definition["properties"].keys() or set(definition["required"]) - args.keys():
                raise ValueError("缺少必要参数或包含未知参数；请按工具 schema 传参")
            for key, value in args.items():
                kind = definition["properties"][key]["type"]
                # bool 是 int 的子类，必须用 type 精确检查，不能把 true 当行号。
                if (kind == "string" and type(value) is not str) or (kind == "integer" and type(value) is not int):
                    raise ValueError(f"参数 {key} 必须是 {kind}")
            result = self.handlers[name](**args)
        except (ValueError, OSError, TypeError) as error:
            result = f"Error: {error}"
        except Exception as error:
            # 未预期的工具错误也必须有 tool 回复，但不泄漏可能含秘密的异常详情。
            result = f"Error: 工具执行失败（{type(error).__name__}）"
        return self.shorten(result)
