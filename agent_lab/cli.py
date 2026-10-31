"""命令行配置入口；API key 只从环境读取，不接受会留在命令历史里的参数。"""

import argparse
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from .agent import Agent
from .demo import run_demo
from .llm import ChatCompletionsModel
from .storage import RunStore
from .tools import Toolset


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="带中文注释的 Python 工具调用 Agent")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("demo", help="运行无需 API key 的确定性修 bug 演示（macOS/Linux/WSL）")
    run = commands.add_parser("run", help="连接真实模型执行任务")
    run.add_argument("task", help="用引号包住完整任务")
    run.add_argument("--workspace", type=Path, default=Path.cwd(), help="已有工作区目录，默认当前目录")
    run.add_argument("--base-url", default=os.getenv("AGENT_BASE_URL", "https://api.openai.com/v1"))
    run.add_argument("--model", default=os.getenv("AGENT_MODEL", ""), help="服务商的模型 ID；必须支持工具调用")
    run.add_argument("--allow-write", action="store_true", help="允许文件工具创建、修改文件")
    run.add_argument("--allow-shell", action="store_true", help="允许任意 bash 命令（不是沙箱，可绕过只读限制）")
    run.add_argument("--verify", help="模型想结束时必须通过的 bash 命令；需要 --allow-shell")
    run.add_argument("--max-steps", type=int, default=20, help="模型调用次数上限，默认 20")
    run.add_argument("--max-context-chars", type=int, default=60000, help="序列化消息+工具的字符预算，非 token 数")
    run.add_argument("--output-limit", type=int, default=6000, help="单条工具观测字符上限，长输出落盘")
    run.add_argument("--command-timeout", type=float, default=30, help="单条 bash 命令时限（秒），最大 300")
    run.add_argument("--api-timeout", type=float, default=60, help="单次 API 请求超时（秒）")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "demo":
            result, before, after = run_demo()
            print("这是确定性脚本演示，没有调用大模型，也没有产生 API 费用。")
            print("\n修复前（预期失败）：\n" + before)
            print("修复后：\n" + after)
        else:
            workspace = args.workspace.expanduser().resolve(strict=True)
            if not workspace.is_dir():
                raise ValueError("--workspace 必须是已有目录")
            if not args.task.strip():
                raise ValueError("任务不能为空")
            model = ChatCompletionsModel(args.base_url, args.model, os.getenv("AGENT_API_KEY", ""), args.api_timeout)
            if not model.api_key and urlsplit(args.base_url).hostname not in ("localhost", "127.0.0.1", "::1"):
                raise ValueError("请先在本机环境变量 AGENT_API_KEY 中设置密钥；不要提交到 GitHub")
            store = RunStore(workspace)
            tools = Toolset(workspace, store, allow_write=args.allow_write, allow_shell=args.allow_shell,
                            output_limit=args.output_limit, command_timeout=args.command_timeout)
            print(f"工作区：{workspace}\n日志：{store.directory}", flush=True)
            if args.allow_shell:
                print("bash 已启用：命令以当前用户权限执行；cwd 和文件工具的路径检查不是沙箱。", flush=True)
            result = Agent(model, tools, store, max_steps=args.max_steps,
                           max_context_chars=args.max_context_chars, verify_command=args.verify).run(args.task)
        print(f"\n状态：{result.status}；模型调用：{result.steps} 次\n{result.answer}\n日志：{result.run_directory}")
        return 0 if result.status == "completed" else 1
    except (ValueError, OSError, RuntimeError) as error:
        print(f"配置或运行错误：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
