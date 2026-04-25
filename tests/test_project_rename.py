import importlib.util
import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from src.config import Config, IntelligenceConfig, LoggingConfig, ServiceConfig, UIConfig


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ProjectRenameTests(unittest.TestCase):
    def test_console_branding_uses_os_agent_team_identity(self):
        console_source = (PROJECT_ROOT / "src" / "ui" / "console.py").read_text(encoding="utf-8")

        self.assertIn("OS_Agent Team", console_source)
        self.assertNotIn("树苗", console_source)
        self.assertIn("操作系统智能代理", console_source)

    def test_setup_and_intelligence_metadata_use_os_agent_team(self):
        setup_source = (PROJECT_ROOT / "setup.py").read_text(encoding="utf-8")
        intelligence_source = (PROJECT_ROOT / "src" / "intelligence" / "__init__.py").read_text(encoding="utf-8")

        self.assertIn('author="OS_Agent Team"', setup_source)
        self.assertIn('__author__ = "OS_Agent Team"', intelligence_source)
        self.assertNotIn("树苗", setup_source)
        self.assertNotIn("树苗", intelligence_source)

    def test_project_contains_original_author_attribution(self):
        readme_source = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        console_source = (PROJECT_ROOT / "src" / "ui" / "console.py").read_text(encoding="utf-8")
        setup_source = (PROJECT_ROOT / "setup.py").read_text(encoding="utf-8")
        intelligence_source = (PROJECT_ROOT / "src" / "intelligence" / "__init__.py").read_text(encoding="utf-8")

        expected_link = "https://github.com/Eilen6316/LinuxAgent"
        self.assertIn(expected_link, readme_source)
        self.assertIn(expected_link, console_source)
        self.assertIn("二次开发", readme_source)
        self.assertIn("二次开发", console_source)
        self.assertIn("LinuxAgent original author", setup_source)
        self.assertIn("LinuxAgent original author", intelligence_source)

    def test_os_agent_entrypoint_file_exists(self):
        self.assertTrue((PROJECT_ROOT / "os_agent.py").exists())

    def test_default_paths_use_os_agent_prefix(self):
        ui_config = UIConfig({})
        logging_config = LoggingConfig({})
        service_config = ServiceConfig({})
        intelligence_config = IntelligenceConfig({})

        self.assertEqual(ui_config.history_file, "~/.os_agent_history")
        self.assertEqual(logging_config.file, "~/.os_agent.log")
        self.assertEqual(service_config.audit_dir, "~/.os_agent/tasks")
        self.assertEqual(intelligence_config.learning_data_file, "~/.os_agent_learning.json")
        self.assertEqual(intelligence_config.knowledge_data_dir, "~/.os_agent_knowledge")
        self.assertEqual(intelligence_config.pattern_data_file, "~/.os_agent_patterns.json")
        self.assertEqual(intelligence_config.context_data_dir, "~/.os_agent_context")

    def test_config_yaml_defaults_use_os_agent_prefix(self):
        config = Config(str(PROJECT_ROOT / "config.yaml"))

        self.assertEqual(config.ui.history_file, "~/.os_agent_history")
        self.assertEqual(config.logging.file, "~/.os_agent.log")
        self.assertEqual(config.service.audit_dir, "~/.os_agent/tasks")

    def test_version_output_uses_os_agent_name(self):
        entrypoint = PROJECT_ROOT / "os_agent.py"
        self.assertTrue(entrypoint.exists())

        spec = importlib.util.spec_from_file_location("os_agent", entrypoint)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            with patch("sys.argv", ["os_agent.py", "--version"]):
                exit_code = module.main()

        self.assertEqual(exit_code, 0)
        self.assertIn("OS_Agent", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
