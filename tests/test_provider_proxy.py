import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.providers.deepseek import DeepSeekProvider
from src.providers.openai import OpenAIProvider


class ProviderProxyTests(unittest.TestCase):
    def setUp(self):
        self.api_config = SimpleNamespace(
            api_key="test-key",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            model="qwen3.5-plus",
            timeout=30,
        )

    def test_openai_provider_uses_session_without_env_proxy_for_non_stream_request(self):
        session = Mock()
        response = Mock()
        response.status_code = 200
        response.json.return_value = {"choices": [{"message": {"content": '{"command":"df -h","explanation":"查看磁盘"}'}}]}
        session.post.return_value = response

        with patch("src.providers.openai.requests.Session", return_value=session):
            provider = OpenAIProvider(self.api_config)
            result = provider.generate_command("查看磁盘", {"ID": "ubuntu"})

        self.assertFalse(session.trust_env)
        session.post.assert_called_once()
        self.assertEqual(result["command"], "df -h")

    def test_openai_provider_uses_session_without_env_proxy_for_availability_check(self):
        session = Mock()
        response = Mock(status_code=200, text="ok")
        session.get.return_value = response

        with patch("src.providers.openai.requests.Session", return_value=session):
            provider = OpenAIProvider(self.api_config)
            available = provider.is_available()

        self.assertTrue(available)
        self.assertFalse(session.trust_env)
        session.get.assert_called_once()

    def test_deepseek_provider_uses_session_without_env_proxy_for_stream_request(self):
        session = Mock()
        response = Mock()
        response.status_code = 200
        response.iter_lines.return_value = [b'data: {"choices":[{"delta":{"content":"hello"}}]}', b'data: [DONE]']
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)
        session.post.return_value = response

        with patch("src.providers.deepseek.requests.Session", return_value=session):
            provider = DeepSeekProvider(self.api_config)
            chunks = list(provider.stream_response([{"role": "user", "content": "ping"}]))

        self.assertEqual(chunks, ["hello"])
        self.assertFalse(session.trust_env)
        session.post.assert_called_once()


if __name__ == "__main__":
    unittest.main()
