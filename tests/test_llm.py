"""HTTP 适配器契约测试：检查真正发给传输层的请求字节，不消耗 API 额度。"""

import io
import json
import unittest
from unittest.mock import Mock
from urllib.error import HTTPError, URLError

from agent_lab.llm import ChatCompletionsModel, ModelError, NoRedirect


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.model = ChatCompletionsModel("https://example.com/v1/", "test-model", "fake-test-key")
        self.model.opener = Mock()

    def response(self, message=None, reason="stop"):
        payload = {"choices": [{"message": message or {"role": "assistant", "content": "好"},
                                "finish_reason": reason}], "usage": {"prompt_tokens": 12}}
        self.model.opener.open.return_value = io.BytesIO(json.dumps(payload).encode())

    def test_request_endpoint_auth_and_message_preservation(self):
        message = {"role": "assistant", "content": "好", "reasoning_content": "provider-field"}
        self.response(message)
        reply = self.model.complete([{"role": "user", "content": "你好"}], [])
        request = self.model.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://example.com/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer fake-test-key")
        payload = json.loads(request.data)
        self.assertEqual(payload["model"], "test-model")
        self.assertEqual(payload["messages"][0]["content"], "你好")
        self.assertEqual(reply.message, message)
        self.assertEqual(reply.usage["prompt_tokens"], 12)

    def test_truncated_response_is_not_executed_as_success(self):
        self.response(reason="length")
        with self.assertRaises(ModelError):
            self.model.complete([], [])

    def test_structurally_invalid_json_becomes_model_error(self):
        self.model.opener.open.return_value = io.BytesIO(b'{"choices":[42]}')
        with self.assertRaises(ModelError):
            self.model.complete([], [])

    def test_http_error_does_not_echo_response_secrets(self):
        self.model.opener.open.side_effect = HTTPError("https://example.com", 401, "secret", {}, io.BytesIO(b"secret"))
        with self.assertRaises(ModelError) as caught:
            self.model.complete([], [])
        self.assertIn("401", str(caught.exception))
        self.assertNotIn("secret", str(caught.exception))

    def test_network_and_bad_json_become_model_errors(self):
        self.model.opener.open.side_effect = URLError("private-network-detail")
        with self.assertRaises(ModelError):
            self.model.complete([], [])
        self.model.opener.open.side_effect = None
        self.model.opener.open.return_value = io.BytesIO(b"not json")
        with self.assertRaises(ModelError):
            self.model.complete([], [])

    def test_remote_http_and_embedded_credentials_rejected(self):
        for url in ("http://example.com/v1", "https://user:pass@example.com", "https://example.com/?key=x"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                ChatCompletionsModel(url, "test")
        ChatCompletionsModel("http://127.0.0.1:8000/v1", "test")

    def test_redirects_are_not_followed(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "https://elsewhere.example"))
