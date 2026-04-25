import importlib.util
import io
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from src.config import Config, ConfigValidationError


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def write_config(path: Path, api_key: str, audit_dir: Path) -> None:
    path.write_text(
        textwrap.dedent(
            f"""
            api:
              provider: "openai"
              api_key: "{api_key}"
              base_url: "https://example.com/v1"
              model: "test-model"

            ui:
              history_file: "{path.parent / 'history.txt'}"

            logging:
              level: "INFO"
              file: "{path.parent / 'logs' / 'os_agent.log'}"

            service:
              audit_dir: "{audit_dir}"

            intelligence:
              learning:
                data_file: "{path.parent / 'learning.json'}"
              knowledge:
                data_dir: "{path.parent / 'knowledge'}"
              pattern_analysis:
                data_file: "{path.parent / 'patterns.json'}"
              context:
                data_dir: "{path.parent / 'context'}"
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )


class ConfigValidationTests(unittest.TestCase):
    def test_validate_fails_when_api_key_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.yaml"
            write_config(config_path, api_key="", audit_dir=root / "audits")

            config = Config(str(config_path))

            with self.assertRaises(ConfigValidationError) as context:
                config.validate()

            self.assertIn("api.api_key", str(context.exception))

    def test_to_safe_dict_redacts_api_key(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.yaml"
            write_config(config_path, api_key="super-secret-key", audit_dir=root / "audits")

            config = Config(str(config_path))

            safe_config = config.to_safe_dict()

            self.assertEqual(safe_config["api"]["api_key"], "***REDACTED***")
            self.assertNotIn("super-secret-key", str(safe_config))


class OsAgentCheckCommandTests(unittest.TestCase):
    def _load_entrypoint_module(self):
        spec = importlib.util.spec_from_file_location("os_agent", PROJECT_ROOT / "os_agent.py")
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module

    def test_check_returns_success_for_valid_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.yaml"
            write_config(config_path, api_key="valid-key", audit_dir=root / "audits")
            module = self._load_entrypoint_module()

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                with patch("sys.argv", ["os_agent.py", "--check", "-c", str(config_path)]):
                    exit_code = module.main()

            output = stdout.getvalue()
            self.assertEqual(exit_code, 0)
            self.assertIn("配置校验通过", output)
            self.assertNotIn("valid-key", output)

    def test_check_returns_failure_for_invalid_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.yaml"
            write_config(config_path, api_key="", audit_dir=root / "audits")
            module = self._load_entrypoint_module()

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                with patch("sys.argv", ["os_agent.py", "--check", "-c", str(config_path)]):
                    exit_code = module.main()

            output = stdout.getvalue()
            self.assertEqual(exit_code, 1)
            self.assertIn("配置校验失败", output)


if __name__ == "__main__":
    unittest.main()
