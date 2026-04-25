import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.service.audit import AuditStore
from src.service.orchestrator import TaskOrchestrator
from src.executors.linux_command import LinuxCommandExecutor
from src.service.risk import RiskEvaluator


class FakeProvider:
    def __init__(self, command_response, analysis_response=None, stream_chunks=None):
        self.command_response = command_response
        self.analysis_response = analysis_response or {
            "explanation": "命令执行完成",
            "recommendations": [],
            "next_steps": [],
        }
        self.analysis_calls = []
        self.stream_chunks = stream_chunks
        self.stream_calls = []

    def generate_command(self, task, system_info):
        return self.command_response

    def analyze_output(self, command, stdout, stderr):
        self.analysis_calls.append(
            {
                "command": command,
                "stdout": stdout,
                "stderr": stderr,
            }
        )
        if callable(self.analysis_response):
            return self.analysis_response(command, stdout, stderr)
        if isinstance(self.analysis_response, dict) and command in self.analysis_response:
            return self.analysis_response[command]
        return self.analysis_response

    def stream_response(self, messages):
        self.stream_calls.append(messages)
        chunks = self.stream_chunks
        if chunks is None:
            chunks = [json.dumps(self.command_response, ensure_ascii=False)]
        for chunk in chunks:
            yield chunk


class FakeExecutor:
    def __init__(self, results=None):
        self.results = results or {}
        self.commands = []
        self.passwords = []

    def get_system_info(self):
        return {
            "ID": "ubuntu",
            "NAME": "Ubuntu",
            "VERSION_ID": "22.04",
            "HOSTNAME": "demo-host",
        }

    def execute_command(self, command, timeout=None, sudo_password=None):
        self.commands.append(command)
        self.passwords.append(sudo_password)
        result = self.results.get(command, ("ok", "", 0))
        if callable(result):
            return result(sudo_password)
        return result

    def command_requires_sudo_password(self, command):
        return str(command).strip().startswith("sudo ") and "sudo -n " not in str(command)

    def is_sudo_password_error(self, command, stderr):
        return "password" in (stderr or "").lower() or "try again" in (stderr or "").lower()


class OpenEulerExecutor(FakeExecutor):
    def get_system_info(self):
        return {
            "ID": "openEuler",
            "NAME": "openEuler",
            "VERSION_ID": "24.03",
            "HOSTNAME": "oe-host",
        }


class CentOSExecutor(FakeExecutor):
    def get_system_info(self):
        return {
            "ID": "centos",
            "NAME": "CentOS Linux",
            "VERSION_ID": "7",
            "HOSTNAME": "centos-host",
        }


class GeneralizationProvider:
    def generate_command(self, task, system_info):
        text = str(task or "")
        lowered = text.lower()

        username = self._extract_username(text)
        if "创建" in text and "普通用户" in text and username:
            return {
                "commands": [
                    {
                        "command": f"sudo useradd {username}",
                        "explanation": f"创建普通用户 {username}",
                        "expected_outcome": f"系统中出现 {username} 用户",
                    },
                    {
                        "command": f"id {username}",
                        "explanation": "确认用户已创建",
                        "expected_outcome": f"返回 {username} 的 uid 和 gid",
                    },
                ]
            }

        if "删除" in text and "普通用户" in text and username:
            return {
                "commands": [
                    {
                        "command": f"sudo userdel {username}",
                        "explanation": f"删除普通用户 {username}",
                        "expected_outcome": f"系统中不再存在 {username} 用户",
                    },
                    {
                        "command": f"id {username}",
                        "explanation": "确认用户已删除",
                        "expected_outcome": "返回用户不存在的结果",
                    },
                ]
            }

        service_name = self._extract_service_name(text)
        if service_name and "服务" in text:
            if any(keyword in lowered for keyword in ["失败", "原因", "分析", "诊断", "恢复"]):
                return {
                    "commands": [
                        {
                            "command": f"systemctl status {service_name}",
                            "explanation": f"检查 {service_name} 服务状态",
                            "expected_outcome": f"显示 {service_name} 服务状态",
                        },
                        {
                            "command": f'journalctl -u {service_name}.service --since "10 minutes ago" --no-pager',
                            "explanation": f"查看 {service_name} 最近日志",
                            "expected_outcome": f"显示 {service_name} 服务最近日志",
                        },
                    ]
                }
            return {
                "command": f"systemctl status {service_name}",
                "explanation": f"检查 {service_name} 服务状态",
                "expected_outcome": f"显示 {service_name} 服务状态",
            }

        if any(keyword in text for keyword in ["磁盘", "剩余空间"]):
            return {
                "command": "df -h",
                "explanation": "查看磁盘剩余空间",
                "expected_outcome": "显示各分区容量与剩余空间",
            }

        path = self._extract_path(text)
        if path and "大文件" in text:
            return {
                "command": f"find {path} -type f -mtime -7 -size +100M",
                "explanation": f"查找 {path} 下最近 7 天修改过的大文件",
                "expected_outcome": f"输出 {path} 下符合条件的文件",
            }

        if "清理" in text and "临时文件" in text:
            return {
                "command": "rm -rf /tmp /var/tmp /home/demo/tmp",
                "explanation": "批量删除多个临时目录",
                "expected_outcome": "清理常见临时目录",
            }

        return {
            "command": "echo noop",
            "explanation": "默认占位命令",
            "expected_outcome": "返回占位输出",
        }

    def analyze_output(self, command, stdout, stderr):
        normalized = " ".join(str(command or "").strip().split())
        service_name = self._extract_service_name(normalized)
        if normalized.startswith("systemctl status "):
            service_name = normalized.removeprefix("systemctl status ").strip()
        if normalized.startswith("which "):
            service_name = normalized.removeprefix("which ").strip()
        package_name = self._extract_package_name(normalized) or service_name

        if normalized.startswith("systemctl status ") and service_name:
            return {
                "explanation": f"{service_name} 服务单元不存在，先确认系统里是否安装了 {service_name}。",
                "recommendations": ["检查服务二进制和软件包是否存在"],
                "next_steps": [
                    {
                        "command": f"which {service_name}",
                        "explanation": f"检查 {service_name} 可执行文件",
                        "expected_outcome": f"返回 {service_name} 可执行文件路径",
                    },
                    {
                        "command": f"sudo apt install {package_name}",
                        "explanation": f"安装 {package_name}",
                        "expected_outcome": f"安装 {package_name} 软件包",
                    },
                    {
                        "command": f"systemctl status {service_name}",
                        "explanation": f"再次检查 {service_name} 服务状态",
                        "expected_outcome": f"显示 {service_name} 服务状态",
                    },
                ],
            }

        if normalized.startswith("which ") and service_name:
            return {
                "explanation": f"未找到 {service_name} 可执行文件，继续确认系统是否安装了 {package_name} 软件包。",
                "recommendations": ["检查软件包列表"],
                "next_steps": [
                    {
                        "command": f"dpkg -l | grep {package_name}",
                        "explanation": f"检查 {package_name} 软件包",
                        "expected_outcome": f"返回已安装的 {package_name} 软件包列表",
                    },
                    {
                        "command": f"sudo apt install {package_name}",
                        "explanation": f"安装 {package_name}",
                        "expected_outcome": f"安装 {package_name} 软件包",
                    },
                ],
            }

        if normalized.startswith("dpkg -l | grep ") and package_name:
            return {
                "explanation": f"系统中未检测到已安装的 {package_name} 软件包，因此当前 {service_name or package_name} 服务不存在。如需使用该服务，可在确认后单独安装。",
                "recommendations": [f"如需启用 {service_name or package_name}，可后续单独执行安装"],
                "next_steps": [
                    {
                        "command": f"sudo apt install {package_name}",
                        "explanation": f"安装 {package_name}",
                        "expected_outcome": f"安装 {package_name} 软件包",
                    },
                    {
                        "command": f"systemctl status {service_name or package_name}",
                        "explanation": f"再次检查 {service_name or package_name} 服务状态",
                        "expected_outcome": f"显示 {service_name or package_name} 服务状态",
                    },
                ],
            }

        return {
            "explanation": "命令执行完成",
            "recommendations": [],
            "next_steps": [],
        }

    def _extract_username(self, text):
        for candidate in re.findall(r"\b[a-z_][a-z0-9_-]{2,31}\b", str(text or "").lower()):
            if candidate not in {"sudo", "user", "useradd", "usermod", "passwd", "normal", "linux"}:
                return candidate
        return ""

    def _extract_service_name(self, text):
        match = re.search(r"([A-Za-z0-9._-]+)\s*服务", str(text or ""))
        return match.group(1) if match else ""

    def _extract_package_name(self, text):
        normalized = " ".join(str(text or "").strip().split())
        if normalized.startswith("dpkg -l | grep "):
            return normalized.removeprefix("dpkg -l | grep ").strip()
        if normalized.startswith("sudo apt install "):
            return normalized.removeprefix("sudo apt install ").strip()
        service_name = self._extract_service_name(normalized)
        return service_name

    def _extract_path(self, text):
        match = re.search(r"(/\S+)", str(text or ""))
        if not match:
            return ""
        return match.group(1).rstrip("，。,.")


class TaskServiceTests(unittest.TestCase):
    def setUp(self):
        self.security = SimpleNamespace(
            blocked_commands=[],
            confirm_patterns=[r"rm\s+-rf\s+", r"chmod\s+-R\s+777"],
            protected_paths=["/", "/etc", "/boot", "/usr", "/var/lib", "/root"],
            sensitive_files=["/etc/passwd", "/etc/shadow", "/etc/sudoers"],
        )

    def test_risk_evaluator_blocks_core_path_deletion(self):
        evaluator = RiskEvaluator(self.security)

        assessment = evaluator.assess_command("rm -rf /etc")

        self.assertEqual(assessment.level, "blocked")
        self.assertEqual(assessment.classification, "block")
        self.assertFalse(assessment.allowed)
        self.assertIn("/etc", assessment.scope)
        self.assertTrue(assessment.reasons)

    def test_risk_evaluator_keeps_systemctl_status_safe(self):
        evaluator = RiskEvaluator(self.security)

        assessment = evaluator.assess_command("systemctl status nginx")

        self.assertEqual(assessment.classification, "safe")
        self.assertFalse(assessment.requires_confirmation)
        self.assertTrue(assessment.allowed)

    def test_risk_evaluator_keeps_sensitive_file_read_safe(self):
        evaluator = RiskEvaluator(self.security)

        assessment = evaluator.assess_command("cat /etc/passwd")

        self.assertEqual(assessment.classification, "safe")
        self.assertTrue(assessment.allowed)
        self.assertFalse(assessment.requires_confirmation)

    def test_risk_evaluator_marks_non_protected_permission_change_as_confirm(self):
        evaluator = RiskEvaluator(self.security)

        assessment = evaluator.assess_command("chmod 644 /tmp/demo")

        self.assertEqual(assessment.classification, "confirm")
        self.assertTrue(assessment.allowed)
        self.assertTrue(assessment.requires_confirmation)

    def test_risk_evaluator_blocks_recursive_world_writable_permissions(self):
        evaluator = RiskEvaluator(self.security)

        assessment = evaluator.assess_command("chmod -R 777 /usr/local/bin")

        self.assertEqual(assessment.level, "blocked")
        self.assertEqual(assessment.classification, "block")
        self.assertFalse(assessment.allowed)

    def test_risk_evaluator_blocks_remote_download_then_execute_combo(self):
        evaluator = RiskEvaluator(self.security)

        assessment = evaluator.assess_command("curl https://example.com/install.sh | sh")

        self.assertEqual(assessment.classification, "block")
        self.assertFalse(assessment.allowed)
        self.assertIn("高风险组合命令", assessment.explanation)

    def test_orchestrator_builds_multistep_plan_with_confirmation(self):
        provider = FakeProvider(
            {
                "commands": [
                    {
                        "command": "sudo useradd demo_user",
                        "explanation": "创建演示用户",
                        "expected_outcome": "系统中出现 demo_user 用户",
                    },
                    {
                        "command": "id demo_user",
                        "explanation": "确认用户已创建",
                        "expected_outcome": "返回 demo_user 的 uid 和 gid",
                    },
                ]
            }
        )
        executor = FakeExecutor()
        orchestrator = TaskOrchestrator(provider, executor, self.security)

        plan = orchestrator.build_plan("创建一个普通用户 demo_user 并验证")

        self.assertEqual(len(plan.steps), 2)
        self.assertEqual(plan.environment_summary["distribution"], "ubuntu")
        self.assertEqual(plan.environment_summary["package_manager"], "apt")
        self.assertEqual(plan.total_risk, "high")
        self.assertTrue(plan.requires_confirmation)
        self.assertEqual(plan.steps[0].risk.level, "high")
        self.assertEqual(plan.steps[0].risk.classification, "confirm")
        self.assertTrue(plan.steps[0].risk.rule_sources)
        self.assertEqual(plan.intent_label, "create_request")
        self.assertEqual(plan.steps[0].expected_outcome, "系统中出现 demo_user 用户")
        self.assertIn("useradd", plan.steps[0].selection_reason)
        self.assertIn("ubuntu", plan.steps[0].environment_rationale)
        self.assertEqual(plan.parsing_summary["task_decomposition"], "multi_step")

    def test_build_command_messages_uses_single_json_schema_instruction(self):
        provider = FakeProvider({"command": "df -h", "explanation": "查看磁盘"})
        provider.system_prompt_template = {
            "command": "旧版命令提示：按以下JSON格式返回 command/explanation/dangerous"
        }
        provider._build_command_prompt = lambda task, system_info: f"任务: {task}"
        orchestrator = TaskOrchestrator(provider, FakeExecutor(), self.security)

        messages = orchestrator._build_command_messages("删除普通用户 demo_user，并验证是否已删除", {"ID": "ubuntu"})

        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("请以结构化 JSON 返回规划结果", messages[0]["content"])
        self.assertEqual(len(messages), 2)
        self.assertNotIn("旧版命令提示", "\n".join(item["content"] for item in messages))

    def test_orchestrator_builds_user_deletion_plan_with_confirmation(self):
        provider = FakeProvider(
            {
                "commands": [
                    {
                        "command": "sudo userdel demo_user",
                        "explanation": "删除演示用户",
                        "expected_outcome": "系统中不再存在 demo_user 用户",
                    },
                    {
                        "command": "id demo_user",
                        "explanation": "确认用户已删除",
                        "expected_outcome": "返回用户不存在的结果",
                    },
                ]
            }
        )
        executor = FakeExecutor()
        orchestrator = TaskOrchestrator(provider, executor, self.security)

        plan = orchestrator.build_plan("删除普通用户 demo_user 并验证是否已删除")

        self.assertEqual(len(plan.steps), 2)
        self.assertEqual(plan.intent_label, "delete_request")
        self.assertEqual(plan.total_risk, "high")
        self.assertTrue(plan.requires_confirmation)
        self.assertEqual(plan.steps[0].risk.level, "high")
        self.assertIn("userdel", plan.steps[0].selection_reason)
        self.assertIn("ubuntu", plan.steps[0].environment_rationale)

    def test_orchestrator_normalizes_delete_verification_command_from_id_probe(self):
        provider = FakeProvider(
            {
                "commands": [
                    {
                        "command": "sudo userdel demo_user",
                        "explanation": "删除演示用户",
                        "expected_outcome": "系统中不再存在 demo_user 用户",
                    },
                    {
                        "command": "id demo_user 2>&1 | grep -q 'no such' && echo '用户已删除' || echo '用户仍存在'",
                        "explanation": "确认用户已删除",
                        "expected_outcome": "显示用户已删除",
                    },
                ]
            }
        )
        executor = FakeExecutor()
        orchestrator = TaskOrchestrator(provider, executor, self.security)

        plan = orchestrator.build_plan("删除普通用户 demo_user 并验证是否已删除")

        self.assertEqual(plan.steps[0].command, "sudo userdel demo_user")
        self.assertEqual(plan.steps[1].command, "getent passwd demo_user || echo deleted")
        self.assertIn("demo_user", plan.steps[1].command)

    def test_orchestrator_normalizes_delete_command_without_home_removal_request(self):
        provider = FakeProvider(
            {
                "commands": [
                    {
                        "command": "sudo userdel -r demo_user",
                        "explanation": "删除用户及其主目录",
                        "expected_outcome": "用户被删除",
                    },
                    {
                        "command": "sudo deluser demo_user",
                        "explanation": "备用删除命令",
                        "expected_outcome": "用户被删除",
                    },
                ]
            }
        )
        executor = FakeExecutor()
        orchestrator = TaskOrchestrator(provider, executor, self.security)

        plan = orchestrator.build_plan("删除普通用户 demo_user，并验证是否已删除")

        self.assertEqual(plan.steps[0].command, "sudo userdel demo_user")
        self.assertEqual(plan.steps[1].command, "sudo userdel demo_user")

    def test_orchestrator_adapts_package_install_command_for_openeuler(self):
        provider = FakeProvider(
            {
                "commands": [
                    {
                        "command": "sudo apt install nginx",
                        "explanation": "安装 nginx",
                        "expected_outcome": "系统安装 nginx 软件包",
                    }
                ]
            }
        )
        executor = OpenEulerExecutor()
        orchestrator = TaskOrchestrator(provider, executor, self.security)

        plan = orchestrator.build_plan("安装 nginx")

        self.assertEqual(plan.environment_summary["distribution"], "openeuler")
        self.assertEqual(plan.environment_summary["package_manager"], "dnf")
        self.assertEqual(plan.steps[0].command, "sudo dnf install -y nginx")
        self.assertIn("dnf", plan.steps[0].environment_rationale)

    def test_orchestrator_adapts_package_install_command_for_centos(self):
        provider = FakeProvider(
            {
                "commands": [
                    {
                        "command": "sudo apt install nginx",
                        "explanation": "安装 nginx",
                        "expected_outcome": "系统安装 nginx 软件包",
                    }
                ]
            }
        )
        executor = CentOSExecutor()
        orchestrator = TaskOrchestrator(provider, executor, self.security)

        plan = orchestrator.build_plan("安装 nginx")

        self.assertEqual(plan.environment_summary["distribution"], "centos")
        self.assertEqual(plan.environment_summary["package_manager"], "yum")
        self.assertEqual(plan.steps[0].command, "sudo yum install -y nginx")
        self.assertIn("yum", plan.steps[0].environment_rationale)

    def test_orchestrator_executes_and_writes_audit_files(self):
        provider = FakeProvider({"command": "df -h", "explanation": "查看磁盘空间"})
        executor = FakeExecutor({"df -h": ("Filesystem info", "", 0)})

        with tempfile.TemporaryDirectory() as temp_dir:
            audit_store = AuditStore(temp_dir)
            orchestrator = TaskOrchestrator(
                provider,
                executor,
                self.security,
                audit_store=audit_store,
                analysis_mode="full",
            )

            trace = orchestrator.execute_task(
                "查看当前磁盘剩余空间",
                approval_callback=lambda prompt, level, step: True,
            )

            self.assertEqual(trace.status, "completed")
            self.assertEqual(len(trace.steps), 1)
            self.assertEqual(trace.steps[0].return_code, 0)

            task_dir = Path(temp_dir) / trace.task_id
            self.assertTrue((task_dir / "request.json").exists())
            self.assertTrue((task_dir / "plan.json").exists())
            self.assertTrue((task_dir / "events.jsonl").exists())
            self.assertTrue((task_dir / "result.json").exists())
            self.assertIn("state_transitions", trace.trace_summary)
            self.assertEqual(trace.trace_summary["state_transitions"][0]["state"], "planning")

            result_payload = json.loads((task_dir / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(result_payload["status"], "completed")
            events = [json.loads(line) for line in (task_dir / "events.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
            event_types = [event["type"] for event in events]
            self.assertIn("analysis_started", event_types)
            self.assertIn("analysis_finished", event_types)

    def test_orchestrator_stops_after_failed_step(self):
        provider = FakeProvider(
            {
                "commands": [
                    {"command": "echo ok", "explanation": "第一步"},
                    {"command": "false", "explanation": "第二步"},
                    {"command": "echo never", "explanation": "第三步"},
                ]
            }
        )
        executor = FakeExecutor(
            {
                "echo ok": ("ok", "", 0),
                "false": ("", "boom", 1),
                "echo never": ("never", "", 0),
            }
        )
        orchestrator = TaskOrchestrator(provider, executor, self.security)

        trace = orchestrator.execute_task("依次执行三步命令")

        self.assertEqual(trace.status, "failed")
        self.assertEqual([step.command for step in trace.steps], ["echo ok", "false"])
        self.assertEqual(executor.commands, ["echo ok", "false"])

    def test_orchestrator_falls_back_for_system_info_when_provider_returns_placeholder(self):
        provider = FakeProvider(
            {
                "command": "echo 'Unable to generate a proper command'",
                "explanation": "```json",
            }
        )
        executor = FakeExecutor()
        orchestrator = TaskOrchestrator(provider, executor, self.security, allow_template_fallback=True)

        plan = orchestrator.build_plan("显示系统基本信息")

        self.assertEqual(len(plan.steps), 1)
        self.assertIn("hostnamectl", plan.steps[0].command)
        self.assertIn("系统基本信息", plan.steps[0].goal)

    def test_orchestrator_streams_planning_events_before_returning_plan(self):
        provider = FakeProvider(
            {"command": "df -h", "explanation": "查看磁盘空间"},
            stream_chunks=['{"command":"df -h"', ',"explanation":"查看磁盘空间"}'],
        )
        executor = FakeExecutor()
        orchestrator = TaskOrchestrator(provider, executor, self.security)
        events = []

        plan = orchestrator.build_plan("查看当前磁盘剩余空间", event_callback=events.append)

        self.assertEqual(plan.steps[0].command, "df -h")
        self.assertEqual(
            [event["type"] for event in events],
            [
                "planning_stream_started",
                "planning_first_chunk",
                "planning_chunk",
                "planning_parse_started",
                "planning_parse_finished",
            ],
        )
        self.assertEqual(events[0]["planning_model"], "unknown")
        self.assertEqual(events[1]["first_token_ms"], 0)
        self.assertEqual(events[2]["planning_stream_preview"], '{"command":"df -h","explanation":"查看磁盘空间"}')

    def test_orchestrator_classifies_port_query_with_environment_rationale(self):
        provider = FakeProvider(
            {
                "intent_label": "check_port",
                "intent_summary": "检查 8080 端口占用与进程归属",
                "command": "sudo ss -tunap | grep ':8080'",
                "explanation": "查看 8080 端口是否被占用",
                "expected_outcome": "返回监听 8080 端口的进程信息",
            }
        )
        executor = FakeExecutor({"sudo ss -tunap | grep ':8080'": ("LISTEN 0 128 *:8080", "", 0)})
        orchestrator = TaskOrchestrator(provider, executor, self.security)

        plan = orchestrator.build_plan("查看 8080 端口是否被占用，并说明对应进程")

        self.assertEqual(plan.intent_label, "check_port")
        self.assertEqual(plan.parsing_summary["input_understanding"], "query")
        self.assertEqual(plan.steps[0].expected_outcome, "返回监听 8080 端口的进程信息")
        self.assertIn("ss", plan.steps[0].selection_reason)
        self.assertIn("ubuntu", plan.steps[0].environment_rationale)
        self.assertIn("ss", plan.parsing_summary["environment_decision"])

    def test_orchestrator_supports_file_search_request_with_find_plan(self):
        provider = FakeProvider(
            {
                "intent_label": "locate_recent_large_logs",
                "intent_summary": "检索最近修改过的大日志文件",
                "commands": [
                    {
                        "command": "find /var/log -type f -mtime -3 -size +50M",
                        "explanation": "查找最近 3 天修改过的大日志文件",
                        "expected_outcome": "输出符合条件的日志文件路径",
                    }
                ],
            }
        )
        executor = FakeExecutor()
        orchestrator = TaskOrchestrator(provider, executor, self.security)

        plan = orchestrator.build_plan("帮我找出 /var/log 下最近 3 天修改过的大文件")

        self.assertEqual(plan.intent_label, "locate_recent_large_logs")
        self.assertEqual(plan.steps[0].command, "find /var/log -type f -mtime -3 -size +50M")
        self.assertIn("find", plan.steps[0].selection_reason)
        self.assertIn("/var/log", plan.steps[0].command)

    def test_orchestrator_does_not_split_semicolons_inside_quoted_shell_snippets(self):
        provider = FakeProvider(
            {
                "command": (
                    "ps aux --sort=-%cpu | awk 'NR==2 {print $2}' | "
                    "xargs -I {} sh -c 'echo \"Path: $(readlink -f /proc/{}/exe)\"; "
                    "stat -c \"Permissions: %A, Owner: %U:%G\" $(readlink -f /proc/{}/exe)'"
                ),
                "explanation": "查看 CPU 占用最高进程的路径和权限",
                "expected_outcome": "输出路径和权限信息",
            }
        )
        executor = FakeExecutor()
        orchestrator = TaskOrchestrator(provider, executor, self.security)

        plan = orchestrator.build_plan("帮我查看当前系统中CPU占用最高的程序，并指出该程序的路径以及相关的权限")

        self.assertEqual(len(plan.steps), 1)
        self.assertIn("sh -c", plan.steps[0].command)
        self.assertIn("stat -c", plan.steps[0].command)

    def test_orchestrator_explains_cpu_top_process_lookup_rationale(self):
        provider = FakeProvider(
            {
                "command": (
                    "ps aux --sort=-%cpu | awk 'NR==2 {print $2}' | "
                    "xargs -I {} sh -c 'echo \"PID: {}\"; ls -l /proc/{}/exe; ls -l $(readlink /proc/{}/exe)'"
                ),
                "explanation": "获取 CPU 占用最高进程的路径和权限",
                "expected_outcome": "显示进程路径与权限信息",
            }
        )
        executor = FakeExecutor()
        orchestrator = TaskOrchestrator(provider, executor, self.security)

        plan = orchestrator.build_plan("帮我查看当前系统中CPU占用最高的程序，并指出该程序的路径以及相关的权限")

        self.assertIn("CPU", plan.steps[0].selection_reason)
        self.assertIn("/proc", plan.steps[0].environment_rationale)

    def test_orchestrator_builds_local_analysis_for_cpu_top_process_lookup(self):
        provider = FakeProvider(
            {
                "command": (
                    "sh -c 'pid=$(ps -eo pid=,%cpu=,comm= --sort=-%cpu | awk \"NR==1 {print \\$1}\"); "
                    "exe=$(readlink -f /proc/$pid/exe 2>/dev/null); "
                    "ps -p \"$pid\" -o pid=,comm=,%cpu= --no-headers; "
                    "printf \"PATH: %s\\n\" \"${exe:-N/A}\"; "
                    "[ -n \"$exe\" ] && stat -c \"PERMISSIONS: %A (%a) OWNER: %U GROUP: %G FILE: %n\" \"$exe\"'"
                ),
                "explanation": "获取 CPU 占用最高进程的路径和权限",
                "expected_outcome": "显示进程路径与权限信息",
            }
        )
        executor = FakeExecutor(
            {
                "sh -c 'pid=$(ps -eo pid=,%cpu=,comm= --sort=-%cpu | awk \"NR==1 {print \\$1}\"); exe=$(readlink -f /proc/$pid/exe 2>/dev/null); ps -p \"$pid\" -o pid=,comm=,%cpu= --no-headers; printf \"PATH: %s\\n\" \"${exe:-N/A}\"; [ -n \"$exe\" ] && stat -c \"PERMISSIONS: %A (%a) OWNER: %U GROUP: %G FILE: %n\" \"$exe\"'": (
                    "2178764 node 34.0\n"
                    "PATH: /home/shl203/.vscode-server/server/node\n"
                    "PERMISSIONS: -rwxr-xr-x (755) OWNER: shl203 GROUP: shl203 FILE: /home/shl203/.vscode-server/server/node\n",
                    "",
                    0,
                )
            }
        )
        orchestrator = TaskOrchestrator(provider, executor, self.security, analysis_mode="smart")

        trace = orchestrator.execute_task("帮我查看当前系统中CPU占用最高的程序，并指出该程序的路径以及相关的权限")

        self.assertEqual(trace.status, "completed")
        explanation = trace.steps[0].analysis["explanation"]
        self.assertIn("2178764", explanation)
        self.assertIn("/home/shl203/.vscode-server/server/node", explanation)
        self.assertIn("-rwxr-xr-x", explanation)
        self.assertIn("shl203:shl203", explanation)

    def test_orchestrator_upgrades_incomplete_cpu_process_permission_command(self):
        provider = FakeProvider(
            {
                "command": "ls -l /proc/$(ps -eo pid,%cpu --sort=-%cpu | awk 'NR==2 {print $1}')/exe",
                "explanation": "查看 CPU 占用最高进程的路径和权限",
                "expected_outcome": "显示路径和权限",
            }
        )
        executor = FakeExecutor()
        orchestrator = TaskOrchestrator(provider, executor, self.security)

        plan = orchestrator.build_plan("帮我查看当前系统中CPU占用最高的程序，并指出该程序的路径以及相关的权限")

        self.assertEqual(len(plan.steps), 1)
        self.assertIn("stat -c", plan.steps[0].command)
        self.assertIn("PATH:", plan.steps[0].command)

    def test_orchestrator_accepts_freeform_intent_label_for_non_catalog_task(self):
        provider = FakeProvider(
            {
                "intent_label": "investigate_storage_hotspots",
                "intent_summary": "定位磁盘热点并展开重点目录",
                "commands": [
                    {
                        "command": "du -xhd1 / | sort -h",
                        "explanation": "列出根目录下一层目录的容量",
                        "expected_outcome": "输出磁盘占用最高的一级目录",
                        "selection_reason": "先用 du 汇总目录大小，再决定下一步展开对象。",
                        "environment_rationale": "Linux 通用环境下 du 可直接统计目录大小，不依赖固定发行版特性。",
                    }
                ],
            }
        )
        executor = FakeExecutor()
        orchestrator = TaskOrchestrator(provider, executor, self.security)

        plan = orchestrator.build_plan("先找出占用空间最大的目录，再展开前三个子目录的大小")

        self.assertEqual(plan.response_mode, "execute")
        self.assertEqual(plan.intent_label, "investigate_storage_hotspots")
        self.assertEqual(plan.user_intent, "定位磁盘热点并展开重点目录")
        self.assertEqual(plan.steps[0].command, "du -xhd1 / | sort -h")
        self.assertIn("du", plan.steps[0].selection_reason)

    def test_orchestrator_requests_narrower_scope_for_broad_delete_plan(self):
        provider = FakeProvider(
            {
                "intent_label": "bulk_cleanup",
                "intent_summary": "批量清理临时文件",
                "command": "rm -rf /tmp /var/tmp /home/demo/tmp",
                "explanation": "批量删除多个临时目录",
            }
        )
        executor = FakeExecutor()
        orchestrator = TaskOrchestrator(provider, executor, self.security)

        plan = orchestrator.build_plan("帮我清理临时文件")

        self.assertEqual(plan.response_mode, "clarify")
        self.assertEqual(plan.intent_label, "bulk_cleanup")
        self.assertIn("范围", plan.clarification_prompt)
        self.assertEqual(plan.parsing_summary["result_mode"], "clarify")

    def test_orchestrator_returns_clarification_plan_from_provider_response(self):
        provider = FakeProvider(
            {
                "response_mode": "clarify",
                "needs_clarification": True,
                "intent_label": "account_provisioning",
                "intent_summary": "创建普通用户账号",
                "clarification_prompt": "请补充要创建的用户名，以及是否需要 sudo 权限。",
            }
        )
        executor = FakeExecutor()
        orchestrator = TaskOrchestrator(provider, executor, self.security)

        plan = orchestrator.build_plan("创建一个普通用户")

        self.assertEqual(plan.response_mode, "clarify")
        self.assertEqual(plan.intent_label, "account_provisioning")
        self.assertEqual(plan.steps, [])
        self.assertIn("用户名", plan.clarification_prompt)
        self.assertEqual(plan.parsing_summary["result_mode"], "clarify")

    def test_orchestrator_uses_generic_local_intent_for_observability_only(self):
        provider = FakeProvider(
            {
                "command": "sudo useradd demo_user",
                "explanation": "创建演示用户",
                "expected_outcome": "系统中出现 demo_user 用户",
            }
        )
        executor = FakeExecutor()
        orchestrator = TaskOrchestrator(provider, executor, self.security)

        plan = orchestrator.build_plan("创建一个普通用户 demo_user")

        self.assertEqual(plan.intent_label, "create_request")
        self.assertEqual(plan.parsing_summary["input_understanding"], "create")

    def test_orchestrator_execute_task_stops_with_needs_clarification_trace(self):
        provider = FakeProvider(
            {
                "response_mode": "clarify",
                "needs_clarification": True,
                "intent_label": "account_provisioning",
                "intent_summary": "创建普通用户账号",
                "clarification_prompt": "请补充要创建的用户名，以及是否需要 sudo 权限。",
            }
        )
        executor = FakeExecutor()
        orchestrator = TaskOrchestrator(provider, executor, self.security)
        events = []

        trace = orchestrator.execute_task("创建一个普通用户", event_callback=events.append)

        self.assertEqual(trace.status, "needs_clarification")
        self.assertEqual(trace.state, "needs_clarification")
        self.assertEqual(trace.steps, [])
        self.assertIn("用户名", trace.final_feedback)
        self.assertEqual(events[-1]["type"], "task_needs_clarification")

    def test_orchestrator_reflects_and_adjusts_remaining_steps(self):
        provider = FakeProvider(
            {
                "commands": [
                    {"command": "echo scan-root", "explanation": "扫描根目录"},
                    {"command": "echo stale-step", "explanation": "过时的第二步"},
                ]
            },
            analysis_response=lambda command, stdout, stderr: {
                "echo scan-root": {
                    "explanation": "根目录扫描完成，需要改为展开前三个热点目录。",
                    "recommendations": ["根据扫描结果改写剩余步骤"],
                    "next_steps": [
                        {
                            "command": "echo inspect-top3",
                            "explanation": "展开前三个热点目录",
                            "expected_outcome": "输出前三个热点目录的大小明细",
                        }
                    ],
                },
                "echo inspect-top3": {
                    "explanation": "热点目录分析完成",
                    "recommendations": [],
                    "next_steps": [],
                },
            }.get(command, {"explanation": "命令执行完成", "recommendations": [], "next_steps": []}),
        )
        executor = FakeExecutor(
            {
                "echo scan-root": ("root summary", "", 0),
                "echo inspect-top3": ("top3 details", "", 0),
                "echo stale-step": ("stale", "", 0),
            }
        )
        orchestrator = TaskOrchestrator(provider, executor, self.security, analysis_mode="full")
        events = []

        trace = orchestrator.execute_task(
            "先找出占用空间最大的目录，再展开前三个子目录的大小",
            event_callback=events.append,
        )

        self.assertEqual(trace.status, "completed")
        self.assertEqual(executor.commands, ["echo scan-root", "echo inspect-top3"])
        self.assertEqual(trace.trace_summary["plan_adjustments"], 1)
        self.assertGreaterEqual(trace.trace_summary["reflection_count"], 2)
        self.assertEqual(trace.steps[0].analysis["next_action"], "revise_remaining_steps")
        self.assertIn("reflection", trace.steps[0].analysis)
        self.assertIn("plan_adjusted", [event["type"] for event in events])

    def test_orchestrator_uses_recovery_steps_after_failure(self):
        provider = FakeProvider(
            {
                "commands": [
                    {"command": "false", "explanation": "执行原始诊断命令"},
                    {"command": "echo stale-followup", "explanation": "原始后续步骤"},
                ]
            },
            analysis_response=lambda command, stdout, stderr: {
                "false": {
                    "explanation": "原始命令失败，建议切换到恢复方案。",
                    "recommendations": ["执行安全恢复步骤"],
                    "next_steps": [
                        {
                            "command": "echo recover-status",
                            "explanation": "执行恢复后的检查步骤",
                            "expected_outcome": "返回恢复后的状态结果",
                        }
                    ],
                },
                "echo recover-status": {
                    "explanation": "恢复后的检查已完成",
                    "recommendations": [],
                    "next_steps": [],
                },
            }.get(command, {"explanation": "命令执行完成", "recommendations": [], "next_steps": []}),
        )
        executor = FakeExecutor(
            {
                "false": ("", "boom", 1),
                "echo recover-status": ("status ok", "", 0),
                "echo stale-followup": ("stale", "", 0),
            }
        )
        orchestrator = TaskOrchestrator(provider, executor, self.security, analysis_mode="full")
        events = []

        trace = orchestrator.execute_task(
            "检查 nginx 服务状态，如果失败就帮我分析原因",
            event_callback=events.append,
        )

        self.assertEqual(trace.status, "completed")
        self.assertEqual(executor.commands, ["false", "echo recover-status"])
        self.assertTrue(trace.trace_summary["recovery_used"])
        self.assertEqual(trace.trace_summary["plan_adjustments"], 1)
        self.assertEqual(trace.steps[0].analysis["next_action"], "recover")
        self.assertIn("recovery_suggested", [event["type"] for event in events])

    def test_orchestrator_treats_missing_user_delete_as_completed_state(self):
        provider = FakeProvider(
            {
                "commands": [
                    {"command": "sudo userdel demo_user", "explanation": "删除演示用户"},
                    {"command": "getent passwd demo_user || echo deleted", "explanation": "确认用户已删除"},
                ]
            },
            analysis_response=lambda command, stdout, stderr: {
                "sudo userdel demo_user": {
                    "explanation": "目标用户不存在，系统已处于删除后状态。",
                    "recommendations": [],
                    "next_steps": [
                        {"command": "sudo useradd -m demo_user", "explanation": "重新创建用户"},
                    ],
                },
                "getent passwd demo_user || echo deleted": {
                    "explanation": "已确认用户不存在。",
                    "recommendations": [],
                    "next_steps": [],
                },
            }.get(command, {"explanation": "命令执行完成", "recommendations": [], "next_steps": []}),
        )
        executor = FakeExecutor(
            {
                "sudo userdel demo_user": ("", "userdel：用户“demo_user”不存在\n", 6),
                "getent passwd demo_user || echo deleted": ("deleted\n", "", 0),
            }
        )
        orchestrator = TaskOrchestrator(provider, executor, self.security, analysis_mode="full")
        events = []

        trace = orchestrator.execute_task(
            "删除普通用户 demo_user，并验证是否已删除",
            approval_callback=lambda prompt, level, step: True,
            event_callback=events.append,
        )

        self.assertEqual(trace.status, "completed")
        self.assertEqual(executor.commands, ["sudo userdel demo_user", "getent passwd demo_user || echo deleted"])
        self.assertNotIn("recovery_suggested", [event["type"] for event in events])
        self.assertFalse(trace.trace_summary["recovery_used"])

    def test_orchestrator_uses_local_analysis_for_user_create_verification(self):
        provider = FakeProvider(
            {
                "commands": [
                    {"command": "sudo useradd demo_user", "explanation": "创建用户"},
                    {"command": "id demo_user", "explanation": "验证用户"},
                ]
            },
            analysis_response={
                "sudo useradd demo_user": {
                    "explanation": "用户创建成功",
                    "recommendations": [],
                    "next_steps": [],
                },
                "id demo_user": {
                    "explanation": "这条不应被调用",
                    "recommendations": [],
                    "next_steps": [],
                },
            },
        )
        executor = FakeExecutor(
            {
                "sudo useradd demo_user": ("", "", 0),
                "id demo_user": ("uid=1001(demo_user) gid=1001(demo_user) groups=1001(demo_user)", "", 0),
            }
        )
        orchestrator = TaskOrchestrator(provider, executor, self.security, analysis_mode="full")

        trace = orchestrator.execute_task(
            "创建一个普通用户 demo_user，并验证是否创建成功",
            approval_callback=lambda prompt, level, step: True,
        )

        self.assertEqual(trace.status, "completed")
        self.assertEqual(len(provider.analysis_calls), 1)
        self.assertEqual(trace.steps[-1].command, "id demo_user")
        self.assertIn("已确认用户 demo_user 存在", trace.steps[-1].analysis["explanation"])

    def test_orchestrator_uses_local_analysis_for_user_delete_verification(self):
        provider = FakeProvider(
            {
                "commands": [
                    {"command": "sudo userdel demo_user", "explanation": "删除用户"},
                    {"command": "getent passwd demo_user || echo deleted", "explanation": "验证删除"},
                ]
            },
            analysis_response={
                "sudo userdel demo_user": {
                    "explanation": "用户删除成功",
                    "recommendations": [],
                    "next_steps": [],
                },
                "getent passwd demo_user || echo deleted": {
                    "explanation": "这条不应被调用",
                    "recommendations": [],
                    "next_steps": [],
                },
            },
        )
        executor = FakeExecutor(
            {
                "sudo userdel demo_user": ("", "", 0),
                "getent passwd demo_user || echo deleted": ("deleted\n", "", 0),
            }
        )
        orchestrator = TaskOrchestrator(provider, executor, self.security, analysis_mode="full")

        trace = orchestrator.execute_task(
            "删除普通用户 demo_user，并验证是否已删除",
            approval_callback=lambda prompt, level, step: True,
        )

        self.assertEqual(trace.status, "completed")
        self.assertEqual(len(provider.analysis_calls), 1)
        self.assertEqual(trace.steps[-1].command, "getent passwd demo_user || echo deleted")
        self.assertIn("已确认用户 demo_user 不存在", trace.steps[-1].analysis["explanation"])

    def test_orchestrator_preserves_user_verification_steps_after_success(self):
        provider = FakeProvider(
            {
                "commands": [
                    {
                        "command": "sudo useradd -m demo_user",
                        "explanation": "创建用户",
                        "expected_outcome": "用户创建成功",
                    },
                    {
                        "command": "id demo_user",
                        "explanation": "验证用户存在",
                        "expected_outcome": "返回 demo_user 的 uid 和 gid",
                    },
                ]
            },
            analysis_response=lambda command, stdout, stderr: {
                "sudo useradd -m demo_user": {
                    "explanation": "用户已创建，建议补充检查家目录和密码。",
                    "recommendations": ["可进一步查看家目录"],
                    "next_steps": [
                        {
                            "command": "id demo_user",
                            "explanation": "确认用户存在",
                            "expected_outcome": "返回 demo_user 的 uid 和 gid",
                        },
                        {
                            "command": "ls -ld /home/demo_user",
                            "explanation": "检查家目录",
                            "expected_outcome": "显示家目录权限",
                        },
                        {
                            "command": "sudo passwd demo_user",
                            "explanation": "设置密码",
                            "expected_outcome": "设置用户密码",
                        },
                    ],
                },
                "id demo_user": {
                    "explanation": "用户验证完成",
                    "recommendations": [],
                    "next_steps": [],
                },
            }.get(command, {"explanation": "命令执行完成", "recommendations": [], "next_steps": []}),
        )
        executor = FakeExecutor(
            {
                "sudo useradd -m demo_user": ("", "", 0),
                "id demo_user": ("uid=1002(demo_user) gid=1002(demo_user) groups=1002(demo_user)", "", 0),
                "ls -ld /home/demo_user": ("drwxr-x--- 2 demo_user demo_user 4096 /home/demo_user", "", 0),
                "sudo passwd demo_user": ("", "", 0),
            }
        )
        orchestrator = TaskOrchestrator(provider, executor, self.security, analysis_mode="full")

        trace = orchestrator.execute_task(
            "创建一个普通用户 demo_user，并验证是否创建成功",
            approval_callback=lambda prompt, level, step: True,
        )

        self.assertEqual(trace.status, "completed")
        self.assertEqual(executor.commands, ["sudo useradd -m demo_user", "id demo_user"])
        self.assertFalse(trace.trace_summary["recovery_used"])
        self.assertEqual(trace.trace_summary["plan_adjustments"], 0)
        self.assertEqual(trace.steps[-1].command, "id demo_user")
        self.assertIn("已确认用户 demo_user 存在", trace.final_feedback)

    def test_orchestrator_stops_diagnostic_recovery_after_confirming_nginx_not_installed(self):
        provider = FakeProvider(
            {
                "commands": [
                    {"command": "systemctl status nginx", "explanation": "检查 nginx 服务状态"},
                    {
                        "command": 'journalctl -u nginx.service --since "10 minutes ago" --no-pager',
                        "explanation": "查看最近日志",
                    },
                ]
            },
            analysis_response=lambda command, stdout, stderr: {
                "systemctl status nginx": {
                    "explanation": "nginx 服务单元不存在，先确认系统里是否安装了 nginx。",
                    "recommendations": ["检查 nginx 可执行文件和已安装软件包"],
                    "next_steps": [
                        {
                            "command": "which nginx",
                            "explanation": "检查 nginx 可执行文件",
                            "expected_outcome": "返回 nginx 可执行文件路径",
                        },
                        {
                            "command": "sudo apt install nginx",
                            "explanation": "安装 nginx",
                            "expected_outcome": "安装 nginx 软件包",
                        },
                        {
                            "command": "systemctl status nginx",
                            "explanation": "再次检查 nginx 服务状态",
                            "expected_outcome": "显示 nginx 服务状态",
                        },
                    ],
                },
                "which nginx": {
                    "explanation": "未找到 nginx 可执行文件，继续确认系统是否安装了 nginx 软件包。",
                    "recommendations": ["检查 dpkg 包列表"],
                    "next_steps": [
                        {
                            "command": "dpkg -l | grep nginx",
                            "explanation": "检查 nginx 软件包",
                            "expected_outcome": "返回已安装的 nginx 软件包列表",
                        },
                        {
                            "command": "sudo apt install nginx",
                            "explanation": "安装 nginx",
                            "expected_outcome": "安装 nginx 软件包",
                        },
                    ],
                },
                "dpkg -l | grep nginx": {
                    "explanation": "系统中未检测到已安装的 nginx 软件包，因此当前 nginx 服务不存在。如需使用 nginx，可在确认后单独安装。",
                    "recommendations": ["如需启用 nginx，可后续单独执行安装"],
                    "next_steps": [
                        {
                            "command": "sudo apt install nginx",
                            "explanation": "安装 nginx",
                            "expected_outcome": "安装 nginx 软件包",
                        },
                        {
                            "command": "systemctl status nginx",
                            "explanation": "再次检查 nginx 服务状态",
                            "expected_outcome": "显示 nginx 服务状态",
                        },
                    ],
                },
            }.get(command, {"explanation": "命令执行完成", "recommendations": [], "next_steps": []}),
        )
        executor = FakeExecutor(
            {
                "systemctl status nginx": ("", "Unit nginx.service could not be found.", 4),
                "which nginx": ("", "", 1),
                "dpkg -l | grep nginx": ("", "", 1),
                'journalctl -u nginx.service --since "10 minutes ago" --no-pager': ("", "", 0),
            }
        )
        orchestrator = TaskOrchestrator(provider, executor, self.security, analysis_mode="full")

        trace = orchestrator.execute_task(
            "检查 nginx 服务状态，如果失败就帮我分析原因",
            approval_callback=lambda prompt, level, step: True,
        )

        self.assertEqual(trace.status, "completed")
        self.assertEqual(
            executor.commands,
            [
                "systemctl status nginx",
                "which nginx",
                "dpkg -l | grep nginx",
            ],
        )
        self.assertTrue(trace.trace_summary["recovery_used"])
        self.assertNotIn("sudo apt install nginx", executor.commands)
        self.assertIn("未检测到已安装的 nginx 软件包", trace.final_feedback)

    def test_orchestrator_sanitizes_mixed_package_lookup_and_stops_after_terminal_diagnostic_evidence(self):
        provider = FakeProvider(
            {
                "commands": [
                    {"command": "systemctl status nginx", "explanation": "检查 nginx 服务状态"},
                ]
            },
            analysis_response=lambda command, stdout, stderr: {
                "systemctl status nginx": {
                    "explanation": "nginx 服务单元不存在，继续检查软件包是否安装。",
                    "recommendations": ["检查 nginx 软件包"],
                    "next_steps": [
                        {
                            "command": "dpkg -l | grep nginx 或 rpm -qa | grep nginx",
                            "explanation": "检查 nginx 软件包",
                            "expected_outcome": "返回已安装的 nginx 软件包列表",
                        },
                        {
                            "command": "sudo apt install nginx",
                            "explanation": "安装 nginx",
                            "expected_outcome": "安装 nginx 软件包",
                        },
                    ],
                },
                "dpkg -l | grep nginx": {
                    "explanation": "系统中未检测到已安装的 nginx 软件包，因此当前 nginx 服务不存在。",
                    "recommendations": ["如需启用 nginx，可后续单独执行安装"],
                    "next_steps": [
                        {
                            "command": "cat /etc/os-release",
                            "explanation": "确认当前系统类型",
                            "expected_outcome": "显示系统版本信息",
                        },
                        {
                            "command": "sudo apt install nginx",
                            "explanation": "安装 nginx",
                            "expected_outcome": "安装 nginx 软件包",
                        },
                    ],
                },
            }.get(command, {"explanation": "命令执行完成", "recommendations": [], "next_steps": []}),
        )
        executor = FakeExecutor(
            {
                "systemctl status nginx": ("", "Unit nginx.service could not be found.", 4),
                "dpkg -l | grep nginx": ("", "", 1),
            }
        )
        orchestrator = TaskOrchestrator(provider, executor, self.security, analysis_mode="full")

        trace = orchestrator.execute_task(
            "检查 nginx 服务状态，如果失败就帮我分析原因",
            approval_callback=lambda prompt, level, step: True,
        )

        self.assertEqual(trace.status, "completed")
        self.assertEqual(
            executor.commands,
            [
                "systemctl status nginx",
                "dpkg -l | grep nginx",
            ],
        )
        self.assertNotIn("dpkg -l | grep nginx 或 rpm -qa | grep nginx", executor.commands)
        self.assertNotIn("cat /etc/os-release", executor.commands)
        self.assertIn("未检测到已安装的 nginx 软件包", trace.final_feedback)

    def test_orchestrator_sanitizes_mixed_package_lookup_for_centos_recovery(self):
        provider = FakeProvider(
            {
                "commands": [
                    {"command": "systemctl status nginx", "explanation": "检查 nginx 服务状态"},
                ]
            },
            analysis_response=lambda command, stdout, stderr: {
                "systemctl status nginx": {
                    "explanation": "nginx 服务单元不存在，继续检查软件包是否安装。",
                    "recommendations": ["检查 nginx 软件包"],
                    "next_steps": [
                        {
                            "command": "dpkg -l | grep nginx 或 rpm -qa | grep nginx",
                            "explanation": "检查 nginx 软件包",
                            "expected_outcome": "返回已安装的 nginx 软件包列表",
                        },
                    ],
                },
                "rpm -qa | grep nginx": {
                    "explanation": "系统中未检测到已安装的 nginx 软件包，因此当前 nginx 服务不存在。",
                    "recommendations": ["如需启用 nginx，可后续单独执行安装"],
                    "next_steps": [
                        {
                            "command": "cat /etc/os-release",
                            "explanation": "确认当前系统类型",
                            "expected_outcome": "显示系统版本信息",
                        },
                    ],
                },
            }.get(command, {"explanation": "命令执行完成", "recommendations": [], "next_steps": []}),
        )
        executor = CentOSExecutor(
            {
                "systemctl status nginx": ("", "Unit nginx.service could not be found.", 4),
                "rpm -qa | grep nginx": ("", "", 1),
            }
        )
        orchestrator = TaskOrchestrator(provider, executor, self.security, analysis_mode="full")

        trace = orchestrator.execute_task(
            "检查 nginx 服务状态，如果失败就帮我分析原因",
            approval_callback=lambda prompt, level, step: True,
        )

        self.assertEqual(trace.status, "completed")
        self.assertEqual(
            executor.commands,
            [
                "systemctl status nginx",
                "rpm -qa | grep nginx",
            ],
        )
        self.assertNotIn("cat /etc/os-release", executor.commands)
        self.assertIn("未检测到已安装的 nginx 软件包", trace.final_feedback)

    def test_orchestrator_prefers_package_lookup_over_path_diagnostics_after_missing_binary(self):
        provider = FakeProvider(
            {
                "commands": [
                    {"command": "systemctl status nginx", "explanation": "检查 nginx 服务状态"},
                ]
            },
            analysis_response=lambda command, stdout, stderr: {
                "systemctl status nginx": {
                    "explanation": "nginx 服务单元不存在，先确认系统里是否安装了 nginx。",
                    "recommendations": ["检查 nginx 二进制和软件包"],
                    "next_steps": [
                        {"command": "which nginx", "explanation": "检查 nginx 可执行文件"},
                        {"command": "sudo systemctl daemon-reload", "explanation": "重载 systemd 配置"},
                        {"command": "sudo apt install nginx", "explanation": "安装 nginx"},
                        {"command": "sudo yum install nginx", "explanation": "安装 nginx"},
                    ],
                },
                "which nginx": {
                    "explanation": "命令 `which nginx` 没有返回任何输出，表明系统当前无法在环境变量 $PATH 定义的目录中找到名为 'nginx' 的可执行文件。",
                    "recommendations": ["检查 nginx 是否已安装"],
                    "next_steps": [
                        {"command": "sudo apt install nginx 或 sudo yum install nginx", "explanation": "根据系统类型安装 nginx"},
                        {"command": "echo $PATH", "explanation": "检查当前环境变量路径"},
                        {"command": "which nginx 2>&1 || echo 'nginx not found in PATH'", "explanation": "再次确认命令不存在"},
                    ],
                },
                "dpkg -l | grep nginx": {
                    "explanation": "系统中未检测到已安装的 nginx 软件包，因此当前 nginx 服务不存在。",
                    "recommendations": ["如需启用 nginx，可后续单独执行安装"],
                    "next_steps": [],
                },
            }.get(command, {"explanation": "命令执行完成", "recommendations": [], "next_steps": []}),
        )
        executor = FakeExecutor(
            {
                "systemctl status nginx": ("", "Unit nginx.service could not be found.", 4),
                "which nginx": ("", "", 1),
                "dpkg -l | grep nginx": ("", "", 1),
                "echo $PATH": ("/usr/local/bin:/usr/bin:/bin\n", "", 0),
            }
        )
        orchestrator = TaskOrchestrator(provider, executor, self.security, analysis_mode="full")

        trace = orchestrator.execute_task(
            "检查 nginx 服务状态，如果失败就帮我分析原因",
            approval_callback=lambda prompt, level, step: True,
        )

        self.assertEqual(trace.status, "completed")
        self.assertEqual(
            executor.commands,
            [
                "systemctl status nginx",
                "which nginx",
                "dpkg -l | grep nginx",
            ],
        )
        self.assertNotIn("echo $PATH", executor.commands)
        self.assertIn("未检测到已安装的 nginx 软件包", trace.final_feedback)

    def test_orchestrator_raises_timeout_without_template_fallback_by_default(self):
        provider = FakeProvider(
            {"command": "df -h", "explanation": "查看磁盘空间"},
            stream_chunks=[],
        )
        executor = FakeExecutor()
        orchestrator = TaskOrchestrator(provider, executor, self.security)
        events = []

        with self.assertRaisesRegex(TimeoutError, "规划阶段等待模型响应超时"):
            orchestrator.build_plan("查看当前磁盘剩余空间", event_callback=events.append)

        self.assertEqual(
            [event["type"] for event in events],
            ["planning_stream_started", "planning_timeout"],
        )

    def test_orchestrator_normalizes_analysis_markdown_json(self):
        provider = FakeProvider(
            {"command": "df -h", "explanation": "查看磁盘"},
            {
                "explanation": "```json\n{\"explanation\":\"磁盘健康\",\"recommendations\":[\"定期巡检\"],\"next_steps\":[{\"command\":\"du -sh /\",\"explanation\":\"查看根目录\"}]}\n```"
            },
        )
        executor = FakeExecutor({"df -h": ("ok", "", 0)})
        orchestrator = TaskOrchestrator(provider, executor, self.security, analysis_mode="full")

        trace = orchestrator.execute_task("查看当前磁盘情况")

        self.assertEqual(trace.steps[0].analysis["explanation"], "磁盘健康")
        self.assertEqual(trace.steps[0].analysis["recommendations"], ["定期巡检"])
        self.assertEqual(trace.steps[0].analysis["next_steps"][0]["command"], "du -sh /")
        self.assertIn("磁盘健康", trace.final_feedback)

    def test_orchestrator_smart_mode_skips_analysis_for_single_low_risk_success(self):
        provider = FakeProvider({"command": "df -h", "explanation": "查看磁盘空间"})
        executor = FakeExecutor({"df -h": ("Filesystem info", "", 0)})
        orchestrator = TaskOrchestrator(provider, executor, self.security, analysis_mode="smart")

        trace = orchestrator.execute_task("查看当前磁盘剩余空间")

        self.assertEqual(provider.analysis_calls, [])
        self.assertEqual(trace.status, "completed")
        self.assertEqual(trace.trace_summary["analysis_ms"], 0)
        self.assertIn("planning_ms", trace.trace_summary)
        self.assertIn("execution_ms", trace.trace_summary)
        self.assertIn("total_ms", trace.trace_summary)
        self.assertIn("Filesystem info", trace.steps[0].analysis["explanation"])

    def test_orchestrator_smart_mode_keeps_analysis_for_failed_step(self):
        provider = FakeProvider(
            {"command": "false", "explanation": "制造失败"},
            {"explanation": "命令执行失败，请检查参数"},
        )
        executor = FakeExecutor({"false": ("", "boom", 1)})
        orchestrator = TaskOrchestrator(provider, executor, self.security, analysis_mode="smart")

        trace = orchestrator.execute_task("执行一条会失败的命令")

        self.assertEqual(trace.status, "failed")
        self.assertEqual(len(provider.analysis_calls), 1)
        self.assertGreaterEqual(trace.trace_summary["analysis_ms"], 0)
        self.assertIn("命令执行失败", trace.steps[0].analysis["explanation"])

    def test_orchestrator_treats_empty_port_grep_as_completed_observation(self):
        provider = FakeProvider(
            {
                "intent_label": "check_port",
                "intent_summary": "检查 8080 端口占用与进程归属",
                "command": "sudo ss -tulnp | grep :8080",
                "explanation": "查看 8080 端口是否被占用",
                "expected_outcome": "返回监听 8080 端口的进程信息",
            }
        )
        executor = FakeExecutor({"sudo ss -tulnp | grep :8080": ("", "", 1)})
        orchestrator = TaskOrchestrator(provider, executor, self.security, analysis_mode="smart")

        trace = orchestrator.execute_task("查看 8080 端口是否被占用，并说明对应进程")

        self.assertEqual(trace.status, "completed")
        self.assertEqual(executor.commands, ["sudo ss -tulnp | grep :8080"])
        self.assertEqual(provider.analysis_calls, [])
        self.assertEqual(trace.steps[0].status, "completed")
        self.assertIn("8080", trace.steps[0].analysis["explanation"])
        self.assertIn("未发现", trace.final_feedback)

    def test_orchestrator_drops_placeholder_recovery_commands(self):
        provider = FakeProvider(
            {"command": "false", "explanation": "执行原始诊断命令"},
            {
                "explanation": "原始命令失败，建议进一步诊断。",
                "recommendations": ["先确认服务名和脚本路径"],
                "next_steps": [
                    {
                        "command": "sudo systemctl status <相关服务名>",
                        "explanation": "检查服务运行状态",
                    },
                    {
                        "command": "bash -n <脚本文件路径>",
                        "explanation": "检查脚本语法",
                    },
                ],
            },
        )
        executor = FakeExecutor({"false": ("", "boom", 1)})
        orchestrator = TaskOrchestrator(provider, executor, self.security, analysis_mode="full")
        events = []

        trace = orchestrator.execute_task(
            "检查 8080 端口相关服务状态",
            event_callback=events.append,
        )

        self.assertEqual(trace.status, "failed")
        self.assertEqual(executor.commands, ["false"])
        self.assertNotIn("recovery_suggested", [event["type"] for event in events])
        self.assertIn("boom", trace.final_feedback)

    def test_orchestrator_retries_sudo_step_until_password_is_correct(self):
        provider = FakeProvider({"command": "sudo ss -tunap", "explanation": "查看端口"})
        executor = FakeExecutor(
            {
                "sudo ss -tunap": lambda password: ("", "Sorry, try again.", 1)
                if password != "secret"
                else ("port info", "", 0)
            }
        )
        orchestrator = TaskOrchestrator(provider, executor, self.security)
        credential_attempts = []
        events = []

        def credential_callback(prompt, step, attempt, last_error):
            credential_attempts.append(
                {
                    "prompt": prompt,
                    "command": step.command,
                    "attempt": attempt,
                    "last_error": last_error,
                }
            )
            return {
                "action": "submit",
                "password": "wrong" if attempt == 1 else "secret",
            }

        trace = orchestrator.execute_task(
            "查看端口状态",
            approval_callback=lambda prompt, level, step: True,
            credential_callback=credential_callback,
            event_callback=events.append,
        )

        self.assertEqual(trace.status, "completed")
        self.assertEqual(executor.passwords, ["wrong", "secret"])
        self.assertEqual([item["attempt"] for item in credential_attempts], [1, 2])
        self.assertEqual(credential_attempts[1]["last_error"], "sudo 密码错误，请重试。")
        self.assertIn("credential_rejected", [event["type"] for event in events])
        self.assertIn("credential_accepted", [event["type"] for event in events])

    def test_orchestrator_does_not_persist_sudo_password_in_audit_files(self):
        provider = FakeProvider({"command": "sudo ss -tunap", "explanation": "查看端口"})
        executor = FakeExecutor({"sudo ss -tunap": ("port info", "", 0)})

        with tempfile.TemporaryDirectory() as temp_dir:
            audit_store = AuditStore(temp_dir)
            orchestrator = TaskOrchestrator(
                provider,
                executor,
                self.security,
                audit_store=audit_store,
            )

            trace = orchestrator.execute_task(
                "查看端口状态",
                approval_callback=lambda prompt, level, step: True,
                credential_callback=lambda prompt, step, attempt, last_error: {"action": "submit", "password": "secret"},
            )

            task_dir = Path(temp_dir) / trace.task_id
            persisted = "\n".join(
                path.read_text(encoding="utf-8")
                for path in [
                    task_dir / "request.json",
                    task_dir / "plan.json",
                    task_dir / "events.jsonl",
                    task_dir / "result.json",
                ]
            )

            self.assertNotIn("secret", persisted)

    def test_audit_store_generates_human_readable_report(self):
        provider = FakeProvider({"command": "df -h", "explanation": "查看磁盘空间"})
        executor = FakeExecutor({"df -h": ("Filesystem info", "", 0)})

        with tempfile.TemporaryDirectory() as temp_dir:
            audit_store = AuditStore(temp_dir)
            orchestrator = TaskOrchestrator(
                provider,
                executor,
                self.security,
                audit_store=audit_store,
            )

            trace = orchestrator.execute_task(
                "查看当前磁盘剩余空间",
                approval_callback=lambda prompt, level, step: True,
            )

            report_path = Path(temp_dir) / trace.task_id / "report.md"
            self.assertTrue(report_path.exists())
            report_text = report_path.read_text(encoding="utf-8")
            self.assertIn("任务报告", report_text)
            self.assertIn("查看当前磁盘剩余空间", report_text)
            self.assertIn("df -h", report_text)
            self.assertIn("Filesystem info", report_text)
            self.assertIn("状态时间线", report_text)

    def test_orchestrator_generalizes_query_synonyms_for_disk_checks(self):
        provider = GeneralizationProvider()
        executor = FakeExecutor()
        orchestrator = TaskOrchestrator(provider, executor, self.security)

        for user_input in [
            "帮我看看当前磁盘剩余空间",
            "检查一下磁盘还剩多少",
            "确认当前磁盘剩余空间",
        ]:
            with self.subTest(user_input=user_input):
                plan = orchestrator.build_plan(user_input)
                self.assertEqual(plan.response_mode, "execute")
                self.assertEqual(plan.intent_label, "query_request")
                self.assertEqual(plan.parsing_summary["input_understanding"], "query")
                self.assertEqual(len(plan.steps), 1)
                self.assertIn("df", plan.steps[0].command)

    def test_orchestrator_generalizes_user_management_for_varied_usernames(self):
        provider = GeneralizationProvider()
        executor = FakeExecutor()
        orchestrator = TaskOrchestrator(provider, executor, self.security)

        expectations = [
            ("创建一个普通用户 alice_ops，并验证是否创建成功", "alice_ops", "useradd"),
            ("删除普通用户 testuser01，并验证是否已删除", "testuser01", "userdel"),
        ]

        for user_input, username, keyword in expectations:
            with self.subTest(user_input=user_input):
                plan = orchestrator.build_plan(user_input)
                self.assertEqual(plan.response_mode, "execute")
                self.assertEqual(len(plan.steps), 2)
                self.assertIn(keyword, plan.steps[0].command)
                self.assertIn(username, plan.steps[0].command)
                self.assertIn(username, plan.steps[1].command)
                self.assertTrue(plan.requires_confirmation)
                self.assertEqual(plan.total_risk, "high")

    def test_orchestrator_generalizes_service_checks_for_varied_services(self):
        provider = GeneralizationProvider()
        executor = FakeExecutor()
        orchestrator = TaskOrchestrator(provider, executor, self.security)

        cases = [
            ("检查 ssh 服务状态，如果失败就帮我分析原因", "ssh", "diagnose_request", "diagnose", 2),
            ("看看 cron 服务现在是否正常", "cron", "query_request", "query", 1),
        ]

        for user_input, service_name, intent_label, understanding, step_count in cases:
            with self.subTest(user_input=user_input):
                plan = orchestrator.build_plan(user_input)
                self.assertEqual(plan.intent_label, intent_label)
                self.assertEqual(plan.parsing_summary["input_understanding"], understanding)
                self.assertEqual(len(plan.steps), step_count)
                self.assertIn("systemctl status", plan.steps[0].command)
                self.assertIn(service_name, plan.steps[0].command)

    def test_orchestrator_generalizes_path_handling_for_find_and_cleanup(self):
        provider = GeneralizationProvider()
        executor = FakeExecutor()
        orchestrator = TaskOrchestrator(provider, executor, self.security)

        explicit_plan = orchestrator.build_plan("帮我找出 /opt 下最近 7 天改过的大文件")
        self.assertEqual(explicit_plan.response_mode, "execute")
        self.assertEqual(explicit_plan.intent_label, "query_request")
        self.assertIn("find /opt", explicit_plan.steps[0].command)

        clarify_plan = orchestrator.build_plan("帮我清理临时文件")
        self.assertEqual(clarify_plan.response_mode, "clarify")
        self.assertIn("范围", clarify_plan.clarification_prompt)
        self.assertEqual(clarify_plan.parsing_summary["result_mode"], "clarify")

    def test_orchestrator_generalizes_diagnostic_recovery_for_non_sample_service(self):
        provider = GeneralizationProvider()
        executor = FakeExecutor(
            {
                "systemctl status redis-server": ("", "Unit redis-server.service could not be found.", 4),
                "which redis-server": ("", "", 1),
                "dpkg -l | grep redis-server": ("", "", 1),
                'journalctl -u redis-server.service --since "10 minutes ago" --no-pager': ("", "", 0),
            }
        )
        orchestrator = TaskOrchestrator(provider, executor, self.security, analysis_mode="full")

        trace = orchestrator.execute_task(
            "检查 redis-server 服务状态，如果失败就帮我分析原因",
            approval_callback=lambda prompt, level, step: True,
        )

        self.assertEqual(trace.status, "completed")
        self.assertEqual(
            executor.commands,
            [
                "systemctl status redis-server",
                "which redis-server",
                "dpkg -l | grep redis-server",
            ],
        )
        self.assertTrue(trace.trace_summary["recovery_used"])
        self.assertNotIn("sudo apt install redis-server", executor.commands)
        self.assertIn("未检测到已安装的 redis-server 软件包", trace.final_feedback)

    def test_linux_command_executor_injects_sudo_password_non_interactively(self):
        security = SimpleNamespace(
            confirm_dangerous_commands=True,
            blocked_commands=[],
            confirm_patterns=[],
        )
        executor = LinuxCommandExecutor(security)
        process = Mock()
        process.communicate.return_value = ("ok", "")
        process.returncode = 0

        with patch("src.executors.linux_command.subprocess.Popen", return_value=process) as popen:
            stdout, stderr, return_code = executor.execute_command("sudo ss -tunap", sudo_password="secret")

        self.assertEqual((stdout, stderr, return_code), ("ok", "", 0))
        popen.assert_called_once()
        self.assertIn("sudo -S -p '' ss -tunap", popen.call_args.args[0])
        process.communicate.assert_called_once_with(input="secret\n", timeout=120)

    def test_linux_command_executor_does_not_treat_batched_top_pipeline_as_interactive(self):
        security = SimpleNamespace(
            confirm_dangerous_commands=True,
            blocked_commands=[],
            confirm_patterns=[],
        )
        executor = LinuxCommandExecutor(security)
        process = Mock()
        process.communicate.return_value = ("top output", "")
        process.returncode = 0

        with patch("src.executors.linux_command.subprocess.Popen", return_value=process) as popen:
            stdout, stderr, return_code = executor.execute_command("top -b -n 1 | head -20")

        self.assertEqual((stdout, stderr, return_code), ("top output", "", 0))
        popen.assert_called_once()
        process.communicate.assert_called_once_with(input=None, timeout=120)


if __name__ == "__main__":
    unittest.main()
