"""模型适配层：只有这里知道 HTTP 协议，Agent 循环不依赖具体厂商。

使用标准库发送 OpenAI 兼容的 Chat Completions 请求，不需要安装 SDK。
模型必须支持 function calling；base_url 指向服务商文档里的 API 根路径。
"""

import json
import math
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class ModelError(RuntimeError):
    """配置、网络、截断或响应协议错误；CLI 会给出简洁提示。"""


@dataclass
class ModelReply:
    message: dict[str, Any]
    usage: dict[str, Any]


class Model(Protocol):
    """真实模型与测试替身遵循同一个接口，便于独立测试 harness。"""

    def complete(self, messages: list[dict], tools: list[dict]) -> ModelReply: ...


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # 不允许 Authorization 跟随重定向发送到意料之外的地址。
        return None


class ChatCompletionsModel:
    def __init__(self, base_url: str, model: str, api_key: str = "", timeout: float = 60):
        url = urlsplit(base_url)
        if url.scheme not in ("http", "https") or not url.hostname:
            raise ValueError("AGENT_BASE_URL 必须是 http(s) API 根地址")
        if url.username or url.password or url.query or url.fragment:
            raise ValueError("API 根地址不能含账号密码、查询参数或 fragment")
        if url.scheme == "http" and url.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("远程 API 必须使用 HTTPS；只有本机服务允许 HTTP")
        if not model.strip():
            raise ValueError("请设置 AGENT_MODEL，值为服务商支持的模型 ID")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("API timeout 必须为正数")
        self.endpoint = base_url.rstrip("/") + "/chat/completions"
        self.model, self.api_key, self.timeout = model, api_key, timeout
        self.opener = build_opener(NoRedirect())

    def complete(self, messages: list[dict], tools: list[dict]) -> ModelReply:
        payload = {"model": self.model, "messages": messages, "tools": tools, "tool_choice": "auto"}
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        request = Request(self.endpoint, json.dumps(payload).encode(), headers, method="POST")
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                # 服务端返回异常大响应时，不无限制分配内存。
                raw = response.read(4_000_001)
                if len(raw) > 4_000_000:
                    raise ModelError("API 响应超过 4 MB，已停止")
            data = json.loads(raw)
            choice = data["choices"][0]
            if choice.get("finish_reason") not in ("stop", "tool_calls"):
                raise ModelError("API 未正常结束生成（可能达到输出上限或被过滤），未执行本轮工具")
            message = choice["message"]
            if not isinstance(message, dict) or message.get("role") != "assistant":
                raise ModelError("API 没有返回合法的 assistant 消息")
            # 保留工具 ID、原始 JSON 参数以及 provider 的 reasoning_content 等扩展字段。
            # 不自行重建 assistant 消息，否则某些兼容服务的下一轮校验会失败。
            return ModelReply(message, data.get("usage") or {})
        except HTTPError as error:
            # 不打印服务端 body，它可能回显请求内容或凭据。
            hints = {401: "检查 API key", 403: "检查账号/模型权限", 404: "检查 API 根地址和模型 ID", 429: "请求限流或额度不足"}
            raise ModelError(f"API HTTP {error.code}：{hints.get(error.code, '请查看服务商状态或接口配置')}") from None
        except (URLError, TimeoutError, OSError):
            raise ModelError("API 连接失败或超时；请检查网络、代理和 API 根地址") from None
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise ModelError("API 响应格式不符合 Chat Completions 协议") from None
